"""校验上传的位图，并放到用户自己的私有目录。

文件名和路径都不可信。只接受实际能解码的 PNG、JPEG、GIF、WEBP、BMP。
原图不进入仓库，也不通过公共静态目录提供。
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from src.config import ROOT

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 8_000_000
MAX_SIDE = 6000
_FORMATS = {
    "JPEG": (".jpg", "image/jpeg"),
    "PNG": (".png", "image/png"),
    "GIF": (".gif", "image/gif"),
    "WEBP": (".webp", "image/webp"),
    "BMP": (".bmp", "image/bmp"),
}


def private_root() -> Path:
    override = os.environ.get("LEARNING_ASSISTANT_PRIVATE", "").strip()
    if override:
        return Path(override)
    return ROOT / "data" / "private"


def inspect_bitmap(data: bytes) -> tuple[Image.Image, str, str, str]:
    if len(data) > MAX_BYTES:
        raise ValueError("图片超过 8MB。")
    if len(data) < 24:
        raise ValueError("文件太小，不是可用的位图。")
    if data.startswith((b"<", b"%PDF", b"PK")) or b"<svg" in data[:300].lower():
        raise ValueError("只接受 PNG、JPEG、GIF、WEBP、BMP 静止位图。")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        probe = Image.open(io.BytesIO(data))
        image_format = probe.format
        frames = getattr(probe, "n_frames", 1)
        probe.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        raise ValueError("无法按位图解码，已拒绝。") from exc
    if image_format not in _FORMATS or frames > 1:
        raise ValueError("只接受 PNG、JPEG、GIF、WEBP、BMP 静止位图。")
    width, height = image.size
    if width < 8 or height < 8 or width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
        raise ValueError("图片尺寸超出限制。")
    extension, mime = _FORMATS[image_format]
    return image, extension, mime, hashlib.sha256(data).hexdigest()


def preview_bytes(image: Image.Image) -> bytes:
    derived = image.convert("RGB")
    derived.thumbnail((1280, 1280))
    buffer = io.BytesIO()
    derived.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def allocate_key() -> str:
    return uuid.uuid4().hex


def write_private(user_id: int, storage_key: str, extension: str, original: bytes, preview: bytes) -> tuple[str, str]:
    if not storage_key.isalnum():
        raise ValueError("存储名无效。")
    root = private_root().resolve()
    folder = (root / str(int(user_id)) / storage_key).resolve()
    folder.relative_to(root)
    folder.mkdir(parents=True, exist_ok=False)
    original_rel = f"{int(user_id)}/{storage_key}/original{extension}"
    preview_rel = f"{int(user_id)}/{storage_key}/preview.jpg"
    (root / original_rel).write_bytes(original)
    (root / preview_rel).write_bytes(preview)
    return original_rel, preview_rel


def private_file(relative: str) -> Path:
    if not relative or relative.startswith(("/", "\\")) or ".." in Path(relative).parts:
        raise FileNotFoundError(relative)
    root = private_root().resolve()
    path = (root / relative).resolve()
    path.relative_to(root)
    if not path.is_file():
        raise FileNotFoundError(relative)
    return path
