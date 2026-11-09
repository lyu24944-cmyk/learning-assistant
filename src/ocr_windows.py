"""Windows.Media.Ocr。只用系统里已经装好的简体中文识别语言。

不下载引擎，不运行安装包，不改执行策略、证书或时区。
图片路径通过环境变量交给 powershell.exe -Command，不使用 ExecutionPolicy Bypass。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

from src.ocr_types import OcrFailed, OcrUnavailable

ENGINE = "Windows.Media.Ocr"
PROVIDER = "windows"
MAX_DIMENSION = 10000

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapTransform, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapPixelFormat, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapAlphaMode, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.ExifOrientationMode, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.ColorManagementMode, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapInterpolationMode, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Media.Ocr, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
function Await-Op($op, $type) {
  $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
  } | Select-Object -First 1
  $task = $method.MakeGenericMethod($type).Invoke($null, @($op))
  if (-not $task.Wait(25000)) { throw 'OCR_TIMEOUT' }
  if ($task.IsFaulted) { throw $task.Exception.InnerException }
  return $task.Result
}
function Read-PositiveInt($object, $name) {
  $raws = @()
  try { $raws += $object.$name } catch {}
  try {
    $prop = $object.GetType().GetProperty($name)
    if ($null -ne $prop) { $raws += $prop.GetValue($object, $null) }
  } catch {}
  foreach ($raw in $raws) {
    if ($null -eq $raw) { continue }
    $parsed = 0L
    if ([int64]::TryParse(([string]$raw).Trim(), [ref]$parsed) -and $parsed -gt 0) { return $parsed }
  }
  return $null
}
function Json-Num($value) { if ($null -eq $value) { return 'null' }; return [string]$value }
$tags = @([Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages | ForEach-Object { $_.LanguageTag })
$utf8 = New-Object System.Text.UTF8Encoding $false
if ($env:LEARNING_ASSISTANT_OCR_MODE -eq 'status') {
  [System.IO.File]::WriteAllLines($env:LEARNING_ASSISTANT_OCR_OUT, $tags, $utf8)
  exit 0
}
if ($tags -notcontains 'zh-Hans-CN') { throw 'NO_ZH_HANS' }
$language = New-Object Windows.Globalization.Language 'zh-Hans-CN'
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { throw 'NO_ZH_HANS' }
$max = Read-PositiveInt $engine 'MaxImageDimension'
$file = Await-Op ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:LEARNING_ASSISTANT_OCR_IMAGE)) ([Windows.Storage.StorageFile])
$stream = Await-Op ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$bitmap = $null
try {
  $decoder = Await-Op ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $width = Read-PositiveInt $decoder 'OrientedPixelWidth'
  if ($null -eq $width) { $width = Read-PositiveInt $decoder 'PixelWidth' }
  $height = Read-PositiveInt $decoder 'OrientedPixelHeight'
  if ($null -eq $height) { $height = Read-PositiveInt $decoder 'PixelHeight' }
  $scale = ($null -ne $width) -and ($null -ne $height) -and ($null -ne $max) -and (($width -gt $max) -or ($height -gt $max))
  $diag = '{"width":' + (Json-Num $width) + ',"height":' + (Json-Num $height) + ',"max":' + (Json-Num $max) + ',"scaled":' + ($(if ($scale) { 'true' } else { 'false' })) + '}'
  if ($env:LEARNING_ASSISTANT_OCR_DIAG) { [System.IO.File]::WriteAllText($env:LEARNING_ASSISTANT_OCR_DIAG, $diag, $utf8) }
  if ($scale) {
    $ratio = [double]$max / [Math]::Max($width, $height)
    $transform = New-Object Windows.Graphics.Imaging.BitmapTransform
    $transform.ScaledWidth = [uint32]([Math]::Max(1, [Math]::Floor($width * $ratio)))
    $transform.ScaledHeight = [uint32]([Math]::Max(1, [Math]::Floor($height * $ratio)))
    $transform.InterpolationMode = [Windows.Graphics.Imaging.BitmapInterpolationMode]::Fant
    $bitmap = Await-Op ($decoder.GetSoftwareBitmapAsync([Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied, $transform, [Windows.Graphics.Imaging.ExifOrientationMode]::IgnoreExifOrientation, [Windows.Graphics.Imaging.ColorManagementMode]::DoNotColorManage)) ([Windows.Graphics.Imaging.SoftwareBitmap])
  } else {
    $bitmap = Await-Op ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $bitmap = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert($bitmap, [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8, [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied)
  }
} finally {
  $stream.Dispose()
}
try {
  $result = Await-Op ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $lines = New-Object System.Collections.Generic.List[string]
  foreach ($line in $result.Lines) { [void]$lines.Add($line.Text) }
  [System.IO.File]::WriteAllLines($env:LEARNING_ASSISTANT_OCR_OUT, $lines, $utf8)
} finally {
  if ($null -ne $bitmap) { $bitmap.Dispose() }
}
"""


def describe_recognizer_languages(tags: list[str]) -> dict:
    languages = [tag.strip() for tag in tags if tag and tag.strip()]
    simplified = [tag for tag in languages if tag.lower().startswith("zh-hans")]
    english = [tag for tag in languages if tag.lower().startswith("en")]
    if not simplified:
        return {
            "available": False,
            "provider": PROVIDER,
            "engine": ENGINE,
            "languages": languages,
            "english_independent": bool(english),
            "detail": "系统 OcrEngine 没有简体中文语言（需要 zh-Hans-CN）。识别不可用。没有改用外部服务。",
        }
    if english:
        english_note = "已安装独立的英语识别语言。"
    else:
        english_note = "英语不是已安装的识别语言，不保证中英混排或网址能被读出。"
    return {
        "available": True,
        "provider": PROVIDER,
        "engine": ENGINE,
        "languages": languages,
        "english_independent": bool(english),
        "detail": f"使用本机 Windows.Media.Ocr，已装语言：{'、'.join(languages)}。{english_note}图片只在这台电脑上识别。",
    }


def status() -> dict:
    if sys.platform != "win32":
        return {
            "available": False,
            "provider": PROVIDER,
            "engine": ENGINE,
            "languages": [],
            "english_independent": False,
            "detail": "当前系统不是 Windows，不能使用 Windows.Media.Ocr。没有改用外部服务，也没有伪造识别结果。",
        }
    if _powershell() is None:
        return {
            "available": False,
            "provider": PROVIDER,
            "engine": ENGINE,
            "languages": [],
            "english_independent": False,
            "detail": "找不到 powershell.exe，无法调用系统 OcrEngine。识别不可用。没有改执行策略。",
        }
    try:
        tags = _run(mode="status", image=None)
    except OcrUnavailable as exc:
        return {
            "available": False,
            "provider": PROVIDER,
            "engine": ENGINE,
            "languages": [],
            "english_independent": False,
            "detail": str(exc),
        }
    except OcrFailed as exc:
        return {
            "available": False,
            "provider": PROVIDER,
            "engine": ENGINE,
            "languages": [],
            "english_independent": False,
            "detail": str(exc),
        }
    return describe_recognizer_languages(tags)


def transcribe(image: Image.Image) -> str:
    if sys.platform != "win32":
        raise OcrUnavailable("当前系统不是 Windows，不能使用 Windows.Media.Ocr。")
    prepared = prepare_ocr_image(image, MAX_DIMENSION)
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.png"
        prepared.save(page, format="PNG")
        text = _run(mode="recognize", image=page)
    if isinstance(text, list):
        return "\n".join(text).strip()
    return str(text).strip()


def coerce_positive_int(value: object) -> int | None:
    """Parse a WinRT dimension. Text order is not a size comparison."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text.isdecimal():
            return None
        number = int(text)
        return number if number > 0 else None
    return None


def dimension_exceeds(width: object, height: object, limit: object) -> bool:
    """True only when both sides and the engine limit are positive integers.

    ``"1700" -gt "10000"`` is true as text and was blocking a 1700x1200 poster.
    A missing limit is not treated as too large.
    """
    width_n = coerce_positive_int(width)
    height_n = coerce_positive_int(height)
    limit_n = coerce_positive_int(limit)
    if width_n is None or height_n is None or limit_n is None:
        return False
    return width_n > limit_n or height_n > limit_n


def prepare_ocr_image(image: Image.Image, max_dimension: int) -> Image.Image:
    """只缩放交给引擎的副本。调用方仍保存原来的上传字节。"""
    prepared = image if image.mode in {"RGB", "L"} else image.convert("RGB")
    if prepared.width < 1000:
        grown = prepared.resize((prepared.width * 2, prepared.height * 2), Image.Resampling.LANCZOS)
        if max(grown.size) <= max_dimension:
            prepared = grown
    longest = max(prepared.size)
    if longest > max_dimension:
        scale = max_dimension / longest
        prepared = prepared.resize(
            (max(1, int(prepared.width * scale)), max(1, int(prepared.height * scale))),
            Image.Resampling.LANCZOS,
        )
    return prepared


def _run(mode: str, image: Path | None) -> list[str]:
    binary = _powershell()
    if binary is None:
        raise OcrUnavailable("找不到 powershell.exe，无法调用系统 OcrEngine。")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.txt"
        env = os.environ.copy()
        env["LEARNING_ASSISTANT_OCR_MODE"] = mode
        env["LEARNING_ASSISTANT_OCR_OUT"] = str(out)
        env["LEARNING_ASSISTANT_OCR_DIAG"] = str(out.with_suffix(".json"))
        if image is not None:
            env["LEARNING_ASSISTANT_OCR_IMAGE"] = str(image)
        try:
            completed = subprocess.run(
                [binary, "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
                capture_output=True,
                check=False,
                timeout=30,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrFailed("识别超时，没有得到竞赛信息。") from exc
        diag = _read_diag(env.get("LEARNING_ASSISTANT_OCR_DIAG", ""))
        if completed.returncode != 0 or not out.is_file():
            detail = _decode_output(completed.stderr + completed.stdout).strip()[:300]
            if diag:
                detail = f"{detail} diag={diag}"
            if "NO_ZH_HANS" in detail:
                raise OcrUnavailable("系统 OcrEngine 没有简体中文语言（需要 zh-Hans-CN）。识别不可用。")
            raise OcrFailed(f"Windows.Media.Ocr 没有返回文字。{detail}")
        try:
            raw = out.read_bytes()
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OcrFailed("识别输出不是 UTF-8，已拒绝，没有当成竞赛信息。") from exc
    return [line.strip() for line in text.splitlines() if line.strip()]


def _powershell() -> str | None:
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return str(candidate)
    return shutil.which("powershell")


def _read_diag(path: str) -> str:
    if not path:
        return ""
    file = Path(path)
    if not file.is_file():
        return ""
    try:
        return file.read_text(encoding="utf-8").strip()[:500]
    except UnicodeDecodeError:
        return file.read_bytes()[:200].decode("utf-8", errors="backslashreplace")


def _decode_output(data: bytes) -> str:
    if not data:
        return ""
    if data.startswith(b"\xff\xfe") or (len(data) >= 4 and data[1] == 0 and data[3] == 0):
        return data.decode("utf-16-le", errors="backslashreplace")
    return data.decode("utf-8", errors="backslashreplace")
