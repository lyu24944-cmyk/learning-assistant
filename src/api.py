"""学习助手的 HTTP 壳。时间线要登录。不抓网页，不发明日期。"""

from __future__ import annotations

import datetime as dt
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src import ocr_queue
from src.accounts import (
    AccountError,
    change_password,
    delete_all_sessions,
    list_sessions,
    login,
    logout,
    register,
    reset_password,
    revoke_session,
    user_for_token,
)
from src.bitmap import MAX_BYTES
from src.config import load_settings, resolve_today
from src.courses import add_course, add_material, list_courses, list_materials, material_file
from src.db import bootstrap
from src.image_drafts import (
    DraftError,
    cancel_draft,
    confirm_draft,
    draft_file,
    enqueue_draft,
    get_draft,
    queue_position,
    reap_stale,
    recover_interrupted,
    retry_draft,
    run_ocr,
)
from src.logging import configure_logging, log_request, new_request_id, request_id_var
from src.ocr import engine_status_cached
from src.prefs import get as get_reminder_prefs
from src.prefs import update as update_reminder_prefs
from src.ratelimit import (
    change_password_blocked,
    clear_login_failures,
    login_blocked,
    login_ip_blocked,
    note_change_password_failure,
    note_login_failure,
    note_login_ip,
    note_registration,
    note_reset,
    register_blocked,
    reset_blocked,
)
from src.review import get_current, save_current
from src.security import hash_ip, mask_email
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
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
DEFAULT_ORIGINS = {"http://127.0.0.1:5173", "http://localhost:5173"}
logger = logging.getLogger("app.auth")


class ApiError(HTTPException):
    """带错误码的 HTTPException。批次 5 再长成完整信封。"""

    def __init__(self, status_code: int, detail: str, code: str, headers: dict | None = None) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    bootstrap()
    # 队列必须在 bootstrap 之后启动：recover_interrupted() 要读表，表得先建好。
    # 第二个参数是看门狗：每 60 秒回收卡在 running 太久的行。
    ocr_queue.start(run_ocr, reap_stale)
    recover_interrupted()
    try:
        yield
    finally:
        ocr_queue.shutdown()


app = FastAPI(title="学习助手", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)
configure_logging()


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
        headers=exc.headers,
    )


def _csrf_enabled() -> bool:
    raw = os.environ.get("LEARNING_ASSISTANT_CSRF", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _allowed_origins() -> set[str]:
    raw = os.environ.get("LEARNING_ASSISTANT_ALLOWED_ORIGINS", "")
    extra = {item.strip().rstrip("/") for item in raw.split(",") if item.strip()}
    return DEFAULT_ORIGINS | extra


def _origin_ok(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    if origin == "null":
        return False
    normalized = origin.rstrip("/")
    if normalized in _allowed_origins():
        return True
    parsed = urlsplit(normalized)
    return (parsed.scheme, parsed.netloc) == (request.url.scheme, request.url.netloc)


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


@app.middleware("http")
async def origin_guard(request: Request, call_next):
    """写请求的 Origin 必须是同源或在允许列表里；顺手加四个安全响应头。"""
    started = time.perf_counter()
    if request.method in UNSAFE_METHODS and not _origin_ok(request):
        log_request(
            method=request.method,
            path=request.url.path,
            status=403,
            duration_ms=(time.perf_counter() - started) * 1000,
            error_code="origin_rejected",
        )
        response = JSONResponse(
            status_code=403,
            content={"detail": "请求来源不对，请从本站页面操作。", "code": "origin_rejected"},
        )
    else:
        response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


class Credentials(BaseModel):
    email: str
    password: str


class ResetPasswordBody(BaseModel):
    token: str
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


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
    if request.method in UNSAFE_METHODS and _csrf_enabled():
        token = request.headers.get("X-CSRF-Token", "")
        if not hmac.compare_digest(token, str(user["csrf_token"])):
            raise ApiError(403, "请求校验失败，请刷新页面重试。", "csrf_failed")
    return user


def _account_error(exc: AccountError) -> HTTPException:
    if exc.code:
        return ApiError(exc.status, str(exc), exc.code)
    return HTTPException(status_code=exc.status, detail=str(exc))


def _parse_today(today: str | None) -> dt.date | None:
    if today is None:
        return None
    try:
        return dt.date.fromisoformat(today)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="today 必须是 YYYY-MM-DD") from exc


def _client_ip(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _session_meta(request: Request) -> dict[str, str]:
    return {
        "user_agent": request.headers.get("user-agent", ""),
        "ip_hash": hash_ip(_client_ip(request)),
        "previous_token": request.cookies.get(SESSION_COOKIE, ""),
    }


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
        raise ApiError(
            429,
            "注册过于频繁，请稍后再试。",
            "rate_limited",
            headers={"Retry-After": "3600"},
        )
    try:
        user, token, csrf = register(body.email, body.password, **_session_meta(request))
    except AccountError as exc:
        raise _account_error(exc) from exc
    note_registration(client)
    _session(request, response, token)
    logger.info("auth.register", extra={"event": "register_ok", "email_masked": mask_email(body.email)})
    return {"email": str(user["email"]), "csrf_token": csrf}


@app.post("/api/login")
def login_account(body: Credentials, request: Request, response: Response) -> dict[str, str]:
    client = _client_ip(request)
    key = (client, body.email.strip().lower())
    if login_ip_blocked(client) or login_blocked(key):
        raise ApiError(
            429,
            "登录失败次数过多，请稍后再试。",
            "rate_limited",
            headers={"Retry-After": "900"},
        )
    try:
        user, token, csrf = login(body.email, body.password, **_session_meta(request))
    except AccountError as exc:
        if exc.status == 401:
            note_login_failure(key)
            note_login_ip(client)
            logger.warning(
                "auth.login_failed",
                extra={"event": "login_failed", "email_masked": mask_email(body.email)},
            )
        raise _account_error(exc) from exc
    clear_login_failures(key)
    _session(request, response, token)
    logger.info("auth.login", extra={"event": "login_ok", "email_masked": mask_email(body.email)})
    return {"email": str(user["email"]), "csrf_token": csrf}


@app.post("/api/logout")
def logout_account(
    request: Request,
    response: Response,
    _user: dict = Depends(current_user),
) -> dict[str, bool]:
    logout(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/", secure=_secure_cookies(request))
    return {"ok": True}


@app.post("/api/auth/reset-password")
def reset_account(body: ResetPasswordBody, request: Request, response: Response) -> dict[str, bool]:
    client = _client_ip(request)
    if reset_blocked(client):
        raise ApiError(
            429,
            "尝试次数过多，请稍后再试。",
            "rate_limited",
            headers={"Retry-After": "3600"},
        )
    note_reset(client)
    try:
        reset_password(body.token, body.password)
    except AccountError as exc:
        logger.warning(
            "auth.reset_failed",
            extra={"event": "reset_failed", "error_code": exc.code or "reset_failed"},
        )
        raise _account_error(exc) from exc
    response.delete_cookie(SESSION_COOKIE, path="/", secure=_secure_cookies(request))
    logger.info("auth.reset", extra={"event": "reset_ok"})
    return {"ok": True}


@app.post("/api/auth/change-password")
def change_own_password(body: ChangePasswordBody, user: dict = Depends(current_user)) -> dict[str, bool]:
    user_id = int(user["id"])
    if change_password_blocked(user_id):
        raise ApiError(
            429,
            "尝试次数过多，请稍后再试。",
            "rate_limited",
            headers={"Retry-After": "900"},
        )
    try:
        change_password(
            user_id,
            body.current_password,
            body.new_password,
            keep_public_id=str(user["session_id"]),
        )
    except AccountError as exc:
        if exc.code == "current_password_invalid":
            note_change_password_failure(user_id)
        raise _account_error(exc) from exc
    logger.info("auth.password_changed", extra={"event": "password_changed", "user_id": user_id})
    return {"ok": True}


@app.post("/api/auth/logout-all")
def logout_all(
    request: Request,
    response: Response,
    user: dict = Depends(current_user),
) -> dict[str, bool]:
    delete_all_sessions(int(user["id"]))
    response.delete_cookie(SESSION_COOKIE, path="/", secure=_secure_cookies(request))
    return {"ok": True}


@app.get("/api/sessions")
def read_sessions(request: Request, user: dict = Depends(current_user)) -> dict:
    return {"sessions": list_sessions(int(user["id"]), request.cookies.get(SESSION_COOKIE))}


@app.delete("/api/sessions/{public_id}")
def remove_session(
    public_id: str,
    request: Request,
    response: Response,
    user: dict = Depends(current_user),
) -> dict[str, bool]:
    if not revoke_session(int(user["id"]), public_id):
        raise ApiError(404, "找不到这个会话。", "session_not_found")
    current = public_id == str(user["session_id"])
    if current:
        response.delete_cookie(SESSION_COOKIE, path="/", secure=_secure_cookies(request))
    return {"ok": True, "current": current}


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    return {
        "email": str(user["email"]),
        "email_verified": bool(user["email_verified"]),
        "csrf_token": str(user["csrf_token"]),
        "session_id": str(user["session_id"]),
    }


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
    return {**engine_status_cached(), "queue": ocr_queue.stats()}


@app.post("/api/image-drafts", status_code=202)
def upload_image_draft(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
) -> dict:
    """收下图片，排进队列，立刻返回 202。

    这个路由刻意写成同步 `def`：FastAPI 会把它整个丢进线程池，
    连读文件都不占事件循环。改成 `async def` 就得用 `await file.read()`，
    那又会把上传本身挂回事件循环上。
    """
    payload = file.file.read(MAX_BYTES + 1)
    file.file.close()
    try:
        draft, reused = enqueue_draft(int(user["id"]), payload)
    except DraftError as exc:
        raise ApiError(exc.status, str(exc), exc.code) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**draft, "reused": reused, "queue_position": queue_position(int(draft["id"]))}


@app.post("/api/image-drafts/{draft_id}/retry")
def retry_image_draft(draft_id: int, user: dict = Depends(current_user)) -> dict:
    try:
        draft = retry_draft(int(user["id"]), draft_id)
    except DraftError as exc:
        raise ApiError(exc.status, str(exc), exc.code) from exc
    if draft is None:
        raise ApiError(404, "找不到这份识图草稿。", "draft_not_found")
    return draft


@app.get("/api/image-drafts/{draft_id}")
def read_image_draft(draft_id: int, user: dict = Depends(current_user)) -> dict:
    draft = get_draft(int(user["id"]), draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="找不到这份识图草稿。")
    # queue_position 属于队列，不属于草稿，所以在这里拼，不塞进 _public()。
    return {**draft, "queue_position": queue_position(draft_id)}


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
    except DraftError as exc:
        raise ApiError(exc.status, str(exc), exc.code) from exc
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
