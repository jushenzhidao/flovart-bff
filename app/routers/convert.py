"""图像格式转换（BFF）：当前只服务 PSD。

PSD 是封闭二进制格式（与 JPEG/PNG 不同），浏览器端无法原生生成。
用户在 Flovart 工具栏下载节点媒体时选 PSD → 前端走这个端点，
服务端 Pillow 把源图转成单图层 PSD 流回。

## 重要安全约束
- 仅允许 NEWAPI_BASE_URL 同主机源 URL（防 SSRF：禁止任意内网/外网拉取）。
- 源大小上限 30 MB（防 OOM 与无意义大图）。
- 不做 user 鉴权（new-api 媒体默认可公开匿名下载；如未来做权限则要在
  这里复制新-api PAT 一起带上）。
- 不做格式嗅探以外的二次信任（源是 new-api 自有域 + Pillow 自己解析，
  解码失败由 Pillow 抛出 → 415）。
"""
from io import BytesIO
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from PIL import Image, UnidentifiedImageError
from psd_tools import PSDImage

from .. import config

router = APIRouter()

MAX_SOURCE_BYTES = 30 * 1024 * 1024  # 30MB
ALLOWED_FORMATS = {"psd"}


def _is_allowed_source(url: str) -> bool:
    """源 URL 必须在 NEWAPI_BASE_URL 同主机（同 scheme + netloc）。"""
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return False
        if not u.netloc:
            return False
        base = urlparse(config.NEWAPI_BASE_URL)
        return (u.scheme == base.scheme) and (u.netloc == base.netloc)
    except Exception:
        return False


def _pil_to_psd_bytes(pil_img: Image.Image) -> bytes:
    """PIL Image → 单图层 PSD 字节流（psd-tools 写出，Pillow 只读不能写）。

    颜色模式选择：
    - 源图带 alpha 通道（PNG 抠图产物等） → PSDImage 新建 RGBA 模式，保留透明背景。
      否则用 RGB 模式会把透明区域填成黑色（飞哥之前遇到的「黑底 PSD」就是这个原因）。
    - 灰度（mode=L/LA）也走对应模式保持文件尺寸可控。
    """
    buf = BytesIO()
    # 决定 PSD 颜色模式：优先保留 alpha
    has_alpha = pil_img.mode in ("RGBA", "LA", "PA") or "A" in pil_img.getbands()
    if has_alpha and pil_img.mode != "RGBA":
        pil_img = pil_img.convert("RGBA")
        psd_mode = "RGBA"
    elif not has_alpha and pil_img.mode != "RGB":
        # L/P/1 等其他模式全部转 RGB（最通用）
        pil_img = pil_img.convert("RGB")
        psd_mode = "RGB"
    elif pil_img.mode == "P":
        # P（调色板）需要转 RGB 才不会丢色
        pil_img = pil_img.convert("RGB")
        psd_mode = "RGB"
    else:
        psd_mode = pil_img.mode  # RGBA / RGB 直用

    psd = PSDImage.new(mode=psd_mode, size=pil_img.size)
    psd.create_pixel_layer(pil_img, name="Layer 1")
    # encoding="utf-8"：PSD legacy pascal-string 通道默认 macroman，
    # 中文图层名会触发 UnicodeEncodeError；utf-8 可覆盖（Photoshop 走 unicode name 通道）。
    psd.save(buf, encoding="utf-8")
    return buf.getvalue()


def _pil_from_bytes(blob: bytes) -> Image.Image:
    """解码原始二进制：保留 alpha 通道，让 _pil_to_psd_bytes 决定写 RGB/RGBA。
    1 位、调色板模式先转 RGBA（统一入口），其他模式原样返回。
    """
    pil_img = Image.open(BytesIO(blob))
    pil_img.load()
    if pil_img.mode == "1":
        pil_img = pil_img.convert("L")
    elif pil_img.mode == "P":
        # 调色板：先看是否带透明 → 决定 RGBA 还是 RGB
        pil_img = pil_img.convert("RGBA" if "A" in pil_img.getbands() else "RGB")
    elif pil_img.mode == "PA":
        pil_img = pil_img.convert("RGBA")
    return pil_img


def _make_response(psd_bytes: bytes, filename: str):
    return StreamingResponse(
        iter([psd_bytes]),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(psd_bytes)),
            "Cache-Control": "no-store",
        },
    )


@router.get("/api/convert/image")
async def convert_image(
    url: str = Query(..., description="源图像绝对 URL（必须与 NEWAPI_BASE_URL 同主机）"),
    format: str = Query("psd", description="目标格式：v1 只支持 psd"),
):
    """服务端拉 URL 转换。适用：源是 new-api 公共直链（媒体托管 URL）的场景。

    浏览器本地 blob URL 走 POST /api/convert/image，httpx 拉不了 blob://。
    """
    fmt = format.lower()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"暂不支持 format={fmt}")
    if not _is_allowed_source(url):
        raise HTTPException(status_code=400, detail="源 URL 不在允许的主机内")

    # 拉取源（独立 client，避免复用 newapi_client 单例的 base_url 干扰）
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, headers={"User-Agent": "flovart-bff/convert"})
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"下载源失败: {type(e).__name__}") from e

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"源返回 {resp.status_code}")
    blob = resp.content
    if len(blob) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail=f"源超过 {MAX_SOURCE_BYTES // (1024 * 1024)} MB 限制")

    try:
        pil_img = _pil_from_bytes(blob)
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise HTTPException(status_code=415, detail=f"图像解码失败: {type(e).__name__}") from e

    try:
        psd_bytes = _pil_to_psd_bytes(pil_img)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PSD 写出失败: {type(e).__name__}") from e

    # 从源 URL 末尾推断一个简单文件名；否则随机
    filename = "image.psd"
    try:
        path = urlparse(url).path.rsplit("/", 1)[-1]
        if path and "." in path:
            stem = path.rsplit(".", 1)[0][:32] or "image"
            filename = f"{stem}.psd"
    except Exception:
        pass

    return _make_response(psd_bytes, filename)


@router.post("/api/convert/psd-layers")
async def convert_psd_layers(
    files: list[UploadFile] = File(..., description="各图层图片（PNG/JPG/WebP，按叠放顺序传，先传的在底部）"),
    meta: str = Form("[]", description="每层 JSON 元数据，下标与 files 对齐：[{\"name\":\"图层 1\",\"offsetX\":0,\"offsetY\":0}]"),
    background: str = Form("white", description="画布底色：white 或 transparent"),
):
    """把多张图层图片叠成一个多图层 PSD（Photoshop 可编辑，每层可显隐/拖动）。

    场景：Flovart「拆分图层」把一张图拆成 N 个独立 PNG（带 offset 与图层名），
    前端把 N 张 PNG + offset 传进来 → 这里用 psd-tools 叠成单个多图层 PSD 流回。
    """
    import json as _json

    try:
        metas = _json.loads(meta or "[]")
        if not isinstance(metas, list):
            raise ValueError("meta 需为 JSON 数组")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"meta 格式错误: {e}") from e

    # 逐张解码并计算画布尺寸（各层 offset + 尺寸的并集）
    layers: list[tuple[Image.Image, int, int, str]] = []
    canvas_w, canvas_h = 1, 1
    for index, file in enumerate(files):
        blob = await file.read()
        if not blob:
            raise HTTPException(status_code=400, detail=f"第 {index + 1} 个文件为空")
        if len(blob) > MAX_SOURCE_BYTES:
            raise HTTPException(status_code=413, detail=f"第 {index + 1} 个文件超过 {MAX_SOURCE_BYTES // (1024 * 1024)} MB 限制")
        try:
            img = _pil_from_bytes(blob)
        except (UnidentifiedImageError, OSError, ValueError) as e:
            raise HTTPException(status_code=415, detail=f"第 {index + 1} 个图像解码失败: {type(e).__name__}") from e

        m = metas[index] if index < len(metas) and isinstance(metas[index], dict) else {}
        ox = int(m.get("offsetX") if m.get("offsetX") is not None else (m.get("x") or 0))
        oy = int(m.get("offsetY") if m.get("offsetY") is not None else (m.get("y") or 0))
        name = str(m.get("name") or file.filename or f"图层 {index + 1}")[:60] or f"图层 {index + 1}"
        canvas_w = max(canvas_w, ox + img.width)
        canvas_h = max(canvas_h, oy + img.height)
        layers.append((img, ox, oy, name))

    if not layers:
        raise HTTPException(status_code=400, detail="没有可用的图层文件")

    transparent = (background or "white").strip().lower() == "transparent"

    try:
        if transparent:
            psd = PSDImage.new(mode="RGBA", size=(canvas_w, canvas_h), color=(0, 0, 0, 0))
        else:
            psd = PSDImage.new(mode="RGB", size=(canvas_w, canvas_h), color=(255, 255, 255))
            # 白底背景层放最底部（先 append 的在底层），其余图层叠加在上
            white_bg = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
            bg_layer = psd.create_pixel_layer(white_bg, name="背景")
            bg_layer.name = "背景"  # 触发 name setter → 写入 UNICODE_LAYER_NAME（否则中文名在 PS 里乱码）
        for img, ox, oy, name in layers:
            layer = psd.create_pixel_layer(img, name=name, left=ox, top=oy)
            layer.name = name  # 同上：确保 unicode 名称通道写入
        buf = BytesIO()
        psd.save(buf, encoding="utf-8")
        data = buf.getvalue()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"多图层 PSD 写出失败: {type(e).__name__}: {e}") from e

    return _make_response(data, "layers.psd")


@router.post("/api/convert/image")
async def convert_image_upload(
    file: UploadFile = File(..., description="源图像二进制（PNG/JPG/SVG/WebP …）"),
    format: str = Form("psd", description="目标格式：v1 只支持 psd"),
):
    """浏览器直接上传源文件二进制。适用：源是浏览器本地 blob URL
    （Flovart 创作站从 IndexedDB 喂出来的图）的场景——服务端 httpx 无法
    解析 blob://，前端先把 blob 读成 ArrayBuffer 走 multipart/form-data 给这里。
    """
    fmt = format.lower()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail=f"暂不支持 format={fmt}")

    blob = await file.read()
    if not blob:
        raise HTTPException(status_code=400, detail="空文件")
    if len(blob) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail=f"文件超过 {MAX_SOURCE_BYTES // (1024 * 1024)} MB 限制")

    try:
        pil_img = _pil_from_bytes(blob)
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise HTTPException(status_code=415, detail=f"图像解码失败: {type(e).__name__}") from e

    try:
        psd_bytes = _pil_to_psd_bytes(pil_img)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PSD 写出失败: {type(e).__name__}") from e

    # 文件名：优先用上传文件名（去扩展名） + .psd；否则 image.psd
    filename = "image.psd"
    if file.filename:
        try:
            stem = file.filename.rsplit(".", 1)[0][:32] or "image"
            filename = f"{stem}.psd"
        except Exception:
            pass

    return _make_response(psd_bytes, filename)


# ---- SVG 矢量转换（visioncortex VTracer） ----
#
# 与 PSD 端点一样走 multipart 上传（适配浏览器本地 blob URL）。把栅格图
# 曲线描摹成 <path> 矢量 SVG。适合 Logo/图标/线条素材；照片级写实图会产出
# 大量路径、文件大、且不可像 AI/Illustrator 那样有意义地编辑。

ALLOWED_SVG_SRC_FORMATS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}


def _infer_svg_src_format(filename: str | None, content_type: str | None) -> str:
    """从上传文件名后缀或 Content-Type 推断 VTracer 需要的 img_format。"""
    if filename:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in ALLOWED_SVG_SRC_FORMATS:
            return "jpg" if ext == "jpeg" else ext
    if content_type:
        ct = content_type.lower()
        if "png" in ct:
            return "png"
        if "jpeg" in ct or "jpg" in ct:
            return "jpg"
        if "gif" in ct:
            return "gif"
        if "bmp" in ct:
            return "bmp"
        if "webp" in ct:
            return "webp"
    return "png"  # 兜底


@router.post("/api/convert/svg")
async def convert_svg(
    file: UploadFile = File(..., description="源栅格图（PNG/JPG/GIF/BMP/WebP）"),
    mode: str = Form("spline"),
    colormode: str = Form("color"),
    filter_speckle: int = Form(4),
    color_precision: int = Form(6),
    layer_difference: int = Form(16),
    corner_threshold: int = Form(60),
    length_threshold: float = Form(4.0),
    max_iterations: int = Form(10),
    splice_threshold: int = Form(45),
    path_precision: int = Form(8),
):
    """栅格图 → 真矢量 SVG（VTracer 曲线描摹）。

    同源 PSD 端点：浏览器把本地 blob 读成二进制 multipart 上传。VTracer 在
    服务端把像素拟合成 SVG <path>。照片级写实图建议谨慎使用（路径爆炸、文件大）。
    """
    blob = await file.read()
    if not blob:
        raise HTTPException(status_code=400, detail="空文件")
    if len(blob) > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=413, detail=f"文件超过 {MAX_SOURCE_BYTES // (1024 * 1024)} MB 限制")

    # webp 走 Pillow 转 PNG（VTracer C 层对 webp 不稳，转一道更稳）
    fmt = _infer_svg_src_format(file.filename, file.content_type)
    if fmt == "webp":
        try:
            img = _pil_from_bytes(blob).convert("RGB")
            buf = BytesIO()
            img.save(buf, "PNG")
            blob = buf.getvalue()
            fmt = "png"
        except (UnidentifiedImageError, OSError, ValueError) as e:
            raise HTTPException(status_code=415, detail=f"WebP 解码失败: {type(e).__name__}") from e
    if fmt not in ALLOWED_SVG_SRC_FORMATS:
        raise HTTPException(status_code=400, detail=f"不支持的源格式: {fmt}（仅 PNG/JPG/GIF/BMP/WebP）")

    mode = mode if mode in ("spline", "polygon", "pixel") else "spline"
    colormode = colormode if colormode in ("color", "binary") else "color"

    try:
        import vtracer

        svg = vtracer.convert_raw_image_to_svg(
            blob,
            img_format=fmt,
            colormode=colormode,
            hierarchical="stacked",
            mode=mode,
            filter_speckle=filter_speckle,
            color_precision=color_precision,
            layer_difference=layer_difference,
            corner_threshold=corner_threshold,
            length_threshold=length_threshold,
            max_iterations=max_iterations,
            splice_threshold=splice_threshold,
            path_precision=path_precision,
        )
    except Exception as e:
        raise HTTPException(status_code=415, detail=f"矢量化失败: {type(e).__name__}: {e}") from e

    if not isinstance(svg, str) or "<svg" not in svg:
        raise HTTPException(status_code=500, detail="矢量化返回内容异常")

    filename = "image.svg"
    if file.filename:
        try:
            stem = file.filename.rsplit(".", 1)[0][:32] or "image"
            filename = f"{stem}.svg"
        except Exception:
            pass

    return Response(
        svg,
        media_type="image/svg+xml",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )