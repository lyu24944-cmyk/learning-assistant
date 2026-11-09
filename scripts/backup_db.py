"""备份 SQLite 数据库并校验完整性。

只复制数据库文件，不复制 data/private/ 的原图；输出里给出原图文件数，
需要完整备份时再单独打包 data/private/。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLES = ("users", "sessions", "user_records", "image_drafts", "materials", "courses")


def backup_database(db: Path, out_dir: Path, keep: int) -> dict:
    if not db.is_file():
        raise SystemExit(f"数据库不存在：{db}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"app-{stamp}.sqlite"
    suffix = 1
    while target.exists():
        target = out_dir / f"app-{stamp}-{suffix}.sqlite"
        suffix += 1

    source = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {}
        for table in TABLES:
            try:
                counts[table] = destination.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                counts[table] = None
    finally:
        destination.close()
        source.close()

    if integrity != "ok":
        target.unlink(missing_ok=True)
        raise SystemExit(f"备份完整性校验失败：{integrity}")

    private_root = ROOT / "data" / "private"
    private_files = sum(1 for path in private_root.rglob("*") if path.is_file()) if private_root.is_dir() else 0

    removed = []
    if keep > 0:
        backups = sorted(out_dir.glob("app-*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            old.unlink()
            removed.append(str(old))

    return {
        "source": str(db),
        "target": str(target),
        "bytes": target.stat().st_size,
        "integrity": integrity,
        "counts": counts,
        "private_files": private_files,
        "removed": removed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="备份 SQLite 数据库")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "app.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "backups")
    parser.add_argument("--keep", type=int, default=7, help="保留最近几份；0 表示不清理")
    args = parser.parse_args()
    print(json.dumps(backup_database(args.db, args.out, args.keep), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
