"""识图草稿。确认之前不写我的记录、不进提醒、不改复盘正文。"""

from __future__ import annotations

import datetime as dt
import json

from src.bitmap import allocate_key, inspect_bitmap, preview_bytes, private_file, write_private
from src.config import load_settings, resolve_today
from src.db import connect
from src.db.sqlite_adapter import rollback_quiet
from src.ocr import OcrFailed, OcrUnavailable, engine_status, transcribe
from src.poster import parse_poster
from src.records import validate_records
from src.review import current_week_key
from src.schema import DRAFT, NOT_FOUND, RECORD_FIELDS, REMIND_NO, REMIND_YES, VERIFIED
from src.store import _COLUMNS, _to_storage

_MODES = {"识别草稿", "人工补录"}


def create_draft(user_id: int, data: bytes) -> dict:
    image, extension, mime, digest = inspect_bitmap(data)
    width, height = image.size
    ocr_text = ""
    ocr_error = ""
    status = engine_status()
    try:
        ocr_text = transcribe(image, status)
    except OcrUnavailable as exc:
        ocr_error = str(exc)
    except OcrFailed as exc:
        ocr_error = str(exc)
    if ocr_error or not ocr_text.strip():
        if not ocr_error:
            ocr_error = "没有读出文字。识别失败。"
        candidate = parse_poster("")
        mode = "人工补录"
    else:
        candidate = parse_poster(ocr_text)
        mode = "识别草稿"
    candidate["方式"] = mode
    candidate["识别提供者"] = str(status.get("provider") or "")
    candidate["识别引擎"] = str(status.get("engine") or "")
    candidate["识别语言"] = list(status.get("languages") or [])
    storage_key = allocate_key()
    original_rel, preview_rel = write_private(user_id, storage_key, extension, data, preview_bytes(image))
    created_at = dt.datetime.now(dt.UTC).isoformat()
    connection = connect()
    try:
        cursor = connection.execute(
            """
            INSERT INTO image_drafts (
                user_id, status, storage_key, original_rel, preview_rel, mime, byte_size,
                width, height, sha256, ocr_text, ocr_engine, ocr_error, candidate_json, created_at
            ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                storage_key,
                original_rel,
                preview_rel,
                mime,
                len(data),
                width,
                height,
                digest,
                ocr_text,
                "" if ocr_error else str(status.get("engine") or ""),
                ocr_error,
                json.dumps(candidate, ensure_ascii=False),
                created_at,
            ),
        )
        draft_id = int(cursor.lastrowid)
        connection.commit()
    except Exception:
        connection.close()
        _remove_quiet(original_rel, preview_rel)
        raise
    connection.close()
    return _public(user_id, draft_id)


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
        _rollback_quiet(connection)
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
        _rollback_quiet(connection)
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
    candidate = json.loads(row["candidate_json"])
    mode = candidate.get("方式") or ("人工补录" if row["ocr_error"] else "识别草稿")
    return {
        "id": int(row["id"]),
        "status": row["status"],
        "mode": mode if mode in _MODES else "人工补录",
        "engine": row["ocr_engine"],
        "provider": str(candidate.get("识别提供者") or ""),
        "languages": list(candidate.get("识别语言") or []),
        "ocr_error": row["ocr_error"],
        "ocr_text": row["ocr_text"],
        "candidate": candidate,
        "width": int(row["width"]),
        "height": int(row["height"]),
        "preview_url": f"/api/image-drafts/{int(row['id'])}/preview",
    }


def _owned(user_id: int, draft_id: int):
    connection = connect()
    try:
        row = connection.execute(
            "SELECT * FROM image_drafts WHERE id = ? AND user_id = ?",
            (draft_id, user_id),
        ).fetchone()
    finally:
        connection.close()
    return row


def _remove_quiet(*relative_paths: str) -> None:
    for relative in relative_paths:
        try:
            path = private_file(relative)
        except FileNotFoundError:
            continue
        path.unlink(missing_ok=True)


def _rollback_quiet(connection) -> None:
    rollback_quiet(connection)
