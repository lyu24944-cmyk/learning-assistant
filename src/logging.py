"""结构化日志。

一行一个 JSON，`request_id` 用 contextvar 贯穿。只记请求元数据，
不记密码、token、Cookie、OCR 原文。
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import os
import re
import sys
import uuid

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

_REQUEST_ID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_EXTRA_FIELDS = (
    "event",
    "method",
    "path",
    "route",
    "status",
    "duration_ms",
    "user_id",
    "client_ip_hash",
    "email_masked",
    "error_code",
    "draft_id",
    "ocr_state",
    "queue_depth",
    "queue_wait_ms",
    "ocr_ms",
)
_configured = False


class JsonFormatter(logging.Formatter):
    """把 LogRecord 变成一行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", request_id_var.get()),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """进程内只配置一次。uvicorn 启动时可能重置 root，lifespan 里会再调一次。"""
    global _configured
    root = logging.getLogger()
    if _configured and root.handlers:
        return
    level = os.environ.get("LEARNING_ASSISTANT_LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    root.setLevel(level)
    _configured = True


def new_request_id(incoming: str | None) -> str:
    """只接受形如 UUID/十六进制的入站值，避免日志注入。"""
    text = (incoming or "").strip()
    if text and _REQUEST_ID.match(text):
        return text
    return uuid.uuid4().hex


def log_request(
    *,
    method: str,
    path: str,
    status: int,
    duration_ms: float,
    user_id: int | None = None,
    error_code: str | None = None,
) -> None:
    logging.getLogger("app.request").info(
        "request",
        extra={
            "event": "request",
            "method": method,
            "path": path,
            "status": status,
            "duration_ms": round(duration_ms, 1),
            "user_id": user_id,
            "error_code": error_code,
            "request_id": request_id_var.get(),
        },
    )
