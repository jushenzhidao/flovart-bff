#!/usr/bin/env bash
# =============================================================================
# flovart-bff 部署后冒烟测试
#
# 设计原则：**按「会碰到哪条 new-api 凭证通道」分层**，而不是按功能分层。
# 原因见下方「通道风险表」—— 这个 BFF 有大量「用户级接口」在兜底时会走管理员
# 通道，而管理员账号目前与 hewapi 共用，碰了就互踢。
#
# 用法：
#   ./scripts/smoke_test.sh                          # 仅 L0（零 new-api 接触）
#   ./scripts/smoke_test.sh --user 测试账号:密码      # + L1（用户通道）
#   ./scripts/smoke_test.sh --user u:p --admin-channel   # + L2（🔴 会踢 hewapi）
#
#   BASE_URL=http://127.0.0.1:8310 ./scripts/smoke_test.sh
#
# 通道风险表（依据 app/routers/* 与 app/newapi_client.py 实际调用）：
#   L0  /healthz /readyz / /api/config + 未登录 401 闸门   → 纯本地，零风险
#   L1  login / user/self / token / log/self / logout      → 仅该账号自己的 PAT
#   L2  me/points / models / console/* / register / shares → 🔴 管理员 PAT
#   ⚠️  chat / tasks（生图）默认复用本地缓存 sk-，**缓存未命中时走管理员代建**，
#      所以新卷首次生图也会踢 —— 见 L2 说明。
# =============================================================================
set -uo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BASE_URL="${BASE_URL:-http://127.0.0.1:8310}"
USER_CRED=""
ALLOW_ADMIN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --url) BASE_URL="${2:-}"; shift 2 ;;
    --user) USER_CRED="${2:-}"; shift 2 ;;
    --admin-channel) ALLOW_ADMIN=1; shift ;;
    -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1（-h 看用法）"; exit 2 ;;
  esac
done

COOKIE_JAR="$(mktemp)"
trap 'rm -f "$COOKIE_JAR"' EXIT

PASS=0; FAIL=0; SKIP=0; WARN=0
c_pass() { PASS=$((PASS+1)); printf '  \033[32m[PASS]\033[0m %s\n' "$*"; }
c_fail() { FAIL=$((FAIL+1)); printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; }
c_skip() { SKIP=$((SKIP+1)); printf '  \033[36m[SKIP]\033[0m %s\n' "$*"; }
c_warn() { WARN=$((WARN+1)); printf '  \033[33m[WARN]\033[0m %s\n' "$*"; }
c_info() { printf '         %s\n' "$*"; }
sec()    { printf '\n=== %s ===\n' "$*"; }

# ---------------------------------------------------------------- HTTP 封装
LAST_CODE=""; LAST_BODY=""
req() {   # req <method> <path> [curl args...]
  local method="$1" path="$2"; shift 2
  local out
  out=$(curl -sS --max-time 25 -X "$method" -b "$COOKIE_JAR" -c "$COOKIE_JAR" \
        -w $'\n%{http_code}' "$@" "${BASE_URL}${path}" 2>&1) || true
  LAST_CODE="${out##*$'\n'}"
  LAST_BODY="${out%$'\n'*}"
  case "$LAST_CODE" in
    [0-9][0-9][0-9]) ;;
    *) LAST_CODE="000"; LAST_BODY="$out" ;;
  esac
}

expect() {   # expect <描述> <期望码>
  if [ "$LAST_CODE" = "$2" ]; then
    c_pass "$1  → $LAST_CODE"
  else
    c_fail "$1  → 期望 $2，实际 $LAST_CODE"
    c_info "body: $(printf '%s' "$LAST_BODY" | head -c 220)"
  fi
}

json_field() {   # json_field <json> <dotted.path>
  local json="$1" path="$2"
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for k in sys.argv[1].split("."):
    try:
        d = d[int(k)] if isinstance(d, list) else d.get(k)
    except Exception:
        sys.exit(0)
    if d is None:
        sys.exit(0)
print(d)' "$path" 2>/dev/null
    return
  fi
  # 无 python3 的退化实现：仅支持顶层标量
  printf '%s' "$json" \
    | sed -n "s/.*\"${path##*.}\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^,\"}]*\)\"\{0,1\}.*/\1/p" \
    | head -1
}

expect_field() {   # expect_field <描述> <字段路径> <期望值>
  local desc="$1" path="$2" want="$3"
  local got; got="$(json_field "$LAST_BODY" "$path")"
  if [ "$got" = "$want" ]; then
    c_pass "$desc = $got"
  else
    c_fail "$desc → 期望 '$want'，实际 '$got'"
    c_info "body: $(printf '%s' "$LAST_BODY" | head -c 220)"
  fi
}

printf 'flovart-bff 冒烟测试\n  BASE_URL = %s\n  REPO_DIR = %s\n' "$BASE_URL" "$REPO_DIR"

# ---------------------------------------------------------------- L0-a 配置体检
sec "0. 配置体检（读 ${REPO_DIR}/.env，不需要服务在线）"

ENV_FILE="${REPO_DIR}/.env"
if [ ! -f "$ENV_FILE" ]; then
  c_warn ".env 不存在 —— 服务器上必须手动放一份（被 .gitignore/.dockerignore 双挡）"
else
  env_get() { sed -n "s/^$1=//p" "$ENV_FILE" | head -1 | tr -d '\r'; }

  image_var="$(env_get FLOVART_BFF_IMAGE)"
  stale_image="$(env_get BFF_IMAGE)"
  if printf '%s' "$stale_image" | grep -q 'newapi-bff'; then
    c_warn "残留的 BFF_IMAGE=${stale_image}"
    c_info "compose 改名后该行已被忽略（这是设计），但说明配置是照抄 hewapi 的，"
    c_info "建议删掉以免下次误读。"
  fi
  if [ -n "$image_var" ]; then
    c_pass "FLOVART_BFF_IMAGE=${image_var}"
    case "$image_var" in
      *:latest) c_warn "用了 :latest —— 无法回溯线上跑的是哪个 commit，建议钉 sha-xxxxxxx" ;;
    esac
  else
    c_info "未设 FLOVART_BFF_IMAGE → 路线 A（服务器本地构建）"
  fi

  admin_uid="$(env_get NEWAPI_ADMIN_UID)"
  if [ "$admin_uid" = "1" ]; then
    c_warn "NEWAPI_ADMIN_UID=1 —— 与 hewapi 共用同一个管理员账号"
    c_info "这是互踢的根因：任一边 401 重登都会重新签发 PAT 并作废对方的。"
    c_info "根治办法：给 flovart 单开一个 new-api 管理员账号（见 DEPLOY-CUTOVER.md 1.4）。"
  elif [ -n "$admin_uid" ]; then
    c_pass "NEWAPI_ADMIN_UID=${admin_uid}（已与 hewapi 分离）"
  fi

  brand="$(env_get BFF_BRAND_NAME)"
  case "$brand" in
    oneArt) c_pass "BFF_BRAND_NAME=${brand}" ;;
    "Workbuddy积分") c_warn "BFF_BRAND_NAME=${brand} —— 这是 hewapi 的品牌，前端会串台" ;;
    "") c_warn "BFF_BRAND_NAME 未设（用代码默认值）" ;;
    *) c_warn "BFF_BRAND_NAME=${brand}（确认是本次要的品牌）" ;;
  esac

  lf_env="$(env_get LOGFIRE_ENVIRONMENT)"
  lf_tok="$(env_get LOGFIRE_TOKEN)"
  if [ -z "$lf_tok" ]; then
    c_warn "LOGFIRE_TOKEN 为空 → 上报关闭（业务无影响，只是没有 trace）"
  elif [ "$lf_env" = "local" ] || [ -z "$lf_env" ]; then
    c_warn "LOGFIRE_ENVIRONMENT=${lf_env:-未设} —— 与 hewapi 同名会混在一起，建议 flovart-prod"
  else
    c_pass "LOGFIRE_ENVIRONMENT=${lf_env}"
  fi

  fwd="$(env_get FORWARDED_ALLOW_IPS)"
  if [ "$fwd" = "127.0.0.1" ] || [ -z "$fwd" ]; then
    c_warn "FORWARDED_ALLOW_IPS=${fwd:-默认 127.0.0.1} —— 容器化后应填 docker0 网关（如 172.17.0.1）"
    c_info "否则 uvicorn 丢弃 X-Forwarded-For → 按 IP 限流把所有用户当成同一人（不报错）"
  else
    c_pass "FORWARDED_ALLOW_IPS=${fwd}"
  fi
fi

# ---------------------------------------------------------------- L0 无凭证层
sec "1. [L0] 本地探针（不接触 new-api）"

req GET /healthz
expect "GET /healthz" 200
expect_field "  data.service" "data.service" "flovart-bff"

req GET /readyz
expect "GET /readyz" 200
if [ "$LAST_CODE" = "200" ] && printf '%s' "$LAST_BODY" | grep -q '"success":true'; then
  c_pass "  readyz 三项检查全通过（secret_key / admin_cred / state_dir）"
  c_info "⚠️ 注意：admin_cred 只校验「配置存在」，不代表管理员凭证真的可用"
fi

req GET /
expect "GET /" 200
expect_field "  data.service" "data.service" "flovart-bff"

sec "2. [L0] 站点配置 /api/config（纯本地，前端启动即拉）"

req GET /api/config
expect "GET /api/config" 200
expect_field "  brand.name" "data.brand.name" "oneArt"
if [ "$LAST_CODE" = "200" ]; then
  c_info "api.base_url = $(json_field "$LAST_BODY" data.api.base_url)"
  c_info "version      = $(json_field "$LAST_BODY" data.version)"
  c_info "赠送开关     = $(json_field "$LAST_BODY" data.features.signup_bonus_enabled) / $(json_field "$LAST_BODY" data.features.signup_bonus_points) 积分"
  c_info "⚠️ 上面 version 应与 .env 的 APP_VERSION、以及镜像 tag 三者一致；"
  c_info "   不一致说明容器跑的不是你以为的那一版。"
fi

sec "3. [L0] 鉴权闸门（未登录应 401，绝不能放行）"

for p in /api/user/self /api/me/points /api/token /api/log/self \
         /api/console/overview /api/console/models /api/me/storage/overview; do
  req GET "$p"
  expect "GET $p（无 Cookie）" 401
done

# ---------------------------------------------------------------- L1 用户通道
sec "4. [L1] 用户通道（仅影响该测试账号自己的 PAT）"

if [ -z "$USER_CRED" ]; then
  c_skip "未提供 --user 账号密码，跳过（用法：--user 用户名:密码）"
  c_info "这一层安全：只调用户自己的凭证，不会碰管理员 PAT。"
else
  U_NAME="${USER_CRED%%:*}"; U_PASS="${USER_CRED#*:}"
  admin_user="$(sed -n 's/^NEWAPI_ADMIN_USERNAME=//p' "$ENV_FILE" 2>/dev/null | head -1 | tr -d '\r')"

  if [ -n "$admin_user" ] && [ "$U_NAME" = "$admin_user" ]; then
    c_fail "禁止用管理员账号（$U_NAME）登录 BFF —— 会重新签发它的 PAT 并踢掉 hewapi"
    c_info "换成任意一个普通用户账号重跑。"
  else
    req POST /api/user/login -H 'Content-Type: application/json' \
        -d "{\"username\":\"$U_NAME\",\"password\":\"$U_PASS\"}"
    expect "POST /api/user/login" 200
    if [ "$LAST_CODE" != "200" ]; then
      c_info "登录失败就到此为止 —— 后面的用例都会连带失败，先查这个。"
    else
      SESS_NAME="$(json_field "$LAST_BODY" data.username)"
      c_info "登录成功：$SESS_NAME  role=$(json_field "$LAST_BODY" data.role)"

      req GET /api/user/self
      expect "GET /api/user/self" 200
      expect_field "  data.username" "data.username" "$U_NAME"
      c_info "points = $(json_field "$LAST_BODY" data.points)  used = $(json_field "$LAST_BODY" data.used_points)"

      req GET "/api/token"
      expect "GET /api/token" 200

      req GET "/api/log/self?p=1&page_size=5"
      expect "GET /api/log/self" 200

      req GET /api/me/storage/overview
      expect "GET /api/me/storage/overview" 200

      req GET /api/user/logout
      expect "GET /api/user/logout" 200

      req GET /api/user/self
      expect "GET /api/user/self（登出后应失效）" 401
    fi
  fi
fi

# ---------------------------------------------------------------- L2 管理通道
sec "5. [L2] 管理通道依赖项（🔴 会触发管理员 PAT 轮换）"

if [ "$ALLOW_ADMIN" -eq 0 ]; then
  c_skip "未加 --admin-channel，跳过以下用例："
  c_info "GET  /api/me/points        ← 恒走 admin_get_user"
  c_info "GET  /api/models           ← 用户 sk- 失败时回落 admin_enabled_models"
  c_info "GET  /api/console/overview ← 管理台总览"
  c_info "GET  /api/console/models   ← 全站模型列表"
  c_info ""
  c_info "⚠️ 为什么默认不跑：这些接口用管理员凭证打 new-api。而 _admin_login() 里的"
  c_info "   GET /api/user/token 会【重新签发 PAT 并作废旧值】—— 只要管理员账号仍与"
  c_info "   hewapi 共用，跑这一步就会把 hewapi 踢下线，两边开始乒乓。"
  c_info "   确认已在 .env 里换成独立管理员账号后，再加 --admin-channel 重跑。"
else
  if [ -z "$USER_CRED" ]; then
    c_skip "L2 需要登录态，请同时加 --user"
  else
    c_warn "即将触发管理员通道 —— 若账号仍与 hewapi 共用，hewapi 会被踢一次"
    req POST /api/user/login -H 'Content-Type: application/json' \
        -d "{\"username\":\"${USER_CRED%%:*}\",\"password\":\"${USER_CRED#*:}\"}"
    expect "重新登录（L2 前置）" 200

    req GET /api/me/points
    expect "GET /api/me/points（管理员读余额）" 200
    c_info "points = $(json_field "$LAST_BODY" data.points)"

    req GET /api/models
    expect "GET /api/models" 200
    c_info "total = $(json_field "$LAST_BODY" data.total)  scoped = $(json_field "$LAST_BODY" data.scoped)"
    c_info "scoped=true（用户分组过滤，正常）；false 说明回落了全站列表，值得查"

    req GET /api/console/overview
    expect "GET /api/console/overview" 200

    req GET /api/console/models
    expect "GET /api/console/models" 200
  fi
fi

# ---------------------------------------------------------------- 汇总
printf '\n=============================================\n'
printf '  通过 %d   失败 %d   警告 %d   跳过 %d\n' "$PASS" "$FAIL" "$WARN" "$SKIP"
printf '=============================================\n'
if [ "$FAIL" -gt 0 ]; then
  printf '有失败项 —— L0/L1 失败通常是 .env、BFF_SECRET_KEY 或反代配置问题。\n'
  exit 1
fi
printf '未登录闸门与本地探针全通过：容器本身是健康的。\n'
if [ "$ALLOW_ADMIN" -eq 0 ]; then
  printf '业务路径尚未验证（需要 new-api 侧配合）—— 见上面 L2 的说明。\n'
fi
