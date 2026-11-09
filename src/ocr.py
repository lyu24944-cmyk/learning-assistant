"""选择本机识别引擎。

Windows 优先使用已经装在系统里的简体中文 Windows.Media.Ocr。
其他系统使用本机 Tesseract。可以用 LEARNING_ASSISTANT_OCR_PROVIDER=windows 或 tesseract 指定。
指定了却不可用时直接失败，不改走另一个引擎，也不伪造识别文字。
"""

from __future__ import annotations

import os
import sys

from PIL import Image

from src.ocr_tesseract import status as tesseract_status
from src.ocr_tesseract import transcribe as transcribe_tesseract
from src.ocr_types import OcrFailed, OcrUnavailable
from src.ocr_windows import describe_recognizer_languages
from src.ocr_windows import prepare_ocr_image
from src.ocr_windows import status as windows_status
from src.ocr_windows import transcribe as transcribe_windows

__all__ = [
    "OcrFailed",
    "OcrUnavailable",
    "describe_recognizer_languages",
    "engine_status",
    "prepare_ocr_image",
    "transcribe",
]


def engine_status() -> dict:
    requested = os.environ.get("LEARNING_ASSISTANT_OCR_PROVIDER", "").strip().lower()
    if requested not in {"", "windows", "tesseract"}:
        return {
            "available": False,
            "provider": requested,
            "engine": "",
            "languages": [],
            "english_independent": False,
            "detail": "LEARNING_ASSISTANT_OCR_PROVIDER 只能是 windows 或 tesseract。识别不可用。",
        }
    if requested == "windows" or (requested == "" and sys.platform == "win32"):
        return windows_status()
    return tesseract_status()


def transcribe(image: Image.Image, status: dict | None = None) -> str:
    chosen = engine_status() if status is None else status
    if not chosen.get("available"):
        raise OcrUnavailable(str(chosen.get("detail") or "识别不可用。"))
    if chosen.get("provider") == "windows":
        return transcribe_windows(image)
    if chosen.get("provider") == "tesseract":
        return transcribe_tesseract(image)
    raise OcrUnavailable("没有可用的本机识别引擎。")
