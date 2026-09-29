"""服务端 dataURL MIME 嗅探兜底（2026-09-26）。

云同步重建的旧素材可能「JPEG 内容标 PNG 前缀」——上游按前缀解码，前缀错即失败。
submit() 入口统一修正，不依赖前端版本（前端 aiGateway 已有 normalizeDataUrlMime）。
"""

import base64


def _jpeg_data_url():
    raw = b"\xff\xd8\xff\xe0" + b"x" * 32
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def _png_data_url():
    raw = b"\x89PNG\r\n\x1a\n" + b"x" * 32
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def _webp_data_url():
    raw = b"RIFF\x00\x00\x00\x00WEBP" + b"x" * 16
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def test_jpeg_content_png_prefix_gets_fixed():
    from app.tasks import _fix_data_url_mime
    href = _jpeg_data_url()
    assert _fix_data_url_mime(href).startswith("data:image/jpeg;base64,")


def test_png_content_png_prefix_untouched():
    from app.tasks import _fix_data_url_mime
    href = _png_data_url()
    assert _fix_data_url_mime(href) == href


def test_webp_content_detected_via_riff():
    from app.tasks import _fix_data_url_mime
    assert _fix_data_url_mime(_webp_data_url()).startswith("data:image/webp;base64,")


def test_non_data_url_passthrough():
    from app.tasks import _fix_data_url_mime
    assert _fix_data_url_mime("https://example.com/a.png") == "https://example.com/a.png"
    assert _fix_data_url_mime("not-a-url") == "not-a-url"
    assert _fix_data_url_mime(None) is None


def test_garbage_base64_passthrough():
    from app.tasks import _fix_data_url_mime
    href = "data:image/png;base64,////"  # 解出的字节无任何已知魔数
    assert _fix_data_url_mime(href) == href


def test_params_image_list_and_mask_fixed():
    from app.tasks import _fix_params_image_mime
    params = {"image": ["https://keep.example/a.png", _jpeg_data_url()],
              "mask": _jpeg_data_url(), "prompt": "p"}
    fixed = _fix_params_image_mime(params)
    assert fixed["image"][0] == "https://keep.example/a.png"
    assert fixed["image"][1].startswith("data:image/jpeg;base64,")
    assert fixed["mask"].startswith("data:image/jpeg;base64,")
    # 原 params 不被就地修改
    assert params["mask"].startswith("data:image/png;base64,")
