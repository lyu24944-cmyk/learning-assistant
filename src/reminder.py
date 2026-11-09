"""按锁定规则预览今天该不该发提醒。

只读本地表格（默认 data/records.csv），不发消息、不改表。飞书里的真实发送是可选旁路，见 docs/feishu-automation.md。
没有命中条目时打印「今日不发送」。飞书工作流的「不满足」分支应什么都不发，不要把这四个字发出去。
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

from src.config import load_settings, resolve_today
from src.records import REAL_RECORDS, load_records
from src.schema import NOT_FOUND, REMIND_YES, VERIFIED

DEFAULT_RECORDS = REAL_RECORDS


def select_reminders(
    rows: list[dict[str, str]],
    *,
    today: dt.date,
    info_type: str,
    within_days: int,
) -> list[dict[str, str]]:
    chosen: list[tuple[dt.date, dict[str, str]]] = []
    for row in rows:
        if row.get("类型") != info_type:
            continue
        if row.get("核对状态") != VERIFIED:
            continue
        if row.get("是否参与提醒") != REMIND_YES:
            continue
        deadline = (row.get("截止日期") or "").strip()
        if deadline == NOT_FOUND:
            continue
        try:
            due = dt.date.fromisoformat(deadline)
        except ValueError:
            continue
        delta = (due - today).days
        if 0 <= delta <= within_days:
            chosen.append((due, row))
    chosen.sort(key=lambda item: (item[0], item[1].get("标题", "")))
    return [row for _due, row in chosen]


def render_message(rows: list[dict[str, str]], *, info_type: str, within_days: int) -> str:
    if not rows:
        return "今日不发送"
    lines = [f"未来{within_days}天截止（{info_type}）"]
    for row in rows:
        lines.append(f"- {row['标题']}")
        lines.append(f"  截止日期：{row['截止日期']}")
        lines.append(f"  来源：{row['来源链接']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="预览提醒正文。无命中则输出「今日不发送」。")
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--today", default=None, help="YYYY-MM-DD。省略则用 Asia/Shanghai 的今天。")
    args = parser.parse_args(argv)
    settings = load_settings()
    today = resolve_today(args.today, settings["timezone"])
    rows = load_records(args.records)
    picked = select_reminders(
        rows,
        today=today,
        info_type=settings["info_type"],
        within_days=settings["reminder_within_days"],
    )
    print(render_message(picked, info_type=settings["info_type"], within_days=settings["reminder_within_days"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
