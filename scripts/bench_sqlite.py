"""SQLite 并发写实验：WAL 的 go/no-go。

用法：
    python scripts/bench_sqlite.py --journal WAL --rounds 3
    python scripts/bench_sqlite.py --journal DELETE --rounds 3

判定：3 轮里出现任意一次未恢复的 readonly/locked 错误，就保持 DELETE。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.db import connect, init_db, reset_for_tests  # noqa: E402
from src.db.sqlite_adapter import write_transaction  # noqa: E402


def _seed_user() -> None:
    connection = connect()
    try:
        connection.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (1, ?, ?, ?)",
            ("bench@example.com", "synthetic", "2026-11-09T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()


def _insert(connection: sqlite3.Connection, index: int) -> None:
    with write_transaction(connection):
        connection.execute(
            """
            INSERT INTO user_records (
                user_id, title, info_type, deadline, source_url, updated_on,
                major, status, review_state, date_quote, remind, condition
            ) VALUES (1, ?, '竞赛', '2026-12-01', '未找到', '2026-11-09',
                      '未找到', '未处理', '草稿', '未找到', '否', '未找到')
            """,
            (f"bench-{index}",),
        )


def run_round(journal: str, writers: int, readers: int, writes: int, reads: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "bench.sqlite"
        os.environ["LEARNING_ASSISTANT_DB"] = str(db)
        os.environ["LEARNING_ASSISTANT_SQLITE_JOURNAL"] = journal
        reset_for_tests()
        init_db()
        _seed_user()

        errors: list[str] = []
        barrier = threading.Barrier(writers + readers)
        started = time.perf_counter()

        def writer(worker: int) -> None:
            connection = connect()
            try:
                barrier.wait()
                for index in range(writes):
                    try:
                        _insert(connection, worker * writes + index)
                    except Exception as exc:  # noqa: BLE001 - 实验要记录所有失败
                        errors.append(f"writer {worker}: {type(exc).__name__}: {exc}")
                        break
            finally:
                connection.close()

        def reader(worker: int) -> None:
            connection = connect()
            try:
                barrier.wait()
                for _ in range(reads):
                    try:
                        connection.execute("SELECT COUNT(*) FROM user_records").fetchone()
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"reader {worker}: {type(exc).__name__}: {exc}")
                        break
            finally:
                connection.close()

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(writers)]
        threads += [threading.Thread(target=reader, args=(index,)) for index in range(readers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - started

        connection = sqlite3.connect(db)
        try:
            total = connection.execute("SELECT COUNT(*) FROM user_records").fetchone()[0]
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            connection.close()

        return {
            "journal": journal,
            "mode": mode,
            "rows": total,
            "expected": writers * writes,
            "errors": errors,
            "elapsed_ms": round(elapsed * 1000, 1),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", choices=["WAL", "DELETE"], default="WAL")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--writers", type=int, default=4)
    parser.add_argument("--readers", type=int, default=4)
    parser.add_argument("--writes", type=int, default=200)
    parser.add_argument("--reads", type=int, default=200)
    args = parser.parse_args()

    results = [
        run_round(args.journal, args.writers, args.readers, args.writes, args.reads)
        for _ in range(args.rounds)
    ]
    errors = [error for result in results for error in result["errors"]]
    print(
        json.dumps(
            {
                "journal": args.journal,
                "rounds": args.rounds,
                "writers": args.writers,
                "readers": args.readers,
                "errors": errors,
                "ok": not errors,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
