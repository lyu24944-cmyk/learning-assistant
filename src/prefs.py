"""每个登录用户的提醒偏好。邮件开关只保存，不发信。"""

from __future__ import annotations

import datetime as dt

from src.config import load_settings, resolve_today
from src.db import connect


def get(user_id: int) -> dict:
    connection = connect()
    try:
        row = connection.execute(
            """
            SELECT within_days, baseline_date, email_enabled, updated_at
            FROM reminder_prefs WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return _payload("", int(load_settings()["reminder_within_days"]), False, "")
    return _payload(row["baseline_date"], int(row["within_days"]), bool(row["email_enabled"]), row["updated_at"])


def update(user_id: int, raw: dict) -> dict:
    baseline = str(raw.get("基准日", "") or "").strip()
    if baseline:
        try:
            dt.date.fromisoformat(baseline)
        except ValueError as exc:
            raise ValueError("基准日只能是 YYYY-MM-DD，或留空表示今天。") from exc
    try:
        within_days = int(raw.get("提前几天"))
    except (TypeError, ValueError) as exc:
        raise ValueError("提前几天必须是正整数。") from exc
    if within_days < 1:
        raise ValueError("提前几天必须是正整数。")
    email_enabled = bool(raw.get("是否邮件"))
    updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    connection = connect()
    try:
        connection.execute(
            """
            INSERT INTO reminder_prefs (user_id, within_days, baseline_date, email_enabled, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                within_days = excluded.within_days,
                baseline_date = excluded.baseline_date,
                email_enabled = excluded.email_enabled,
                updated_at = excluded.updated_at
            """,
            (user_id, within_days, baseline, 1 if email_enabled else 0, updated_at),
        )
        connection.commit()
    finally:
        connection.close()
    return _payload(baseline, within_days, email_enabled, updated_at)


def view_day(user_id: int, today: dt.date | None) -> tuple[dt.date, int]:
    """查询没有带 today 时，用该用户保存的基准日和提前天数。"""
    prefs = get(user_id)
    if today is not None:
        day = today
    elif prefs["基准日"]:
        day = dt.date.fromisoformat(prefs["基准日"])
    else:
        day = resolve_today(None, load_settings()["timezone"])
    return day, int(prefs["提前几天"])


def _payload(baseline: str, within_days: int, email_enabled: bool, updated_at: str) -> dict:
    return {
        "基准日": baseline,
        "提前几天": within_days,
        "是否邮件": email_enabled,
        "邮件触达": "未接通",
        "updated_at": updated_at,
    }
