"""打印已核对记录的公历截止时间线。

只读本地表格，不访问网络，不发消息，不改表。
相对日期在表里是「未找到」，不会出现在时间线上。
已经过去的日期标成「已过期」，还没到的写成「剩余N天」。
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from src.config import load_settings, resolve_today
from src.records import REAL_RECORDS, load_records
from src.schema import NOT_FOUND, VERIFIED

DEFAULT_RECORDS = REAL_RECORDS


def select_timeline(
    rows: list[dict[str, str]],
    *,
    info_type: str,
) -> list[tuple[dt.date, dict[str, str]]]:
    chosen: list[tuple[dt.date, dict[str, str]]] = []
    for row in rows:
        if row.get("类型") != info_type:
            continue
        if row.get("核对状态") != VERIFIED:
            continue
        deadline = (row.get("截止日期") or "").strip()
        if deadline == NOT_FOUND:
            continue
        try:
            due = dt.date.fromisoformat(deadline)
        except ValueError:
            continue
        chosen.append((due, row))
    chosen.sort(key=lambda item: (item[0], item[1].get("标题", "")))
    return chosen


def deadline_mark(due: dt.date, today: dt.date) -> str:
    delta = (due - today).days
    if delta < 0:
        return "已过期"
    return f"剩余{delta}天"


def render_timeline(
    items: list[tuple[dt.date, dict[str, str]]],
    *,
    today: dt.date,
    info_type: str,
) -> str:
    if not items:
        return "没有已核对的公历截止日期"
    lines = [f"截止时间线（{info_type}，基准日 {today.isoformat()}）"]
    for due, row in items:
        lines.append(f"- {row['标题']}")
        lines.append(f"  截止日期：{due.isoformat()}（{deadline_mark(due, today)}）")
        lines.append(f"  来源：{row['来源链接']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打印已核对竞赛的公历截止时间线。")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--today", default=None, help="YYYY-MM-DD。省略则用 Asia/Shanghai 的今天。")
    args = parser.parse_args(argv)
    settings = load_settings()
    today = resolve_today(args.today, settings["timezone"])
    items = select_timeline(load_records(args.records), info_type=settings["info_type"])
    print(render_timeline(items, today=today, info_type=settings["info_type"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
