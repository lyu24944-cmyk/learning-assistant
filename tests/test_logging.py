"""request_id 与 JSON 日志。"""

from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient

from src.api import app
from src.logging import JsonFormatter


def test_health_response_has_request_id() -> None:
    client = TestClient(app)
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers.get("X-Request-ID")


def test_incoming_request_id_is_echoed_when_safe() -> None:
    client = TestClient(app)
    response = client.get("/api/health", headers={"X-Request-ID": "abc12345"})
    assert response.headers["X-Request-ID"] == "abc12345"


def test_unsafe_request_id_is_replaced() -> None:
    client = TestClient(app)
    response = client.get("/api/health", headers={"X-Request-ID": "bad value with spaces"})
    assert response.headers["X-Request-ID"] != "bad value with spaces"
    assert " " not in response.headers["X-Request-ID"]


def test_json_formatter_outputs_one_line() -> None:
    record = logging.LogRecord(
        name="app.request",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request",
        args=(),
        exc_info=None,
    )
    record.request_id = "abc12345"
    record.method = "GET"
    record.path = "/api/health"
    record.status = 200
    record.duration_ms = 1.2
    record.email_masked = "a***@example.com"
    line = JsonFormatter().format(record)
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["request_id"] == "abc12345"
    assert payload["method"] == "GET"
    assert payload["status"] == 200
    assert payload["email_masked"] == "a***@example.com"
