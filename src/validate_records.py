"""检查手工记录是否满足表结构，以及草稿 / 未找到不参与提醒。"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.config import load_settings
from src.records import EXAMPLE_RECORDS, load_records, validate_records

DEFAULT_RECORDS = EXAMPLE_RECORDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验截止记录表")
    parser.add_argument("records", nargs="?", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--min-rows", type=int, default=10)
    args = parser.parse_args(argv)
    settings = load_settings()
    errors = validate_records(
        load_records(args.records),
        min_rows=args.min_rows,
        allowed_types=list(settings["allowed_info_types"]),
    )
    if errors:
        print(f"未通过（{len(errors)}）")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"通过：{args.records}（至少 {args.min_rows} 条，字段与提醒资格符合约定）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
