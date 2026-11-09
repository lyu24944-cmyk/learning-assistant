"""读取仓库里的配置。信息类型只改 config/settings.json。"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = ROOT / "config" / "settings.json"


def load_settings() -> dict:
    with SETTINGS_PATH.open(encoding="utf-8") as handle:
        data = json.load(handle)
    info_type = data.get("info_type")
    allowed = data.get("allowed_info_types") or []
    if info_type not in allowed:
        raise ValueError(
            f"info_type 必须是 {allowed} 之一，当前是 {info_type!r}。请改 config/settings.json。"
        )
    days = int(data.get("reminder_within_days", 7))
    if days < 0:
        raise ValueError("reminder_within_days 不能为负。")
    data["reminder_within_days"] = days
    return data


def resolve_today(raw: str | None, timezone_name: str) -> dt.date:
    """命令行传入的 YYYY-MM-DD，或该时区的今天。提醒时区保持 Asia/Shanghai。"""
    if raw:
        return dt.date.fromisoformat(raw)
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(timezone_name)).date()
    except ZoneInfoNotFoundError:
        return dt.date.today()
