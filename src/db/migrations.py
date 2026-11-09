"""Alembic 的程序化入口。连接串只从参数来，不在这里读环境变量。"""

from __future__ import annotations

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine

from src.config import ROOT


def alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(ROOT))
    config.set_main_option("sqlalchemy.url", url)
    return config


def upgrade_to_head(url: str) -> None:
    command.upgrade(alembic_config(url), "head")


def downgrade(url: str, revision: str = "-1") -> None:
    command.downgrade(alembic_config(url), revision)


def current_revision(url: str) -> str | None:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def head_revision() -> str:
    from alembic.script import ScriptDirectory

    config = alembic_config("sqlite://")
    return ScriptDirectory.from_config(config).get_current_head()
