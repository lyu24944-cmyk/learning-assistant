"""读写手工记录。"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path

from src.config import ROOT
from src.schema import (
    DRAFT,
    NOT_FOUND,
    RECORD_FIELDS,
    REMIND_NO,
    REMIND_YES,
    REVIEW_STATES,
    STATUSES,
    VERIFIED,
)

REAL_RECORDS = ROOT / "data" / "records.csv"
EXAMPLE_RECORDS = ROOT / "templates" / "records.example.csv"

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_records(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".json":
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            raise ValueError("JSON 记录文件必须是数组。")
        return [_stringify(row) for row in data]
    with path.open(encoding="utf-8", newline="") as handle:
        return [_stringify(row) for row in csv.DictReader(handle)]


def write_csv(path: Path, rows: list[dict[str, str]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fields})


def validate_records(rows: list[dict[str, str]], *, min_rows: int, allowed_types: list[str]) -> list[str]:
    errors: list[str] = []
    if len(rows) < min_rows:
        errors.append(f"至少需要 {min_rows} 条记录，当前 {len(rows)} 条。")
    for index, row in enumerate(rows, start=1):
        missing = [field for field in RECORD_FIELDS if not str(row.get(field, "")).strip()]
        if missing:
            errors.append(f"第 {index} 条缺少字段：{'、'.join(missing)}")
            continue
        label = row["标题"]
        if row["类型"] not in allowed_types:
            errors.append(f"「{label}」的类型不在 {allowed_types} 中。")
        if row["状态"] not in STATUSES:
            errors.append(f"「{label}」的状态只能是 {' / '.join(STATUSES)}。")
        if row["核对状态"] not in REVIEW_STATES:
            errors.append(f"「{label}」的核对状态只能是 {' / '.join(REVIEW_STATES)}。")
        if row["是否参与提醒"] not in (REMIND_YES, REMIND_NO):
            errors.append(f"「{label}」的是否参与提醒只能是 是 或 否。")
        deadline = row["截止日期"].strip()
        if deadline != NOT_FOUND and not _real_calendar_date(deadline):
            errors.append(f"「{label}」的截止日期必须是真实公历 YYYY-MM-DD 或「未找到」。")
        if deadline == NOT_FOUND and row["是否参与提醒"] != REMIND_NO:
            errors.append(f"「{label}」日期未找到，不能参与提醒。")
        if row["核对状态"] == DRAFT and row["是否参与提醒"] != REMIND_NO:
            errors.append(f"「{label}」仍是草稿，不能参与提醒。")
        if row["状态"] in ("放弃", "已放弃") and row["是否参与提醒"] != REMIND_NO:
            errors.append(f"「{label}」已放弃，不能参与提醒。")
        if row["核对状态"] == VERIFIED and not row["日期原句"].strip():
            errors.append(f"「{label}」已核对，但日期原句为空。")
        link = row["来源链接"].strip()
        if link != NOT_FOUND and not link.startswith(("http://", "https://")):
            errors.append(f"「{label}」的来源链接必须是 http(s) 或「未找到」。")
    return errors


def _real_calendar_date(value: str) -> bool:
    if not _ISO.match(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _stringify(row: dict) -> dict[str, str]:
    return {str(key): "" if value is None else str(value).strip() for key, value in row.items()}
