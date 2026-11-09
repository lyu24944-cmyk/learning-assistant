"""数据库路径与连接串。只在这里读环境变量。"""

from __future__ import annotations

import os
from pathlib import Path

from src.config import ROOT


def database_path() -> Path:
    override = os.environ.get("LEARNING_ASSISTANT_DB")
    if override:
        return Path(override)
    return ROOT / "data" / "app.sqlite"


def database_url() -> str:
    explicit = os.environ.get("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    return f"sqlite:///{database_path().as_posix()}"
