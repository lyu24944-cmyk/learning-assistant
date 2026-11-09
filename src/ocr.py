"""选择本机识别引擎。

Windows 优先使用已经装在系统里的简体中文 Windows.Media.Ocr。
其他系统使用本机 Tesseract。可以用 LEARNING_ASSISTANT_OCR_PROVIDER=windows 或 tesseract 指定。
指定了却不可用时直接失败，不改走另一个引擎，也不伪造识别文字。

`engine_status()` 是原始查询，每次都会起一次 PowerShell（Windows）或 tesseract（其他系统）。
`engine_status_cached()` 是给请求路径用的 300 秒缓存：问「系统装没装中文识别包」
这件事一分钟内不会变，但每次上传都白起一次进程太浪费。
"""

from __future__ import annotations

import copy
import os
import sys
import threading
import time

from PIL import Image

from src.ocr_tesseract import status as tesseract_status
from src.ocr_tesseract import transcribe as transcribe_tesseract
from src.ocr_types import OcrFailed, OcrUnavailable
from src.ocr_windows import describe_recognizer_languages, prepare_ocr_image
from src.ocr_windows import status as windows_status
from src.ocr_windows import transcribe as transcribe_windows

__all__ = [
    "OcrFailed",
    "OcrUnavailable",
    "clear_status_cache",
    "describe_recognizer_languages",
    "engine_status",
    "engine_status_cached",
    "prepare_ocr_image",
    "status_ttl",
    "transcribe",
]

DEFAULT_STATUS_TTL = 300.0

_cache_lock = threading.Lock()
# key 是 LEARNING_ASSISTANT_OCR_PROVIDER 的归一化值；值是一份 (查询时刻, 结果) 的副本。
# 缓存必须按 provider 分开：同一个进程里切换引擎时，读到上一个引擎的答案会误导调用方。
_cache: dict[str, tuple[float, dict]] = {}


def _requested_provider() -> str:
    return os.environ.get("LEARNING_ASSISTANT_OCR_PROVIDER", "").strip().lower()


def engine_status() -> dict:
    requested = _requested_provider()
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


def status_ttl() -> float:
    """缓存秒数。设为 0 表示关闭缓存；解析不了就用默认值。"""
    raw = os.environ.get("LEARNING_ASSISTANT_OCR_STATUS_TTL", "").strip()
    if not raw:
        return DEFAULT_STATUS_TTL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_STATUS_TTL


def engine_status_cached() -> dict:
    """带缓存的引擎状态。

    失败结果也缓存。系统没装中文识别包时，每次上传都去等一次 PowerShell 超时，
    那才是最坏情况——缓存住这个「不可用」的结论反而更重要。

    返回深拷贝：调用方改 `languages` 不会污染缓存。
    """
    ttl = status_ttl()
    if ttl <= 0:
        return engine_status()
    key = _requested_provider()
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return copy.deepcopy(hit[1])
    status = engine_status()
    with _cache_lock:
        _cache[key] = (now, copy.deepcopy(status))
    return status


def clear_status_cache() -> None:
    with _cache_lock:
        _cache.clear()


def transcribe(image: Image.Image, status: dict | None = None) -> str:
    chosen = engine_status() if status is None else status
    if not chosen.get("available"):
        raise OcrUnavailable(str(chosen.get("detail") or "识别不可用。"))
    if chosen.get("provider") == "windows":
        return transcribe_windows(image)
    if chosen.get("provider") == "tesseract":
        return transcribe_tesseract(image)
    raise OcrUnavailable("没有可用的本机识别引擎。")
