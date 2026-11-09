"""本周复盘。只整理用户写下的话，不编造竞赛日期，也不调用模型。"""

from __future__ import annotations

import datetime as dt

from src.config import load_settings, resolve_today
from src.db import connect

BEHIND_HINT = "进度落后时，只重排你自己的安排，不要把截止日期改成原文里没有的日子。"


def current_week_key() -> str:
    day = resolve_today(None, load_settings()["timezone"])
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def get_current(user_id: int) -> dict:
    week_key = current_week_key()
    connection = connect()
    try:
        row = connection.execute(
            """
            SELECT done_text, stuck_text, next_text, summary, plan
            FROM weekly_reviews WHERE user_id = ? AND week_key = ?
            """,
            (user_id, week_key),
        ).fetchone()
        activities = _activities(connection, user_id, week_key)
    finally:
        connection.close()
    if row is None:
        payload = _empty(week_key)
    else:
        payload = _payload(
            week_key,
            row["done_text"],
            row["stuck_text"],
            row["next_text"],
            row["summary"],
            row["plan"],
        )
    payload["activities"] = activities
    return payload


def save_current(user_id: int, raw: dict) -> dict:
    done = str(raw.get("done", raw.get("本周完成", "")) or "").strip()
    stuck = str(raw.get("stuck", raw.get("卡住", "")) or "").strip()
    next_goal = str(raw.get("next", raw.get("下周目标", "")) or "").strip()
    if not done and not stuck and not next_goal:
        raise ValueError("本周完成、卡住、下周目标至少填一项。")
    summary = "\n".join(
        [
            f"本周完成：{done or '未填写。'}",
            f"卡住：{stuck or '没有写。'}",
        ]
    )
    plan = f"下周计划：{next_goal or '未填写。'}"
    week_key = current_week_key()
    updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    connection = connect()
    try:
        connection.execute(
            """
            INSERT INTO weekly_reviews (
                user_id, week_key, done_text, stuck_text, next_text, summary, plan, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, week_key) DO UPDATE SET
                done_text = excluded.done_text,
                stuck_text = excluded.stuck_text,
                next_text = excluded.next_text,
                summary = excluded.summary,
                plan = excluded.plan,
                updated_at = excluded.updated_at
            """,
            (user_id, week_key, done, stuck, next_goal, summary, plan, updated_at),
        )
        connection.commit()
        activities = _activities(connection, user_id, week_key)
    finally:
        connection.close()
    payload = _payload(week_key, done, stuck, next_goal, summary, plan)
    payload["activities"] = activities
    return payload


def _empty(week_key: str) -> dict:
    return _payload(week_key, "", "", "", "", "")


def _payload(week_key: str, done: str, stuck: str, next_goal: str, summary: str, plan: str) -> dict:
    return {
        "week": week_key,
        "done": done,
        "stuck": stuck,
        "next": next_goal,
        "summary": summary,
        "plan": plan,
        "behind_hint": BEHIND_HINT if stuck else "",
        "activities": [],
    }


def _activities(connection, user_id: int, week_key: str) -> list[dict]:
    rows = connection.execute(
        """
        SELECT id, text, draft_id, material_id, record_id
        FROM review_activities
        WHERE user_id = ? AND week_key = ?
        ORDER BY id
        """,
        (user_id, week_key),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "text": row["text"],
            "draft_id": None if row["draft_id"] is None else int(row["draft_id"]),
            "material_id": None if row["material_id"] is None else int(row["material_id"]),
            "record_id": None if row["record_id"] is None else int(row["record_id"]),
        }
        for row in rows
    ]
