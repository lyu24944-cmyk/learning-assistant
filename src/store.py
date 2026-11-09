"""登录用户能看到的记录。

共享目录来自 data/records.csv，任何登录用户都能读，不能通过接口改它。
私人记录写在 SQLite，必须带 user_id，只返回给本人。
"""

from __future__ import annotations

import datetime as dt

from src.config import load_settings, resolve_today
from src.db import connect
from src.prefs import view_day
from src.records import REAL_RECORDS, load_records, validate_records
from src.reminder import render_message, select_reminders
from src.schema import RECORD_FIELDS
from src.timeline import deadline_mark, select_timeline

_COLUMNS = (
    "title",
    "info_type",
    "deadline",
    "source_url",
    "updated_on",
    "major",
    "status",
    "review_state",
    "date_quote",
    "remind",
    "condition",
)


def list_records(user_id: int) -> list[dict[str, str]]:
    catalog = [{**row, "归属": "共享目录"} for row in load_records(REAL_RECORDS)]
    return catalog + _own_records(user_id)


def list_timeline(user_id: int, *, today: dt.date | None = None) -> dict:
    settings = load_settings()
    day, within_days = view_day(user_id, today)
    items = select_timeline(list_records(user_id), info_type=settings["info_type"])
    return {
        "source": "共享目录 data/records.csv，另加当前用户的私人记录",
        "today": day.isoformat(),
        "info_type": settings["info_type"],
        "within_days": within_days,
        "items": [
            {
                "标题": row["标题"],
                "类型": row["类型"],
                "截止日期": due.isoformat(),
                "标记": deadline_mark(due, day),
                "来源链接": row.get("来源链接", ""),
                "日期原句": row.get("日期原句", ""),
                "归属": row.get("归属", ""),
            }
            for due, row in items
        ],
    }


def list_reminders(user_id: int, *, today: dt.date | None = None) -> dict:
    settings = load_settings()
    day, within_days = view_day(user_id, today)
    picked = select_reminders(
        list_records(user_id),
        today=day,
        info_type=settings["info_type"],
        within_days=within_days,
    )
    items = []
    for row in picked:
        due = dt.date.fromisoformat(row["截止日期"])
        items.append(
            {
                "标题": row["标题"],
                "截止日期": row["截止日期"],
                "标记": deadline_mark(due, day),
                "来源链接": row.get("来源链接", ""),
                "日期原句": row.get("日期原句", ""),
                "归属": row.get("归属", ""),
            }
        )
    return {
        "today": day.isoformat(),
        "info_type": settings["info_type"],
        "within_days": within_days,
        "message": render_message(picked, info_type=settings["info_type"], within_days=within_days),
        "items": items,
    }


def add_user_record(user_id: int, raw: dict) -> dict:
    row = {field: "" if raw.get(field) is None else str(raw.get(field)).strip() for field in RECORD_FIELDS}
    settings = load_settings()
    errors = validate_records([row], min_rows=1, allowed_types=list(settings["allowed_info_types"]))
    if errors:
        raise ValueError("；".join(errors))
    connection = connect()
    try:
        cursor = connection.execute(
            f"""
            INSERT INTO user_records (user_id, {", ".join(_COLUMNS)})
            VALUES (?, {", ".join("?" for _ in _COLUMNS)})
            """,
            (user_id, *(_to_storage(row)[column] for column in _COLUMNS)),
        )
        record_id = int(cursor.lastrowid)
        connection.commit()
    finally:
        connection.close()
    return {**row, "id": record_id, "归属": "我的"}


def list_own_records(user_id: int) -> list[dict]:
    return _own_records(user_id)


_EDIT_FIELDS = (
    "标题",
    "类型",
    "截止日期",
    "来源链接",
    "适合专业",
    "状态",
    "核对状态",
    "日期原句",
    "是否参与提醒",
    "报名条件",
)


def update_own_record(user_id: int, record_id: int, raw: dict) -> dict | None:
    existing = _own_record(user_id, record_id)
    if existing is None:
        return None
    merged = {field: existing.get(field, "") for field in RECORD_FIELDS}
    for field in _EDIT_FIELDS:
        if field in raw and raw[field] is not None:
            merged[field] = str(raw[field]).strip()
    if merged["截止日期"] == "未找到" or merged["状态"] in ("放弃", "已放弃"):
        merged["是否参与提醒"] = "否"
    merged["更新时间"] = resolve_today(None, load_settings()["timezone"]).isoformat()
    settings = load_settings()
    errors = validate_records([merged], min_rows=1, allowed_types=list(settings["allowed_info_types"]))
    if errors:
        raise ValueError("；".join(errors))
    connection = connect()
    try:
        connection.execute(
            f"""
            UPDATE user_records
            SET {", ".join(f"{column} = ?" for column in _COLUMNS)}
            WHERE id = ? AND user_id = ?
            """,
            (*(_to_storage(merged)[column] for column in _COLUMNS), record_id, user_id),
        )
        connection.commit()
        stored = connection.execute(
            f"SELECT id, {', '.join(_COLUMNS)} FROM user_records WHERE id = ? AND user_id = ?",
            (record_id, user_id),
        ).fetchone()
        item = _annotate_expiry(_row_with_id(stored), resolve_today(None, settings["timezone"]))
    finally:
        connection.close()
    return item


def delete_own_record(user_id: int, record_id: int) -> bool:
    connection = connect()
    try:
        cursor = connection.execute(
            "DELETE FROM user_records WHERE id = ? AND user_id = ?",
            (record_id, user_id),
        )
        connection.commit()
        deleted = cursor.rowcount > 0
    finally:
        connection.close()
    return deleted


def update_own_status(user_id: int, record_id: int, status: str) -> dict | None:
    from src.schema import MINE_STATUSES, REMIND_NO

    if status not in MINE_STATUSES:
        raise ValueError("状态只能是 未处理、已报名、已交 或 放弃。")
    connection = connect()
    try:
        found = connection.execute(
            "SELECT id FROM user_records WHERE id = ? AND user_id = ?",
            (record_id, user_id),
        ).fetchone()
        if found is None:
            return None
        if status == "放弃":
            connection.execute(
                "UPDATE user_records SET status = ?, remind = ? WHERE id = ? AND user_id = ?",
                (status, REMIND_NO, record_id, user_id),
            )
        else:
            connection.execute(
                "UPDATE user_records SET status = ? WHERE id = ? AND user_id = ?",
                (status, record_id, user_id),
            )
        connection.commit()
        stored = connection.execute(
            f"SELECT id, {', '.join(_COLUMNS)} FROM user_records WHERE id = ?",
            (record_id,),
        ).fetchone()
        item = _row_with_id(stored)
    finally:
        connection.close()
    return item


def _own_records(user_id: int) -> list[dict]:
    connection = connect()
    try:
        rows = connection.execute(
            f"SELECT id, {', '.join(_COLUMNS)} FROM user_records WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
        today = resolve_today(None, load_settings()["timezone"])
        copied = [_annotate_expiry(_row_with_id(row), today) for row in rows]
        links = connection.execute(
            "SELECT record_id, material_id, draft_id FROM image_links WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        connection.close()
    by_record = {int(link["record_id"]): link for link in links}
    for item in copied:
        link = by_record.get(item["id"])
        item["资料id"] = int(link["material_id"]) if link else None
        item["识图草稿"] = int(link["draft_id"]) if link else None
    return copied


def _own_record(user_id: int, record_id: int) -> dict | None:
    connection = connect()
    try:
        row = connection.execute(
            f"SELECT id, {', '.join(_COLUMNS)} FROM user_records WHERE id = ? AND user_id = ?",
            (record_id, user_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return _row_with_id(row)


def _annotate_expiry(item: dict, today: dt.date) -> dict:
    item["展示"] = ""
    deadline = (item.get("截止日期") or "").strip()
    if deadline and deadline != "未找到":
        try:
            if dt.date.fromisoformat(deadline) < today:
                item["展示"] = "已过期"
        except ValueError:
            pass
    return item


def _row_with_id(row) -> dict:
    item = _from_storage(row)
    item["id"] = int(row["id"])
    item["归属"] = "我的"
    return item


def _to_storage(row: dict[str, str]) -> dict[str, str]:
    return {column: row[field] for column, field in zip(_COLUMNS, RECORD_FIELDS, strict=True)}


def _from_storage(row) -> dict[str, str]:
    stored = {column: row[column] for column in _COLUMNS}
    return {field: stored[column] for column, field in zip(_COLUMNS, RECORD_FIELDS, strict=True)}
