"""引擎状态缓存：命中、失败也缓存、TTL、provider 隔离、关闭开关。"""

from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from src import ocr

AVAILABLE = {
    "available": True,
    "provider": "windows",
    "engine": "Windows.Media.Ocr",
    "languages": ["zh-Hans-CN"],
    "english_independent": True,
    "detail": "可用",
}

UNAVAILABLE = {
    "available": False,
    "provider": "windows",
    "engine": "Windows.Media.Ocr",
    "languages": [],
    "english_independent": False,
    "detail": "系统 OcrEngine 没有简体中文语言（需要 zh-Hans-CN）。识别不可用。",
}


class EngineStatusCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_provider = os.environ.get("LEARNING_ASSISTANT_OCR_PROVIDER")
        self.previous_ttl = os.environ.get("LEARNING_ASSISTANT_OCR_STATUS_TTL")
        os.environ["LEARNING_ASSISTANT_OCR_PROVIDER"] = "windows"
        os.environ.pop("LEARNING_ASSISTANT_OCR_STATUS_TTL", None)
        ocr.clear_status_cache()

    def tearDown(self) -> None:
        for name, value in (
            ("LEARNING_ASSISTANT_OCR_PROVIDER", self.previous_provider),
            ("LEARNING_ASSISTANT_OCR_STATUS_TTL", self.previous_ttl),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        ocr.clear_status_cache()

    def test_second_call_reuses_cached_status(self) -> None:
        with patch.object(ocr, "windows_status", return_value=dict(AVAILABLE)) as status:
            first = ocr.engine_status_cached()
            second = ocr.engine_status_cached()
        self.assertEqual(status.call_count, 1)
        self.assertEqual(first, second)
        self.assertTrue(first["available"])

    def test_unavailable_status_is_cached_too(self) -> None:
        """没装中文包时缓存「不可用」，避免每次上传都等一次 PowerShell 超时。"""
        with patch.object(ocr, "windows_status", return_value=dict(UNAVAILABLE)) as status:
            ocr.engine_status_cached()
            again = ocr.engine_status_cached()
        self.assertEqual(status.call_count, 1)
        self.assertFalse(again["available"])

    def test_ttl_expiry_refetches(self) -> None:
        os.environ["LEARNING_ASSISTANT_OCR_STATUS_TTL"] = "0.01"
        with patch.object(ocr, "windows_status", return_value=dict(AVAILABLE)) as status:
            ocr.engine_status_cached()
            time.sleep(0.03)
            ocr.engine_status_cached()
        self.assertEqual(status.call_count, 2)

    def test_provider_switch_does_not_share_cache(self) -> None:
        tesseract = dict(AVAILABLE, provider="tesseract", engine="tesseract")
        with patch.object(ocr, "windows_status", return_value=dict(AVAILABLE)) as windows:
            ocr.engine_status_cached()
            os.environ["LEARNING_ASSISTANT_OCR_PROVIDER"] = "tesseract"
            with patch.object(ocr, "tesseract_status", return_value=tesseract) as other:
                result = ocr.engine_status_cached()
        self.assertEqual(windows.call_count, 1)
        self.assertEqual(other.call_count, 1)
        self.assertEqual(result["provider"], "tesseract")

    def test_zero_ttl_disables_cache(self) -> None:
        os.environ["LEARNING_ASSISTANT_OCR_STATUS_TTL"] = "0"
        with patch.object(ocr, "windows_status", return_value=dict(AVAILABLE)) as status:
            ocr.engine_status_cached()
            ocr.engine_status_cached()
        self.assertEqual(status.call_count, 2)

    def test_callers_cannot_mutate_the_cached_value(self) -> None:
        with patch.object(ocr, "windows_status", return_value=dict(AVAILABLE)):
            first = ocr.engine_status_cached()
            first["languages"].append("en-US")
            first["available"] = False
            second = ocr.engine_status_cached()
        self.assertEqual(second["languages"], ["zh-Hans-CN"])
        self.assertTrue(second["available"])

    def test_invalid_provider_is_cached_and_reported(self) -> None:
        os.environ["LEARNING_ASSISTANT_OCR_PROVIDER"] = "paddle"
        with patch.object(ocr, "windows_status", return_value=dict(AVAILABLE)) as status:
            first = ocr.engine_status_cached()
            second = ocr.engine_status_cached()
        self.assertEqual(status.call_count, 0)
        self.assertFalse(first["available"])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
