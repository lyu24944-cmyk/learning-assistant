"""学习助手的 HTTP 壳。时间线要登录。不抓网页，不发明日期。"""

from __future__ import annotations

import datetime as dt
import os
import time

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.accounts import AccountError, login, logout, register, user_for_token
from src.bitmap import MAX_BYTES
from src.config import load_settings, resolve_today
from src.courses import add_course, add_material, list_courses, list_materials, material_file
from src.image_drafts import cancel_draft, confirm_draft, create_draft, draft_file, get_draft
from src.logging import configure_logging, log_request, new_request_id, request_id_var
from src.ocr import engine_status
from src.prefs import get as get_reminder_prefs
from src.prefs import update as update_reminder_prefs
from src.ratelimit import (
    clear_login_failures,
    login_blocked,
    note_login_failure,
    note_registration,
    register_blocked,
)
from src.review import get_current, save_current
from src.store import (
    add_user_record,
    delete_own_record,
    list_own_records,
    list_records,
    list_reminders,
    list_timeline,
    update_own_record,
    update_own_status,
)

SESSION_COOKIE = "session"

app = FastAPI(title="学习助手", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)
configure_logging()


@app.middleware("http")
async def request_log(request: Request, call_next):
    """给每个请求一个 request_id，并在结束时记一行 JSON。"""
    request_id = new_request_id(request.headers.get("X-Request-ID"))
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log_request(
            method=request.method,
            path=request.url.path,
            status=500,
            duration_ms=(time.perf_counter() - started) * 1000,
            error_code="internal_error",
        )
        raise
    else:
        response.headers["X-Request-ID"] = request_id
        log_request(
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return response
    finally:
        request_id_var.reset(token)


class Credentials(BaseModel):
    email: str
    password: str


class CourseBody(BaseModel):
    name: str = ""
    term: str = ""
    notes: str = ""


class MaterialBody(BaseModel):
    title: str = ""
    course_id: int | None = None
    kind: str = ""
    source_url: str = ""
    page_hint: str = ""


class MineEditBody(BaseModel):
    标题: str | None = None
    类型: str | None = None
    截止日期: str | None = None
    来源链接: str | None = None
    适合专业: str | None = None
    状态: str | None = None
    核对状态: str | None = None
    日期原句: str | None = None
    是否参与提醒: str | None = None
    报名条件: str | None = None


class StatusBody(BaseModel):
    状态: str


class ReviewBody(BaseModel):
    done: str = ""
    stuck: str = ""
    next: str = ""


class ReminderPrefsBody(BaseModel):
    基准日: str = ""
    提前几天: int
    是否邮件: bool = False


class ImageItem(BaseModel):
    事项: str = ""
    标题: str = ""
    类型: str = ""
    截止日期: str = ""
    日期原句: str = ""
    来源链接: str = ""
    报名条件: str = ""
    主办方: str = ""
    适合专业: str = ""
    核对状态: str = "草稿"
    是否参与提醒: str = "否"


class ImageConfirmBody(BaseModel):
    items: list[ImageItem]


class RecordBody(BaseModel):
    标题: str = ""
    类型: str = ""
    截止日期: str = ""
    来源链接: str = ""
    更新时间: str = ""
    适合专业: str = ""
    状态: str = ""
    核对状态: str = ""
    日期原句: str = ""
    是否参与提醒: str = ""
    报名条件: str = ""


def current_user(request: Request) -> dict:
    user = user_for_token(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="请先登录")
    return user


def _parse_today(today: str | None) -> dt.date | None:
    if today is None:
        return None
    try:
        return dt.date.fromisoformat(today)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="today 必须是 YYYY-MM-DD") from exc


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _secure_cookies(request: Request) -> bool:
    """HTTPS 下给会话 Cookie 加 Secure。

    本地 http://127.0.0.1 不加，否则浏览器不会回传。放在反向代理后面时，
    uvicorn 的 --proxy-headers 会让 request.url.scheme 反映 X-Forwarded-Proto；
    代理没传这个头时，用 LEARNING_ASSISTANT_SECURE_COOKIES=1 强制打开。
    """
    override = os.environ.get("LEARNING_ASSISTANT_SECURE_COOKIES", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return request.url.scheme == "https"


def _session(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=_secure_cookies(request),
        path="/",
        max_age=14 * 24 * 60 * 60,
    )


@app.get("/api/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.post("/api/register", status_code=201)
def register_account(body: Credentials, request: Request, response: Response) -> dict[str, str]:
    client = _client_ip(request)
    if register_blocked(client):
        raise HTTPException(
            status_code=429,
            detail="注册过于频繁，请稍后再试。",
            headers={"Retry-After": "3600"},
        )
    try:
        user, token = register(body.email, body.password)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    note_registration(client)
    _session(request, response, token)
    return {"email": str(user["email"])}


@app.post("/api/login")
def login_account(body: Credentials, request: Request, response: Response) -> dict[str, str]:
    key = (_client_ip(request), body.email.strip().lower())
    if login_blocked(key):
        raise HTTPException(
            status_code=429,
            detail="登录失败次数过多，请稍后再试。",
            headers={"Retry-After": "900"},
        )
    try:
        user, token = login(body.email, body.password)
    except AccountError as exc:
        if exc.status == 401:
            note_login_failure(key)
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    clear_login_failures(key)
    _session(request, response, token)
    return {"email": str(user["email"])}


@app.post("/api/logout")
def logout_account(request: Request, response: Response) -> dict[str, bool]:
    logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/", secure=_secure_cookies(request))
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict[str, str]:
    return {"email": str(user["email"])}


@app.get("/api/records")
def records(user: dict = Depends(current_user)) -> dict:
    return {
        "source": "共享目录 data/records.csv，另加当前用户的私人记录",
        "records": list_records(int(user["id"])),
    }


@app.post("/api/records", status_code=201)
def create_record(body: RecordBody, user: dict = Depends(current_user)) -> dict:
    payload = body.model_dump()
    if not str(payload.get("更新时间", "")).strip():
        settings = load_settings()
        payload["更新时间"] = resolve_today(None, settings["timezone"]).isoformat()
    try:
        return add_user_record(int(user["id"]), payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/courses")
def read_courses(user: dict = Depends(current_user)) -> dict:
    return {"courses": list_courses(int(user["id"]))}


@app.post("/api/courses", status_code=201)
def create_course(body: CourseBody, user: dict = Depends(current_user)) -> dict:
    try:
        return add_course(int(user["id"]), body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/materials")
def read_materials(
    course_id: int | None = Query(default=None),
    user: dict = Depends(current_user),
) -> dict:
    try:
        return {"materials": list_materials(int(user["id"]), course_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/materials", status_code=201)
def create_material(body: MaterialBody, user: dict = Depends(current_user)) -> dict:
    try:
        return add_material(int(user["id"]), body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/mine")
def mine(user: dict = Depends(current_user)) -> dict:
    return {"records": list_own_records(int(user["id"]))}


@app.put("/api/mine/{record_id}")
def edit_mine(record_id: int, body: MineEditBody, user: dict = Depends(current_user)) -> dict:
    payload = {key: value for key, value in body.model_dump().items() if value is not None}
    try:
        updated = update_own_record(int(user["id"]), record_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="找不到这条记录。")
    return updated


@app.delete("/api/mine/{record_id}")
def remove_mine(record_id: int, user: dict = Depends(current_user)) -> dict:
    if not delete_own_record(int(user["id"]), record_id):
        raise HTTPException(status_code=404, detail="找不到这条记录。")
    return {"ok": True}


@app.patch("/api/mine/{record_id}")
def patch_mine(record_id: int, body: StatusBody, user: dict = Depends(current_user)) -> dict:
    try:
        updated = update_own_status(int(user["id"]), record_id, body.状态.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="找不到这条记录。")
    return updated


@app.get("/api/weekly-review")
def read_review(user: dict = Depends(current_user)) -> dict:
    return get_current(int(user["id"]))


@app.put("/api/weekly-review")
def write_review(body: ReviewBody, user: dict = Depends(current_user)) -> dict:
    try:
        return save_current(int(user["id"]), body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/timeline")
def timeline(
    today: str | None = Query(default=None),
    user: dict = Depends(current_user),
) -> dict:
    return list_timeline(int(user["id"]), today=_parse_today(today))


@app.get("/api/reminder-settings")
def read_reminder_settings(user: dict = Depends(current_user)) -> dict:
    return get_reminder_prefs(int(user["id"]))


@app.put("/api/reminder-settings")
def write_reminder_settings(body: ReminderPrefsBody, user: dict = Depends(current_user)) -> dict:
    try:
        return update_reminder_prefs(int(user["id"]), body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/ocr/status")
def read_ocr_status(user: dict = Depends(current_user)) -> dict:
    return engine_status()


@app.post("/api/image-drafts", status_code=201)
async def upload_image_draft(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
) -> dict:
    payload = await file.read(MAX_BYTES + 1)
    await file.close()
    try:
        return create_draft(int(user["id"]), payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/image-drafts/{draft_id}")
def read_image_draft(draft_id: int, user: dict = Depends(current_user)) -> dict:
    draft = get_draft(int(user["id"]), draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="找不到这份识图草稿。")
    return draft


@app.get("/api/image-drafts/{draft_id}/preview")
def read_image_preview(draft_id: int, user: dict = Depends(current_user)) -> FileResponse:
    found = draft_file(int(user["id"]), draft_id, "preview")
    if found is None:
        raise HTTPException(status_code=404, detail="找不到预览。")
    path, mime = found
    return FileResponse(path, media_type=mime)


@app.get("/api/image-drafts/{draft_id}/original")
def read_image_original(draft_id: int, user: dict = Depends(current_user)) -> FileResponse:
    found = draft_file(int(user["id"]), draft_id, "original")
    if found is None:
        raise HTTPException(status_code=404, detail="找不到原图。")
    path, mime = found
    return FileResponse(path, media_type=mime, filename=path.name)


@app.post("/api/image-drafts/{draft_id}/confirm")
def confirm_image_draft(
    draft_id: int,
    body: ImageConfirmBody,
    user: dict = Depends(current_user),
) -> dict:
    try:
        saved = confirm_draft(int(user["id"]), draft_id, [item.model_dump() for item in body.items])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if saved is None:
        raise HTTPException(status_code=404, detail="找不到这份识图草稿。")
    return saved


@app.post("/api/image-drafts/{draft_id}/cancel")
def cancel_image_draft(draft_id: int, user: dict = Depends(current_user)) -> dict:
    try:
        cancelled = cancel_draft(int(user["id"]), draft_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if cancelled is None:
        raise HTTPException(status_code=404, detail="找不到这份识图草稿。")
    return cancelled


@app.get("/api/materials/{material_id}/image")
def read_material_image(material_id: int, user: dict = Depends(current_user)) -> FileResponse:
    path = material_file(int(user["id"]), material_id)
    if path is None:
        raise HTTPException(status_code=404, detail="找不到这张资料图片。")
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=mime, filename=path.name)


@app.get("/api/reminders")
def reminders(
    today: str | None = Query(default=None),
    user: dict = Depends(current_user),
) -> dict:
    return list_reminders(int(user["id"]), today=_parse_today(today))
