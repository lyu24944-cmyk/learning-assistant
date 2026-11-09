"""本机 Tesseract。只在进程内读图，不上传。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageOps

from src.ocr_types import OcrFailed

ENGINE = "tesseract"


def status() -> dict:
    binary = _binary()
    if not binary:
        return _unavailable("本机找不到 Tesseract。识别不可用。图片不会发送到外部服务。可到「添加」页人工补录，那不是识别结果。")
    try:
        languages = sorted(_languages(binary))
    except OcrFailed as exc:
        return _unavailable(str(exc))
    if "chi_sim" not in languages:
        return _unavailable("Tesseract 已安装，但没有 chi_sim 中文语言包。识别不可用。")
    return {
        "available": True,
        "provider": "tesseract",
        "engine": ENGINE,
        "languages": languages,
        "english_independent": "eng" in languages,
        "detail": "使用本机 Tesseract（chi_sim+eng）。图片只在这台电脑上识别。",
    }


def transcribe(image: Image.Image) -> str:
    binary = _binary()
    if not binary:
        raise OcrFailed("本机找不到 Tesseract。识别不可用。")
    prepared = ImageOps.grayscale(image)
    if prepared.width < 1000:
        prepared = prepared.resize((prepared.width * 2, prepared.height * 2), Image.Resampling.LANCZOS)
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.png"
        prepared.save(page, format="PNG")
        try:
            completed = subprocess.run(
                [binary, str(page), "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                capture_output=True,
                check=False,
                timeout=30,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrFailed("识别超时，没有得到竞赛信息。") from exc
    if completed.returncode != 0:
        detail = _decode_output(completed.stderr).strip()[:200]
        raise OcrFailed(f"识别失败，没有得到竞赛信息。{detail}")
    try:
        text = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OcrFailed("识别输出不是 UTF-8，已拒绝，没有当成竞赛信息。") from exc
    return text.strip()


def _unavailable(detail: str) -> dict:
    return {
        "available": False,
        "provider": "tesseract",
        "engine": ENGINE,
        "languages": [],
        "english_independent": False,
        "detail": detail,
    }


def _binary() -> str | None:
    override = os.environ.get("LEARNING_ASSISTANT_TESSERACT", "").strip()
    if override:
        path = Path(override)
        return str(path) if path.is_file() else None
    return shutil.which("tesseract")


def _languages(binary: str) -> set[str]:
    try:
        completed = subprocess.run(
            [binary, "--list-langs"],
            capture_output=True,
            check=False,
            timeout=15,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise OcrFailed("读取 Tesseract 语言列表超时。识别不可用。") from exc
    if completed.returncode != 0:
        raise OcrFailed("读不到 Tesseract 语言列表。识别不可用。")
    try:
        text = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OcrFailed("Tesseract 语言列表不是 UTF-8。识别不可用。") from exc
    return {line.strip() for line in text.splitlines() if line.strip() and not line.startswith("List of")}


def _decode_output(data: bytes) -> str:
    if data.startswith(b"\xff\xfe") or (len(data) >= 4 and data[1] == 0 and data[3] == 0):
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8", errors="replace")
