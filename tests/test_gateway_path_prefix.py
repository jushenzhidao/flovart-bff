"""网关路径前缀契约测试。

🔴 背景（飞哥 2026-09-15 实报）：
前端选完 GPT Image 2.5 模型点生成 → `POST /api/tasks` 返回 **502**，
前端提示「网关未实现该图片端点（images/generations 返回了前端页面而非 JSON）」。

真因不是网关没实现，而是 **BFF 拼出的 URL 少了 `/v1` 前缀**：
- `base_url = NEWAPI_BASE_URL`（如 https://newapi-bff.oneapis.cn，不带 /v1）
- 相对路径曾是 `images/generations`
- httpx 拼成 `{base}/images/generations` —— nginx 上**该路径不存在** →
  被兜底给 new-api 的**前端 SPA** → 返回 `200 text/html`（<title>New API</title>）
- BFF 解析 JSON 失败 → 502

网关实测对照：
    POST /v1/images/generations  → 401 application/json   ✅ 端点存在
    POST /images/generations     → 200 text/html          ❌ 前端页面兜底

本文件锁定「所有网关路径必须带 v1/ 前缀」，防止回归。
"""
import pytest

from app import config


GATEWAY_PATH_ATTRS = [
    "GATEWAY_IMAGE_TASKS_PATH",
    "GATEWAY_VIDEO_TASKS_PATH",
    "GATEWAY_SYNC_IMAGE_PATH",
    "GATEWAY_SYNC_UPSCALE_PATH",
    "GATEWAY_SYNC_REMOVE_BG_PATH",
    "GATEWAY_SYNC_SPLIT_PATH",
    "GATEWAY_SYNC_OUTPAINT_PATH",
    "GATEWAY_SYNC_MASK_PATH",
    "GATEWAY_SYNC_ANNOTATE_PATH",
    "GATEWAY_SYNC_RELIGHT_PATH",
    "GATEWAY_SYNC_EDIT_PATH",
]


@pytest.mark.parametrize("attr", GATEWAY_PATH_ATTRS)
def test_gateway_path_has_v1_prefix(attr):
    """每个网关路径都必须以 v1/ 开头 —— 否则会打到前端页面兜底。"""
    value = getattr(config, attr)
    assert value, f"{attr} 不应为空"
    assert value.startswith("v1/"), (
        f"{attr}={value!r} 缺少 `v1/` 前缀。\n"
        f"网关的 OpenAI 兼容端点全部在 /v1/* 下，base_url 是 NEWAPI_BASE_URL（不带 /v1），\n"
        f"相对路径必须自带 v1/，否则 nginx 会把请求兜底给前端 SPA → 返回 HTML → 502。"
    )


@pytest.mark.parametrize("attr", GATEWAY_PATH_ATTRS)
def test_gateway_path_no_leading_or_trailing_slash(attr):
    """路径不得有首尾斜杠 —— httpx 拼接时会产生双斜杠。"""
    value = getattr(config, attr)
    assert not value.startswith("/"), f"{attr}={value!r} 不应以 / 开头（会与 base_url 拼成 //）"
    assert not value.endswith("/"), f"{attr}={value!r} 不应以 / 结尾"


def test_sync_paths_all_converged_to_generations():
    """2026-09-14 拍板：所有同步图片能力统一走 v1/images/generations（靠 body 字段区分）。"""
    sync_attrs = [a for a in GATEWAY_PATH_ATTRS if a.startswith("GATEWAY_SYNC_")]
    for attr in sync_attrs:
        assert getattr(config, attr) == "v1/images/generations", (
            f"{attr} 应统一为 v1/images/generations（上游只实现这一个端点）"
        )


# ---------------------------------------------------------------------------
# 异步任务端点单独锁定（2026-09-15 二次修正）
# ---------------------------------------------------------------------------
# 早期默认值 `v1/contents/generations/tasks` 系按契约文档（未实测）填写，
# 网关实测 **404**（路径根本不存在）。
# 依据 new-api 源码 router/video-router.go（SetVideoRouter）的真实路由：
#     POST /v1/video/generations            ← 提交（controller.RelayTask）
#     GET  /v1/video/generations/:task_id   ← 轮询（controller.RelayTaskFetch）
#     POST /v1/videos                       ← OpenAI 兼容别名
#     GET  /v1/videos/:video_id             ← OpenAI 兼容别名轮询
# 图片 / 视频异步任务在网关侧**共用该 video 路由组**，靠 body 里的模型分流，
# 故 BFF 的 image / video 两个 env 默认同值。
# 实测（2026-09-15，均返回 401 JSON = 端点存在）：
#     POST /v1/video/generations           → 401 application/json ✅
#     GET  /v1/video/generations/{id}      → 401 application/json ✅
#     POST /v1/contents/generations/tasks  → 404 application/json ❌ 旧值
ASYNC_TASK_PATH_ATTRS = ["GATEWAY_IMAGE_TASKS_PATH", "GATEWAY_VIDEO_TASKS_PATH"]


@pytest.mark.parametrize("attr", ASYNC_TASK_PATH_ATTRS)
def test_async_task_path_is_video_generations(attr):
    """异步任务端点必须是 v1/video/generations —— 旧的 contents/... 实测 404。"""
    value = getattr(config, attr)
    assert value == "v1/video/generations", (
        f"{attr}={value!r} 不正确。\n"
        f"new-api 的异步任务端点是 /v1/video/generations（源码 router/video-router.go），\n"
        f"旧的 v1/contents/generations/tasks 实测 404（路径不存在）。"
    )


def test_async_path_and_poll_path_shape():
    """轮询/取消靠 `{async_path}/{task_id}` 拼接 —— 断言该形态与网关路由吻合。

    tasks.py 中 get_task / cancel_task 均用 f"{async_path}/{task_id}"，
    对应 new-api 的 GET /v1/video/generations/:task_id。
    """
    for attr in ASYNC_TASK_PATH_ATTRS:
        path = getattr(config, attr)
        composed = f"{path}/task-123"
        assert composed == "v1/video/generations/task-123"
        assert not composed.startswith("/"), "拼接后仍不得以 / 开头"
