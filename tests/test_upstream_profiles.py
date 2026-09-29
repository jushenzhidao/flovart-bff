"""upstream_profiles 单测（baidu-wenxin 接入 T2 验收，规格书 §8）。

回归红线：开关 off / 未注册档案时 adapt() 必须**原样返回** params（同一对象）。
"""

import base64
import io

import pytest
from PIL import Image

from app import config, upstream_profiles as up


def _set_switch(monkeypatch, kind: str, value: str):
    monkeypatch.setitem(config.UPSTREAM_PROFILES, kind, value)


def _png_data_uri(w: int = 8, h: int = 8) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# 回归红线：开关 off → 原样返回
# ---------------------------------------------------------------------------
def test_switch_off_returns_params_identity(monkeypatch):
    params = {"model": "whatever", "image": {"data": "abc", "mimeType": "image/png"}, "prompt": "x"}
    assert up.adapt("remove-background", params) is params  # 同一对象
    assert up.adapt("dewatermark", params) is params        # 未开开关的新 type 同样


def test_unregistered_kind_returns_params_identity(monkeypatch):
    _set_switch(monkeypatch, "annotate", "baidu")
    params = {"variant": "annotate", "image": ["x"]}
    assert up.adapt("annotate", params) is params  # 无档案能力（annotate/relight/edit）不硬凑


# ---------------------------------------------------------------------------
# to_data_uri 归一化（§6.1）
# ---------------------------------------------------------------------------
def test_to_data_uri_dict_form():
    got = up.to_data_uri({"data": "QUJD", "mimeType": "image/webp"})
    assert got == "data:image/webp;base64,QUJD"


def test_to_data_uri_dict_data_already_data_uri():
    uri = "data:image/png;base64,QUJD"
    assert up.to_data_uri({"data": uri, "mimeType": "image/png"}) == uri


def test_to_data_uri_list_takes_first_non_empty():
    got = up.to_data_uri([{"data": "", "mimeType": "image/png"}, {"data": "QUJD", "mimeType": "image/png"}])
    assert got == "data:image/png;base64,QUJD"


def test_to_data_uri_bare_base64_gets_prefix():
    assert up.to_data_uri("QUJD") == "data:image/png;base64,QUJD"


def test_to_data_uri_url_and_datauri_passthrough():
    assert up.to_data_uri("https://e.com/a.png") == "https://e.com/a.png"
    assert up.to_data_uri("data:image/jpeg;base64,QQ") == "data:image/jpeg;base64,QQ"


def test_to_data_uri_unrecognized_returns_none():
    assert up.to_data_uri(None) is None
    assert up.to_data_uri("") is None
    assert up.to_data_uri({"nope": 1}) is None
    assert up.to_data_uri(123) is None


# ---------------------------------------------------------------------------
# adapt：缺必需字段 → 清晰异常（不静默发空值）
# ---------------------------------------------------------------------------
def test_missing_required_image_raises_clear_error(monkeypatch):
    _set_switch(monkeypatch, "remove-background", "baidu")
    with pytest.raises(ValueError, match="缺少输入 image"):
        up.adapt("remove-background", {"prompt": "x"})


def test_missing_required_mask_raises_clear_error(monkeypatch):
    _set_switch(monkeypatch, "erase", "baidu")
    with pytest.raises(ValueError, match="缺少输入 mask"):
        up.adapt("erase", {"image": _png_data_uri()})


def test_restyle_requires_explicit_style(monkeypatch):
    _set_switch(monkeypatch, "restyle", "baidu")
    with pytest.raises(ValueError, match="style"):
        up.adapt("restyle", {"image": _png_data_uri(), "prompt": "换风格"})


# ---------------------------------------------------------------------------
# adapt：开开关后的 body 形态（模型映射 + 白名单字段）
# ---------------------------------------------------------------------------
def test_remove_background_adapt_maps_model_and_normalizes(monkeypatch):
    _set_switch(monkeypatch, "remove-background", "baidu")
    params = {"model": "whatever-semantic", "image": {"data": "QUJD", "mimeType": "image/png"},
              "mode": "sync", "options": {"x": 1}}
    body = up.adapt("remove-background", params)
    assert body == {"model": "wenxin:matting", "image": "data:image/png;base64,QUJD"}
    # 白名单外字段（mode/options/原 model）不外发，避免上游拒收
    assert "mode" not in body and "options" not in body


def test_outpaint_direction_maps_to_size_ratio(monkeypatch):
    _set_switch(monkeypatch, "outpaint", "baidu")
    body = up.adapt("outpaint", {"image": _png_data_uri(), "direction": "left"})
    assert body["model"] == "wenxin:expand"
    assert body["size"] == "4:3"  # left/right → 4:3
    body2 = up.adapt("outpaint", {"image": _png_data_uri()})
    assert body2["size"] == "1:1"  # 兜底四周扩


def test_mask_adapt_includes_mask_and_prompt(monkeypatch):
    _set_switch(monkeypatch, "mask", "baidu")
    body = up.adapt("mask", {"image": _png_data_uri(), "mask": _png_data_uri(), "prompt": "把这里换成蓝天"})
    assert body["model"] == "wenxin:replace"
    assert body["mask"].startswith("data:image/")
    assert body["prompt"] == "把这里换成蓝天"


def test_restyle_adapt_maps_style(monkeypatch):
    _set_switch(monkeypatch, "restyle", "baidu")
    body = up.adapt("restyle", {"image": _png_data_uri(), "style": "miyazaki"})
    assert body == {"model": "wenxin:restyle", "image": body["image"], "style": "miyazaki"}


# ---------------------------------------------------------------------------
# shrink_for_legacy（§6.6）：640px/40KB 硬限
# ---------------------------------------------------------------------------
def _big_png_data_uri(w: int = 1024, h: int = 1024) -> str:
    buf = io.BytesIO()
    # 带噪点的图压缩率低，稳定超过 40KB
    img = Image.effect_noise((w, h), 64).convert("RGB")
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_shrink_for_legacy_hits_both_limits():
    big = _big_png_data_uri()
    assert len(big) > 80_000 // 4 * 3  # 原图 >80KB（base64 后更长）
    out = up.shrink_for_legacy(big)
    assert out.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(out.split(",", 1)[1])
    assert len(raw) <= 40_000, f"压缩后 {len(raw)}B 仍超 40KB"
    img = Image.open(io.BytesIO(raw))
    img.load()  # 没压坏，可正常解码
    assert max(img.size) <= 640


def test_shrink_for_legacy_small_image_passthrough_size():
    small = _png_data_uri(100, 60)
    out = up.shrink_for_legacy(small)
    img = Image.open(io.BytesIO(base64.b64decode(out.split(",", 1)[1])))
    assert max(img.size) <= 640  # 不放大


def test_shrink_for_legacy_rejects_non_datauri():
    with pytest.raises(ValueError):
        up.shrink_for_legacy("not-a-data-uri")


def test_shrink_for_legacy_url_passthrough():
    url = "https://e.com/a.png"  # URL 无法本地压缩，原样放行（上游自行拉取）
    assert up.shrink_for_legacy(url) == url


# ---------------------------------------------------------------------------
# legacy 能力自动压缩（dewatermark 等 640px/40KB）
# ---------------------------------------------------------------------------
def test_legacy_profile_compresses_large_input(monkeypatch):
    _set_switch(monkeypatch, "dewatermark", "baidu")
    big = _big_png_data_uri()
    body = up.adapt("dewatermark", {"image": big})
    assert body["model"] == "wenxin:dewatermark"
    raw = base64.b64decode(body["image"].split(",", 1)[1])
    assert len(raw) <= 40_000


def test_profile_table_covers_spec_abilities():
    """规格书 §4：7 个 A 组替换位（去掉 annotate/relight/edit）+ 12 个 B 组新增。"""
    expected = {
        "remove-background", "upscale", "outpaint", "mask",
        "dewatermark", "removetext", "erase", "beauty", "filter", "restore",
        "sketch", "textreplace", "bgreplace", "restyle", "redraw", "similar",
    }
    assert set(up.PROFILES) == expected
    # annotate / relight / edit 明确不建档案（无语义等价能力，硬凑结果不可用）
    assert up.profile_for("annotate") is None
    assert up.profile_for("relight") is None
    assert up.profile_for("edit") is None


def test_all_profiles_models_are_wenxin_and_legacy_flags_match_spec():
    spec_legacy = {"mask", "dewatermark", "erase", "bgreplace", "redraw", "similar"}
    for kind, p in up.PROFILES.items():
        assert p.model.startswith("wenxin:"), kind
        assert p.kind == kind
        assert (kind in spec_legacy) == p.legacy, kind


# ---------------------------------------------------------------------------
# 管理台覆盖层（2026-09-24：开关从 env-only 改为 运行时可配）
# ---------------------------------------------------------------------------

def test_override_wins_over_env(monkeypatch, tmp_path):
    monkeypatch.setattr(up.config, "DATA_DIR", str(tmp_path))
    assert up.is_enabled("dewatermark") is False  # env 无配置 -> off
    up.set_override("dewatermark", "baidu")
    assert up.is_enabled("dewatermark") is True   # 覆盖层生效
    monkeypatch.setitem(up.config.UPSTREAM_PROFILES, "dewatermark", "off")
    assert up.is_enabled("dewatermark") is True   # 覆盖层 > env
    up.set_override("dewatermark", "off")
    assert up.is_enabled("dewatermark") is False  # 覆盖层可关回


def test_set_override_rejects_bad_kind_and_value(tmp_path):
    mp = __import__("pytest").MonkeyPatch()
    mp.setattr(up.config, "DATA_DIR", str(tmp_path))
    try:
        for kwargs in ({"kind": "annotate", "value": "baidu"},
                       {"kind": "dewatermark", "value": "openai"}):
            try:
                up.set_override(**kwargs)
                assert False, f"should reject {kwargs}"
            except ValueError:
                pass
    finally:
        mp.undo()


def test_capability_views_shape(tmp_path):
    monkey = __import__("pytest")
    monkey.MonkeyPatch().setattr(up.config, "DATA_DIR", str(tmp_path))
    views = up.capability_views()
    assert len(views) == 16
    v = views[0]
    assert {"kind", "label", "model", "legacy", "requires", "enabled"} <= set(v)
    assert all(not x["enabled"] for x in views)  # 默认全 off
