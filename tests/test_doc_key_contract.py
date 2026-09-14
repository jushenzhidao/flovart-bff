"""doc_key / scope 校验契约 —— 锁定「正式版 doc_key 必须放行」这条回归。

背景（真实事故）：
  前端「正式版」层用 `<项目id>@v<N>` 作为 doc_key（见 DESIGN-draft-vs-saved.md）。
  但 `_SCOPE_KEY_RE` 原为 `^[A-Za-z0-9_.:-]{1,128}$` —— **不含 `@`**，
  导致保存正式版的 PUT 直接 400「doc_key 格式不合法」，功能完全不可用。
  设计文档 §7 风险表曾写「`_SCOPE_KEY_RE` 已允许 `@`」，但代码实际没允许
  —— 文档与实现不一致，正是这条测试要防的回归。
"""
from app.routers.cloud import _SCOPE_KEY_RE, _validate_scope_key, ALLOWED_SCOPES

import pytest
from fastapi import HTTPException


def _ok(key: str) -> bool:
    return bool(_SCOPE_KEY_RE.match(key))


@pytest.mark.parametrize("key", [
    "proj-abc",                       # 普通草稿 doc_key
    "Uv9wmOsjhWwnEzBoBZFLn@v1",       # 正式版 doc_key（本次事故的实际值）
    "proj-abc@v3",
    "generations",
    "a" * 128,                        # 边界：正好 128
])
def test_valid_keys(key):
    assert _ok(key), f"应放行: {key!r}"


@pytest.mark.parametrize("key", [
    "",                               # 空
    "a" * 129,                        # 超长
    "has space",
    "slash/inside",
    "new\nline",
])
def test_invalid_keys(key):
    assert not _ok(key), f"应拒绝: {key!r}"


def test_version_doc_key_passes_validation():
    """正式版 doc_key 必须通过完整校验（scope + key）。"""
    _validate_scope_key("projects", "Uv9wmOsjhWwnEzBoBZFLn@v1")  # 不抛 = 通过


def test_unknown_scope_rejected():
    with pytest.raises(HTTPException) as exc:
        _validate_scope_key("not-a-scope", "whatever")
    assert exc.value.status_code == 400


def test_illegal_key_rejected_with_400():
    with pytest.raises(HTTPException) as exc:
        _validate_scope_key("projects", "bad key with spaces")
    assert exc.value.status_code == 400


def test_projects_scope_is_allowed():
    """projects 必须在白名单里 —— 正式版与草稿都存这个 scope。"""
    assert "projects" in ALLOWED_SCOPES
