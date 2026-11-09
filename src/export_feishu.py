"""把规范记录导出成飞书可导入的 CSV。

飞书的日期列存不了「未找到」。未找到的行：截止日期留空，截止日期标注写「未找到」。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from src.records import EXAMPLE_RECORDS, load_records, write_csv
from src.schema import FEISHU_IMPORT_FIELDS, NOT_FOUND

DEFAULT_SOURCE = EXAMPLE_RECORDS
DEFAULT_TARGET = EXAMPLE_RECORDS.parent / "feishu-import.example.csv"


def to_feishu_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    exported: list[dict[str, str]] = []
    for row in rows:
        item = dict(row)
        if item.get("截止日期") == NOT_FOUND:
            item["截止日期"] = ""
            item["截止日期标注"] = NOT_FOUND
        else:
            item["截止日期标注"] = "已识别"
        exported.append(item)
    return exported


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出飞书导入用 CSV")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_TARGET)
    args = parser.parse_args(argv)
    rows = to_feishu_rows(load_records(args.source))
    write_csv(args.output, rows, FEISHU_IMPORT_FIELDS)
    print(f"已写出 {len(rows)} 行：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
