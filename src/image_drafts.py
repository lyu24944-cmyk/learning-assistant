"""识图草稿。确认之前不写我的记录、不进提醒、不改复盘正文。

批次 3 把这里拆成两半：

- `enqueue_draft()`：同步做校验、落盘、插一行 `queued`、交给队列，立刻返回。
- `run_ocr()`：工人线程调用，读原图、识别、解析、更新那一行。不抛异常。

`status` 和 `ocr_state` 是两件事：前者管草稿的生死，后者管识别跑到哪一步。
把两者合在一起，「识别失败但草稿还能人工补录」这个状态就没法表达。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os

from PIL import Image

from src import ocr_queue
from src.bitmap import allocate_key, inspect_bitmap, preview_bytes, private_file, write_private
from src.config import load_settings, resolve_today
from src.db import connect, write_retry, write_transaction
from src.db.sqlite_adapter import rollback_quiet
from src.ocr import OcrFailed, OcrUnavailable, engine_status_cached, transcribe
from src.poster import parse_poster
from src.records import validate_records
from src.ratelimit import note_upload, upload_blocked
from src.review import current_week_key
from src.schema import DRAFT, NOT_FOUND, RECORD_FIELDS, REMIND_NO, REMIND_YES, VERIFIED
from src.store import _COLUMNS, _to_storage

logger = logging.getLogger("app.ocr")

_MODES = {"识别草稿", "人工补录"}

MAX_ATTEMPTS = 3
UNFINISHED_LIMIT = 2
UNFINISHED_ENV = "LEARNING_ASSISTANT_OCR_UNFINISHED_LIMIT"
RUNNING_TIMEOUT_SECONDS = 180
POLL_AFTER_QUEUED = 800
POLL_AFTER_RUNNING = 1000
POLL_AFTER_BUSY = 2000
BUSY_DEPTH = 15
SECONDS_PER_TASK = 3

_INSERT = """
INSERT INTO image_drafts (
    user_id, status, storage_key, original_rel, preview_rel, mime, byte_size,
    width, height, sha256, ocr_text, ocr_engine, ocr_error, candidate_json, created_at,
    ocr_state, ocr_queued_at, ocr_started_at, ocr_finished_at, ocr_attempts, ocr_provider
) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, '', '', '', ?, ?, 'queued', ?, '', '', 0, '')
"""


class DraftError(ValueError):
    """带错误码的草稿错误。api.py 按 status/code 映射成 HTTP 响应。"""

    def __init__(self, message: str, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def sync_mode() -> bool:
    """同步模式：请求里直接把识别做完。

    给测试、调试和回滚用。默认关闭；开着的时候测不到队列，
    队列本身由 tests/test_ocr_queue.py 单独覆盖。
    """
    raw = os.environ.get("LEARNING_ASSISTANT_OCR_SYNC", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def unfinished_limit() -> int:
    """单用户同时排队的草稿数上限。默认 2。

    可调是为了测试「队列满回滚」那条分支：不放开它，第 3 张会先被这道闸拦成 429，
    根本走不到 `submit()` 返回 False 的地方。
    """
    raw = os.environ.get(UNFINISHED_ENV, "").strip()
    try:
        return max(1, int(raw)) if raw else UNFINISHED_LIMIT
    except ValueError:
        return UNFINISHED_LIMIT


def empty_candidate(mode: str = "识别草稿") -> dict:
    """合法但空白的候选。

    必须是 `parse_poster("")` 的完整形状，不能是 `{"items": []}`：
    `_public()` 直接把它交给前端，前端按 `事项` 取列表。

    `mode` 决定前端看到的是「识别草稿」还是「人工补录」。识别失败时传后者，
    否则失败草稿会显示成一份正常的识别结果。
    """
    candidate = parse_poster("")
    candidate["方式"] = mode
    return candidate


def enqueue_draft(user_id: int, data: bytes) -> tuple[dict, bool]:
    """收下图片并排进队列。返回 (草稿视图, 是否复用了已有结果)。

    三道闸的顺序是刻意的：单人在途 → 单人频率 → 全局队列。
    队列上限放最后，否则一个人狂点就能吃掉全部 20 个位，别人全被 503。
    闸和查重都在写文件之前——被拒的请求不该在磁盘上留下任何字节。
    """
    image, extension, mime, digest = inspect_bitmap(data)
    width, height = image.size

    if _unfinished_count(user_id) >= unfinished_limit():
        raise DraftError("上一张还在识别，等它出来再传下一张。", "draft_busy", 429)
    if upload_blocked(user_id):
        raise DraftError("今天传得有点多，歇一会儿再试。", "upload_rate_limited", 429)

    reused = _find_reusable(user_id, digest)
    if reused is not None:
        logger.info("ocr.reused", extra={"event": "ocr_reused", "draft_id": int(reused["id"])})
        return _public(user_id, int(reused["id"])), True

    storage_key = allocate_key()
    original_rel, preview_rel = write_private(
        user_id, storage_key, extension, data, preview_bytes(image)
    )
    queued_at = _now()
    try:
        draft_id = _insert_queued(
            user_id=user_id,
            storage_key=storage_key,
            original_rel=original_rel,
            preview_rel=preview_rel,
            mime=mime,
            byte_size=len(data),
            width=width,
            height=height,
            sha256=digest,
            candidate_json=json.dumps(empty_candidate(), ensure_ascii=False),
            queued_at=queued_at,
        )
    except Exception:
        _remove_quiet(original_rel, preview_rel)
        raise
    note_upload(user_id)

    if sync_mode():
        run_ocr(draft_id)
        return _public(user_id, draft_id), False

    if not ocr_queue.stats()["started"]:
        # 队列没启动（CLI、测试、lifespan 没跑）。同步跑完，好过留一条永远 queued 的行。
        logger.warning("ocr.queue_not_started", extra={"event": "ocr_queue_not_started"})
        run_ocr(draft_id)
        return _public(user_id, draft_id), False

    if not ocr_queue.submit(draft_id):
        _discard_draft(user_id, draft_id)
        raise DraftError("前面还有人在识别，约 30 秒后再试。", "ocr_queue_full", 503)

    logger.info(
        "ocr.enqueued",
        extra={
            "event": "ocr_enqueued",
            "draft_id": draft_id,
            "queue_depth": ocr_queue.stats()["depth"],
        },
    )
    return _public(user_id, draft_id), False


def run_ocr(draft_id: int) -> None:
    """工人线程调用。任何异常都在这里落成 failed，绝不往外抛。"""
    row = _row(draft_id)
    if row is None or row["status"] == "cancelled":
        return

    started_at = _now()
    if not _mark_running(draft_id, started_at):
        return
    wait_ms = _elapsed_ms(row["ocr_queued_at"], started_at)
    logger.info(
        "ocr.started",
        extra={"event": "ocr_started", "draft_id": draft_id, "queue_wait_ms": wait_ms},
    )

    try:
        with Image.open(private_file(row["original_rel"])) as image:
            image.load()
            status = engine_status_cached()
            text = transcribe(image, status)
        if not str(text).strip():
            raise OcrFailed("没有读出文字。识别失败。")
        candidate = parse_poster(text)
        candidate["方式"] = "识别草稿"
        candidate["识别提供者"] = str(status.get("provider") or "")
        candidate["识别引擎"] = str(status.get("engine") or "")
        candidate["识别语言"] = list(status.get("languages") or [])
    except (OcrUnavailable, OcrFailed) as exc:
        if not _finish_failed(draft_id, str(exc)):
            _finish_cancelled(draft_id)
        return
    except FileNotFoundError:
        if not _finish_failed(draft_id, "原图不见了，识别中断。请重新上传。"):
            _finish_cancelled(draft_id)
        return
    except Exception as exc:  # 不让任何意外把工人线程带走
        logger.exception("ocr.crashed", extra={"event": "ocr_crashed", "draft_id": draft_id})
        if not _finish_failed(draft_id, f"识别过程出错：{exc}"):
            _finish_cancelled(draft_id)
        return

    if not _finish_done(
        draft_id,
        text,
        candidate,
        str(status.get("engine") or ""),
        str(status.get("provider") or ""),
    ):
        # 用户在识别途中取消了。结果丢掉，但状态要收尾，
        # 否则这一行会永远停在 running，一直占着「未完成」名额。
        _finish_cancelled(draft_id)
        return
    logger.info(
        "ocr.done",
        extra={"event": "ocr_done", "draft_id": draft_id, "ocr_ms": _elapsed_ms(started_at, _now())},
    )


def retry_draft(user_id: int, draft_id: int) -> dict | None:
    """把失败的草稿重新排队。"""
    row = _owned(user_id, draft_id)
    if row is None:
        return None
    if row["status"] == "cancelled":
        raise DraftError("这份草稿已取消，不能再识别。", "draft_cancelled", 409)
    if row["status"] == "confirmed":
        raise DraftError("这份草稿已经写入记录，不用再识别。", "draft_confirmed", 409)
    if row["ocr_state"] in {"queued", "running"}:
        raise DraftError("这份图正在识别，稍等一下。", "ocr_already_running", 409)
    if int(row["ocr_attempts"]) >= MAX_ATTEMPTS:
        raise DraftError(
            f"已经试过 {MAX_ATTEMPTS} 次，请重新上传或人工补录。", "ocr_retry_exhausted", 409
        )

    _requeue_existing(draft_id)
    if sync_mode() or not ocr_queue.stats()["started"]:
        run_ocr(draft_id)
        return _public(user_id, draft_id)
    if not ocr_queue.submit(draft_id):
        _finish_failed(draft_id, "识别任务排队已满，请稍后再试。")
        raise DraftError("识别任务排队已满，请稍后再试。", "ocr_queue_full", 503)
    return _public(user_id, draft_id)


def recover_interrupted(now: dt.datetime | None = None) -> dict:
    """启动时收拾上一次进程留下的烂摊子。

    - `running` 且开始时间超过 5 分钟 → 标失败（进程已经不在了，结果不会回来）。
    - `queued` → 重新入队；塞不下的标失败，不静默丢。
    """
    moment = now or dt.datetime.now(dt.UTC)
    stale_before = (moment - dt.timedelta(seconds=RUNNING_TIMEOUT_SECONDS)).isoformat()
    failed = 0
    connection = connect()
    try:
        stale = connection.execute(
            "SELECT id FROM image_drafts WHERE ocr_state = 'running' "
            "AND (ocr_started_at = '' OR ocr_started_at < ?)",
            (stale_before,),
        ).fetchall()
        for row in stale:
            if _fail_running(
                connection, int(row["id"]), moment.isoformat(), "服务重启，识别中断，请重试。"
            ):
                failed += 1
        queued = [
            int(row["id"])
            for row in connection.execute(
                "SELECT id FROM image_drafts WHERE ocr_state = 'queued' ORDER BY id"
            ).fetchall()
        ]
        connection.commit()
    finally:
        connection.close()

    result = ocr_queue.requeue(queued)
    dropped = 0
    for draft_id in queued[result["requeued"] :]:
        _finish_failed(draft_id, "重启后排队已满，没能恢复。请重试。")
        dropped += 1
    if failed or result["requeued"] or dropped:
        logger.info(
            "ocr.recovered",
            extra={
                "event": "ocr_recovered",
                "queue_depth": result["requeued"],
                "error_code": f"failed={failed},dropped={dropped}",
            },
        )
    return {"failed": failed, "requeued": result["requeued"], "dropped": dropped}


def get_draft(user_id: int, draft_id: int) -> dict | None:
    return _public(user_id, draft_id)


def draft_file(user_id: int, draft_id: int, kind: str) -> tuple[object, str] | None:
    row = _owned(user_id, draft_id)
    if row is None:
        return None
    relative = row["preview_rel"] if kind == "preview" else row["original_rel"]
    mime = "image/jpeg" if kind == "preview" else row["mime"]
    try:
        path = private_file(relative)
    except FileNotFoundError:
        return None
    return path, mime


def cancel_draft(user_id: int, draft_id: int) -> dict | None:
    connection = connect()
    try:
        connection.isolation_level = ""
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status FROM image_drafts WHERE id = ? AND user_id = ?",
            (draft_id, user_id),
        ).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            return None
        if row["status"] == "confirmed":
            raise ValueError("已经确认的草稿不能取消。")
        if row["status"] != "cancelled":
            connection.execute(
                "UPDATE image_drafts SET status = 'cancelled' WHERE id = ? AND user_id = ?",
                (draft_id, user_id),
            )
        connection.execute("COMMIT")
    except Exception:
        rollback_quiet(connection)
        raise
    finally:
        connection.close()
    return _public(user_id, draft_id)


def confirm_draft(user_id: int, draft_id: int, items: list[dict]) -> dict | None:
    connection = connect()
    try:
        connection.isolation_level = ""
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM image_drafts WHERE id = ? AND user_id = ?",
            (draft_id, user_id),
        ).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            return None
        if row["status"] == "cancelled":
            raise ValueError("这份草稿已取消，没有写入记录。")
        if row["status"] == "confirmed":
            connection.execute("ROLLBACK")
            return _confirmed_view(user_id, draft_id)
        # 这一行是修 bug，不是防御性编程：queued/running 时 ocr_text 和 ocr_error 都是空，
        # 下面的 manual 判断会把没识别完的草稿当成「人工补录」，真的写进我的记录和复盘。
        # 只拦这两个状态：failed 是允许确认的，那正是人工补录的入口。
        if row["ocr_state"] in {"queued", "running"}:
            raise DraftError("识别还没结束，请等结果出来再确认。", "ocr_not_ready", 409)
        if not items:
            raise ValueError("至少保留一条事项。")
        manual = bool(row["ocr_error"]) or not str(row["ocr_text"]).strip()
        records = [_record(item) for item in items]
        settings = load_settings()
        errors = validate_records(records, min_rows=1, allowed_types=list(settings["allowed_info_types"]))
        if errors:
            raise ValueError("；".join(errors))
        material_id = _insert_material(connection, user_id, row, records[0], manual)
        week_key = current_week_key()
        stamp = dt.datetime.now(dt.UTC).isoformat()
        saved = []
        for record in records:
            record_id = _insert_record(connection, user_id, record)
            connection.execute(
                """
                INSERT INTO image_links (user_id, draft_id, material_id, record_id)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, draft_id, material_id, record_id),
            )
            text = _activity_text(record["标题"], manual)
            connection.execute(
                """
                INSERT INTO review_activities (
                    user_id, week_key, text, draft_id, material_id, record_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, week_key, text, draft_id, material_id, record_id, stamp),
            )
            saved.append(record_id)
        connection.execute(
            "UPDATE image_drafts SET status = 'confirmed' WHERE id = ? AND user_id = ? AND status = 'pending'",
            (draft_id, user_id),
        )
        connection.execute("COMMIT")
    except Exception:
        rollback_quiet(connection)
        raise
    finally:
        connection.close()
    return _confirmed_view(user_id, draft_id, saved)


def _record(item: dict) -> dict[str, str]:
    matter = str(item.get("事项", "") or "").strip()
    title = str(item.get("标题", "") or "").strip()
    if matter and matter not in title:
        title = f"{title}-{matter}" if title else matter
    deadline = str(item.get("截止日期", "") or "").strip() or NOT_FOUND
    quote = str(item.get("日期原句", "") or "").strip() or NOT_FOUND
    review = str(item.get("核对状态", "") or "").strip() or DRAFT
    remind = str(item.get("是否参与提醒", "") or "").strip() or REMIND_NO
    if review not in (DRAFT, VERIFIED):
        raise ValueError("核对状态只能是草稿或已核对。")
    if review != VERIFIED:
        remind = REMIND_NO
    if deadline == NOT_FOUND:
        remind = REMIND_NO
    if review == VERIFIED and deadline == NOT_FOUND:
        raise ValueError("日期仍是未找到，不能标成已核对后参与提醒。请保持草稿，或先写下公历日期。")
    if remind not in (REMIND_YES, REMIND_NO):
        raise ValueError("是否参与提醒只能是 是 或 否。")
    if remind == REMIND_YES and review != VERIFIED:
        raise ValueError("未核对的日期不参与提醒。")
    condition = _compose_condition(item.get("报名条件", ""), item.get("主办方", ""))
    row = {
        "标题": title,
        "类型": str(item.get("类型", "") or "").strip() or NOT_FOUND,
        "截止日期": deadline,
        "来源链接": str(item.get("来源链接", "") or "").strip() or NOT_FOUND,
        "更新时间": resolve_today(None, load_settings()["timezone"]).isoformat(),
        "适合专业": str(item.get("适合专业", "") or "").strip() or NOT_FOUND,
        "状态": "未处理",
        "核对状态": review,
        "日期原句": quote,
        "是否参与提醒": remind,
        "报名条件": condition,
    }
    return {field: row[field] for field in RECORD_FIELDS}


def _compose_condition(condition, organizer) -> str:
    condition_text = str(condition or "").strip()
    organizer_text = str(organizer or "").strip()
    if organizer_text and organizer_text != NOT_FOUND:
        if not condition_text or condition_text == NOT_FOUND:
            return f"主办方：{organizer_text}"
        return f"{condition_text}（主办方：{organizer_text}）"
    return condition_text or NOT_FOUND


def _insert_material(connection, user_id: int, draft, record: dict[str, str], manual: bool) -> int:
    title = f"{'人工补录原图' if manual else '识图原图'}：{record['标题']}"[:80]
    if record["来源链接"] == NOT_FOUND:
        hint = "原图为来源证据。图中没有官网链接，来源链接保持未找到。"
    else:
        hint = "原图为来源证据。图中读到的链接写在关联记录里。"
    if manual:
        hint = "识别失败或不可用后的人工补录。这不是识别结果。" + hint
    cursor = connection.execute(
        """
        INSERT INTO materials (
            user_id, course_id, title, kind, source_url, page_hint, created_at, image_rel, draft_id
        ) VALUES (?, NULL, ?, ?, '', ?, ?, ?, ?)
        """,
        (
            user_id,
            title,
            "人工补录原图" if manual else "识图原图",
            hint,
            dt.datetime.now(dt.UTC).isoformat(),
            draft["original_rel"],
            int(draft["id"]),
        ),
    )
    return int(cursor.lastrowid)


def _insert_record(connection, user_id: int, record: dict[str, str]) -> int:
    stored = _to_storage(record)
    cursor = connection.execute(
        f"""
        INSERT INTO user_records (user_id, {", ".join(_COLUMNS)})
        VALUES (?, {", ".join("?" for _ in _COLUMNS)})
        """,
        (user_id, *(stored[column] for column in _COLUMNS)),
    )
    return int(cursor.lastrowid)


def _activity_text(title: str, manual: bool) -> str:
    if manual:
        return f"识别失败后人工补录「{title}」，并保存了原图。这不是识别结果。"
    return f"识图新增「{title}」，并整理了资料。"


def _confirmed_view(user_id: int, draft_id: int, record_ids: list[int] | None = None) -> dict:
    connection = connect()
    try:
        links = connection.execute(
            """
            SELECT material_id, record_id FROM image_links
            WHERE user_id = ? AND draft_id = ? ORDER BY id
            """,
            (user_id, draft_id),
        ).fetchall()
    finally:
        connection.close()
    material_id = int(links[0]["material_id"]) if links else None
    stored_ids = [int(link["record_id"]) for link in links]
    return {
        "id": draft_id,
        "status": "confirmed",
        "material_id": material_id,
        "record_ids": record_ids or stored_ids,
        "idempotent": record_ids is None,
    }


def _public(user_id: int, draft_id: int) -> dict | None:
    row = _owned(user_id, draft_id)
    if row is None:
        return None
    candidate = json.loads(row["candidate_json"] or "{}")
    mode = candidate.get("方式") or ("人工补录" if row["ocr_error"] else "识别草稿")
    return {
        "id": int(row["id"]),
        "status": row["status"],
        "mode": mode if mode in _MODES else "人工补录",
        "engine": row["ocr_engine"],
        "provider": row["ocr_provider"] or str(candidate.get("识别提供者") or ""),
        "languages": list(candidate.get("识别语言") or []),
        "ocr_error": row["ocr_error"],
        "ocr_text": row["ocr_text"],
        "ocr_state": row["ocr_state"],
        "ocr_attempts": int(row["ocr_attempts"]),
        "candidate": candidate,
        "width": int(row["width"]),
        "height": int(row["height"]),
        "preview_url": f"/api/image-drafts/{int(row['id'])}/preview",
        "poll_after_ms": _poll_after(row),
        "reused": False,
    }


def _poll_after(row) -> int:
    """轮询间隔由后端给，前端只负责遵守。"""
    if row["status"] != "pending" or row["ocr_state"] in {"done", "failed"}:
        return 0
    if row["ocr_state"] == "running":
        return POLL_AFTER_RUNNING
    return POLL_AFTER_BUSY if ocr_queue.stats()["depth"] >= BUSY_DEPTH else POLL_AFTER_QUEUED


def _owned(user_id: int, draft_id: int):
    connection = connect()
    try:
        return connection.execute(
            "SELECT * FROM image_drafts WHERE id = ? AND user_id = ?",
            (draft_id, user_id),
        ).fetchone()
    finally:
        connection.close()


def _row(draft_id: int):
    connection = connect()
    try:
        return connection.execute(
            "SELECT * FROM image_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
    finally:
        connection.close()


def _find_reusable(user_id: int, digest: str):
    """同一张图已经有结果或正在跑，就复用那条，不再识别一次。

    命中 `done` 返回现成结果；命中 `queued` / `running` 返回那条在途草稿——
    连点两下、或者两个标签页同时传同一张，只会识别一次。
    """
    connection = connect()
    try:
        return connection.execute(
            "SELECT id FROM image_drafts WHERE user_id = ? AND sha256 = ? "
            "AND status = 'pending' AND ocr_state IN ('done', 'queued', 'running') "
            "ORDER BY id DESC LIMIT 1",
            (user_id, digest),
        ).fetchone()
    finally:
        connection.close()


def queue_position(draft_id: int) -> int:
    """前面还有几个任务在排队。0 表示不在排队（正在识别或已结束）。

    口径是「前面还有几个」，走主键计数，不维护内存位置表——
    内存里的顺序和库里的顺序会不一致，反而难对账。
    """
    connection = connect()
    try:
        row = connection.execute(
            "SELECT ocr_state FROM image_drafts WHERE id = ?", (draft_id,)
        ).fetchone()
        if row is None or row["ocr_state"] != "queued":
            return 0
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM image_drafts WHERE ocr_state = 'queued' AND id < ?",
                (draft_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def reap_stale(now: dt.datetime | None = None) -> int:
    """看门狗：把卡在 `running` 超过 180 秒的行标失败。

    正常情况永远不该有活干——`subprocess.run(timeout=30)` 已经给了单个任务上界。
    它防的是异常：PowerShell 卡在 kill 不掉的状态，或者工人线程被某个没预料到的
    阻塞调用占住。没有它，一条 `running` 行会一直占着两个工人之一，
    而且只在进程重启时才被 `recover_interrupted()` 收拾。
    """
    moment = now or dt.datetime.now(dt.UTC)
    cutoff = (moment - dt.timedelta(seconds=RUNNING_TIMEOUT_SECONDS)).isoformat()
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT id, ocr_started_at FROM image_drafts WHERE ocr_state = 'running' "
            "AND (ocr_started_at = '' OR ocr_started_at < ?)",
            (cutoff,),
        ).fetchall()
        reaped = 0
        for row in rows:
            if _fail_running(connection, int(row["id"]), moment.isoformat(), "识别超时，请重试。"):
                reaped += 1
                logger.warning(
                    "ocr.reaped",
                    extra={
                        "event": "ocr_reaped",
                        "draft_id": int(row["id"]),
                        "ocr_ms": _elapsed_ms(row["ocr_started_at"], moment.isoformat()),
                    },
                )
        connection.commit()
        return reaped
    finally:
        connection.close()


def _unfinished_count(user_id: int) -> int:
    connection = connect()
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM image_drafts WHERE user_id = ? "
                "AND ocr_state IN ('queued', 'running')",
                (user_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()


@write_retry
def _insert_queued(
    *,
    user_id: int,
    storage_key: str,
    original_rel: str,
    preview_rel: str,
    mime: str,
    byte_size: int,
    width: int,
    height: int,
    sha256: str,
    candidate_json: str,
    queued_at: str,
) -> int:
    connection = connect()
    try:
        with write_transaction(connection):
            cursor = connection.execute(
                _INSERT,
                (
                    user_id,
                    storage_key,
                    original_rel,
                    preview_rel,
                    mime,
                    byte_size,
                    width,
                    height,
                    sha256,
                    candidate_json,
                    queued_at,
                    queued_at,
                ),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


@write_retry
def _mark_running(draft_id: int, started_at: str) -> bool:
    connection = connect()
    try:
        with write_transaction(connection):
            cursor = connection.execute(
                "UPDATE image_drafts SET ocr_state = 'running', ocr_started_at = ?, "
                "ocr_attempts = ocr_attempts + 1, ocr_error = '' "
                "WHERE id = ? AND status = 'pending' AND ocr_state IN ('queued', 'failed')",
                (started_at, draft_id),
            )
            return cursor.rowcount > 0
    finally:
        connection.close()


@write_retry
def _finish_done(draft_id: int, text: str, candidate: dict, engine: str, provider: str) -> bool:
    connection = connect()
    try:
        with write_transaction(connection):
            cursor = connection.execute(
                "UPDATE image_drafts SET ocr_text = ?, candidate_json = ?, ocr_engine = ?, "
                "ocr_provider = ?, ocr_state = 'done', ocr_error = '', ocr_finished_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (text, json.dumps(candidate, ensure_ascii=False), engine, provider, _now(), draft_id),
            )
            return cursor.rowcount > 0
    finally:
        connection.close()


@write_retry
def _finish_failed(draft_id: int, message: str) -> bool:
    connection = connect()
    try:
        with write_transaction(connection):
            cursor = connection.execute(
                "UPDATE image_drafts SET ocr_state = 'failed', ocr_error = ?, "
                "candidate_json = ?, ocr_finished_at = ? WHERE id = ? AND status = 'pending'",
                (
                    message,
                    json.dumps(empty_candidate("人工补录"), ensure_ascii=False),
                    _now(),
                    draft_id,
                ),
            )
            return cursor.rowcount > 0
    finally:
        connection.close()


@write_retry
def _finish_cancelled(draft_id: int) -> None:
    """用户取消了：不写识别结果，只把任务状态收尾，别让 running 永远挂着。"""
    connection = connect()
    try:
        with write_transaction(connection):
            connection.execute(
                "UPDATE image_drafts SET ocr_state = 'done', ocr_error = '', ocr_finished_at = ? "
                "WHERE id = ? AND status = 'cancelled' AND ocr_state IN ('queued', 'running')",
                (_now(), draft_id),
            )
    finally:
        connection.close()


@write_retry
def _requeue_existing(draft_id: int) -> None:
    connection = connect()
    try:
        with write_transaction(connection):
            connection.execute(
                "UPDATE image_drafts SET ocr_state = 'queued', ocr_queued_at = ?, "
                "ocr_error = '', candidate_json = ? WHERE id = ? AND status = 'pending'",
                (_now(), json.dumps(empty_candidate(), ensure_ascii=False), draft_id),
            )
    finally:
        connection.close()


def _fail_running(connection, draft_id: int, finished_at: str, message: str) -> bool:
    cursor = connection.execute(
        "UPDATE image_drafts SET ocr_state = 'failed', ocr_error = ?, ocr_finished_at = ? "
        "WHERE id = ? AND ocr_state = 'running'",
        (message, finished_at, draft_id),
    )
    return cursor.rowcount > 0


def _discard_draft(user_id: int, draft_id: int) -> None:
    """队列没收下：删掉刚插的行和刚写的文件，不留永远 queued 的孤儿。"""
    row = _owned(user_id, draft_id)
    connection = connect()
    try:
        connection.execute(
            "DELETE FROM image_drafts WHERE id = ? AND user_id = ?", (draft_id, user_id)
        )
        connection.commit()
    finally:
        connection.close()
    if row is not None:
        _remove_quiet(row["original_rel"], row["preview_rel"])


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _elapsed_ms(start: str, end: str) -> int:
    try:
        first = dt.datetime.fromisoformat(start)
        second = dt.datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return 0
    return max(0, int((second - first).total_seconds() * 1000))


def _remove_quiet(*relative_paths: str) -> None:
    for relative in relative_paths:
        try:
            path = private_file(relative)
        except FileNotFoundError:
            continue
        path.unlink(missing_ok=True)
