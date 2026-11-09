"""本周复盘：用户填写，服务端拼总结与计划，不编造竞赛事实。"""

from __future__ import annotations

import datetime as dt

from src.db import connect


def _monday(day: dt.date | None = None) -> dt.date:
    today = day or dt.date.today()
    return today - dt.timedelta(days=today.weekday())


def build_summary_and_plan(*, done: str, stuck: str, next_goals: str, week_start: str) -> tuple[str, str]:
    done_l = [line.strip(" -•\t") for line in done.splitlines() if line.strip()]
    stuck_l = [line.strip(" -•\t") for line in stuck.splitlines() if line.strip()]
    next_l = [line.strip(" -•\t") for line in next_goals.splitlines() if line.strip()]

    summary_parts = [f"本周（起始 {week_start}）"]
    if done_l:
        summary_parts.append("完成：")
        summary_parts.extend(f"- {x}" for x in done_l)
    else:
        summary_parts.append("完成：未填写")
    if stuck_l:
        summary_parts.append("卡住：")
        summary_parts.extend(f"- {x}" for x in stuck_l)
        summary_parts.append("建议：把卡住项拆小，下周只保留 1–2 个可完成动作。")
    else:
        summary_parts.append("卡住：无")

    plan_parts = ["下周计划"]
    if next_l:
        plan_parts.extend(f"- {x}" for x in next_l)
    elif stuck_l:
        plan_parts.append("- 优先处理卡住项中的第一件，写出可检查的完成标准")
    else:
        plan_parts.append("- 写下 1–3 个可检查的目标后再保存")

    return "\n".join(summary_parts), "\n".join(plan_parts)


def get_weekly_review(user_id: int, week_start: str | None = None) -> dict:
    start = week_start.strip() if week_start else _monday().isoformat()
    if week_start:
        try:
            dt.date.fromisoformat(start)
        except ValueError as exc:
            raise ValueError("week_start 必须是 YYYY-MM-DD") from exc
    connection = connect()
    try:
        row = connection.execute(
            """
            SELECT week_start, done_text, stuck_text, next_text, summary, plan, created_at
            FROM weekly_reviews WHERE user_id = ? AND week_start = ?
            """,
            (user_id, start),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return {
            "week_start": start,
            "done_text": "",
            "stuck_text": "",
            "next_text": "",
            "summary": "",
            "plan": "",
            "exists": False,
        }
    return {
        "week_start": str(row["week_start"]),
        "done_text": str(row["done_text"]),
        "stuck_text": str(row["stuck_text"]),
        "next_text": str(row["next_text"]),
        "summary": str(row["summary"]),
        "plan": str(row["plan"]),
        "created_at": str(row["created_at"]),
        "exists": True,
    }


def save_weekly_review(
    user_id: int,
    *,
    done_text: str,
    stuck_text: str,
    next_text: str,
    week_start: str | None = None,
) -> dict:
    start = (week_start or "").strip() or _monday().isoformat()
    try:
        dt.date.fromisoformat(start)
    except ValueError as exc:
        raise ValueError("week_start 必须是 YYYY-MM-DD") from exc
    done = (done_text or "").strip()
    stuck = (stuck_text or "").strip()
    nxt = (next_text or "").strip()
    if not (done or stuck or nxt):
        raise ValueError("请至少填写一项：完成 / 卡住 / 下周目标")
    summary, plan = build_summary_and_plan(done=done, stuck=stuck, next_goals=nxt, week_start=start)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    connection = connect()
    try:
        connection.execute(
            """
            INSERT INTO weekly_reviews
              (user_id, week_start, done_text, stuck_text, next_text, summary, plan, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, week_start) DO UPDATE SET
              done_text = excluded.done_text,
              stuck_text = excluded.stuck_text,
              next_text = excluded.next_text,
              summary = excluded.summary,
              plan = excluded.plan,
              created_at = excluded.created_at
            """,
            (user_id, start, done, stuck, nxt, summary, plan, now),
        )
        connection.commit()
    finally:
        connection.close()
    return get_weekly_review(user_id, start)
