"""可换性规约：方言 SQL 只允许出现在 sqlite_adapter.py。

没有这条，半年后很容易在业务模块里写出 PRAGMA、sqlite_master 或
INSERT OR REPLACE，那时“改 DATABASE_URL 就能换库”就是假的。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

DIALECT_ALLOWED = {
    "src/db/sqlite_adapter.py",
    "src/migrate.py",  # legacy，冻结；只减不增
}
SQLITE_IMPORT_ALLOWED = {
    "src/db/sqlite_adapter.py",
    "src/migrate.py",
}
FORBIDDEN = re.compile(r"\b(PRAGMA|INSERT\s+OR\s+REPLACE|strftime|AUTOINCREMENT|sqlite_master)\b", re.I)
SQLITE_IMPORT = re.compile(r"^\s*(import sqlite3|from sqlite3\s+import)", re.M)


def test_dialect_sql_is_confined_to_adapter() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative in DIALECT_ALLOWED:
            continue
        for match in FORBIDDEN.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{relative}: {match.group(0)}")
    assert not offenders, "方言 SQL 只能出现在 sqlite_adapter.py：\n" + "\n".join(offenders)


def test_sqlite3_import_is_confined_to_adapter() -> None:
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative in SQLITE_IMPORT_ALLOWED:
            continue
        if SQLITE_IMPORT.search(path.read_text(encoding="utf-8")):
            offenders.append(relative)
    assert not offenders, "sqlite3 只能由 sqlite_adapter.py 和冻结的 migrate.py 导入：\n" + "\n".join(offenders)
