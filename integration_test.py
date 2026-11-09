"""联调：登录→添加→我的改状态→提醒设置→查询/提醒→复盘。

先启动 API，再运行本脚本：

    python -m uvicorn src.api:app --host 127.0.0.1 --port 8010
    python integration_test.py

地址可以用 LEARNING_ASSISTANT_BASE 覆盖，例如 http://127.0.0.1:8010。
脚本只动自己新建的账号和记录，结束前会删掉那条私人记录。
"""

from __future__ import annotations

import json
import os
import uuid
from http.cookiejar import CookieJar
from urllib import error, request

BASE = os.environ.get("LEARNING_ASSISTANT_BASE", "http://127.0.0.1:8010").rstrip("/")
TITLE = "联调竞赛A"
jar = CookieJar()
opener = request.build_opener(request.HTTPCookieProcessor(jar))


def call(method: str, path: str, body: dict | None = None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def must(ok: bool, msg: str):
    if not ok:
        raise SystemExit("FAIL: " + msg)
    print("OK:", msg)


def main():
    email = f"itest_{uuid.uuid4().hex[:8]}@example.com"
    password = "testpass12"

    status, body = call("POST", "/api/register", {"email": email, "password": password})
    must(status == 201 and body.get("email") == email, f"注册 {status} {body}")

    status, body = call("GET", "/api/me")
    must(status == 200 and body.get("email") == email, f"会话 /me {status}")

    record = {
        "标题": TITLE,
        "类型": "竞赛",
        "截止日期": "2026-11-20",
        "来源链接": "https://example.com/contest-a",
        "更新时间": "2026-11-02",
        "适合专业": "未找到",
        "状态": "未处理",
        "核对状态": "已核对",
        "日期原句": "截止日期为2026年11月20日",
        "是否参与提醒": "是",
        "报名条件": "未找到",
    }
    status, body = call("POST", "/api/records", record)
    must(status == 201 and body.get("归属") == "我的", f"添加私人记录 {status} {body}")

    status, body = call("GET", "/api/mine")
    must(status == 200 and any(r.get("标题") == TITLE for r in body.get("records", [])), f"我的列表含新记录 {body}")
    rid = next(r["id"] for r in body["records"] if r["标题"] == TITLE)

    status, body = call("PATCH", f"/api/mine/{rid}", {"状态": "已报名"})
    must(status == 200 and body.get("状态") == "已报名", f"改状态 {status} {body}")

    status, body = call(
        "PUT",
        "/api/reminder-settings",
        {"基准日": "2026-11-02", "提前几天": 30, "是否邮件": True},
    )
    must(
        status == 200 and body.get("提前几天") == 30 and body.get("邮件触达") == "未接通",
        f"提醒设置 {status} {body}",
    )

    status, body = call("GET", "/api/reminder-settings")
    must(
        status == 200 and body.get("提前几天") == 30 and body.get("基准日") == "2026-11-02",
        f"设置持久化 {body}",
    )

    status, rem = call("GET", "/api/reminders")
    must(
        status == 200 and rem.get("within_days") == 30 and rem.get("today") == "2026-11-02",
        f"提醒读偏好 {rem}",
    )
    must(any(i.get("标题") == TITLE for i in rem.get("items", [])), f"提醒含私人记录 {rem.get('items')}")

    status, rem2 = call("GET", "/api/reminders?today=2026-11-02")
    must(status == 200 and rem2.get("today") == "2026-11-02", "查询临时基准日可用")

    status, tl = call("GET", "/api/timeline")
    must(status == 200 and any(i.get("标题") == TITLE for i in tl.get("items", [])), f"时间线含私人 {tl.get('items')}")

    status, body = call(
        "PUT",
        "/api/weekly-review",
        {
            "done": f"加了{TITLE}并改为已报名",
            "stuck": "邮件触达未接通",
            "next": "做课程与资料夹",
        },
    )
    must(
        status == 200 and TITLE in body.get("summary", "") and "课程" in body.get("plan", ""),
        f"复盘 {status} {body}",
    )

    status, body = call("GET", "/api/weekly-review")
    must(
        status == 200 and bool(body.get("week")) and TITLE in body.get("summary", ""),
        f"复盘可读回 {body}",
    )

    # 关联：提醒窗口改小后私人记录应消失（截止 11-20，基准 11-02，窗口 5 天不够）
    status, body = call(
        "PUT",
        "/api/reminder-settings",
        {"基准日": "2026-11-02", "提前几天": 5, "是否邮件": False},
    )
    must(status == 200, f"缩小窗口 {status} {body}")
    status, rem = call("GET", "/api/reminders")
    must(
        status == 200 and not any(i.get("标题") == TITLE for i in rem.get("items", [])),
        f"窗口关联生效 {rem.get('items')}",
    )

    status, body = call("DELETE", f"/api/mine/{rid}")
    must(status == 200 and body.get("ok") is True, f"清理私人记录 {status} {body}")

    status, body = call("POST", "/api/logout")
    must(status == 200 and body.get("ok") is True, f"退出 {status} {body}")

    print("ALL_PASS")


if __name__ == "__main__":
    main()
