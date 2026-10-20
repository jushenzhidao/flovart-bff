"""convert 路由 PDF/PPTX 扩展回归测试（P0-3，2026-10-19）。

直接测 _convert_to_bytes 分发（不起 HTTP）：psd 原有行为不变，
pdf 用 Pillow 原生、pptx 用 python-pptx（单页全幅、尺寸=图片尺寸）。
"""
from io import BytesIO

from PIL import Image
from pptx import Presentation

from app.routers.convert import _convert_to_bytes


def _rgb_img() -> Image.Image:
    return Image.new("RGB", (640, 400), (240, 128, 32))


def test_convert_psd_still_works():
    data = _convert_to_bytes(_rgb_img(), "psd")
    assert data[:4] == b"8BPS"  # PSD magic


def test_convert_pdf_produces_pdf():
    data = _convert_to_bytes(_rgb_img(), "pdf")
    assert data[:5] == b"%PDF-"


def test_convert_pptx_sized_to_image():
    data = _convert_to_bytes(_rgb_img(), "pptx")
    prs = Presentation(BytesIO(data))
    assert prs.slide_width == 640 * 9525
    assert prs.slide_height == 400 * 9525
    # 单页，页上有 1 个 picture
    slide = prs.slides[0]
    assert len(slide.shapes) == 1
    assert slide.shapes[0].shape_type == 13  # PICTURE


def test_convert_unknown_format_raises():
    import pytest

    with pytest.raises(ValueError):
        _convert_to_bytes(_rgb_img(), "docx")


def test_convert_rgba_source_for_pptx():
    img = Image.new("RGBA", (300, 200), (0, 128, 255, 128))
    data = _convert_to_bytes(img, "pptx")
    assert len(data) > 500
