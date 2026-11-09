"""课程和资料索引。只保存用户自己写下的名称和链接，不编造课程内容，也不代写作业。"""

from __future__ import annotations

import datetime as dt

from src.db import connect


def list_courses(user_id: int) -> list[dict]:
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT id, name, term, notes FROM courses WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    finally:
        connection.close()
    return [_course(row) for row in rows]


def add_course(user_id: int, raw: dict) -> dict:
    name = str(raw.get("name", "") or "").strip()
    if not name:
        raise ValueError("课程名称不能为空。")
    term = str(raw.get("term", "") or "").strip()
    notes = str(raw.get("notes", "") or "").strip()
    connection = connect()
    try:
        cursor = connection.execute(
            "INSERT INTO courses (user_id, name, term, notes, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, name, term, notes, _now()),
        )
        course_id = int(cursor.lastrowid)
        connection.commit()
    finally:
        connection.close()
    return {"id": course_id, "name": name, "term": term, "notes": notes}


def list_materials(user_id: int, course_id: int | None = None) -> list[dict]:
    connection = connect()
    try:
        if course_id is None:
            rows = connection.execute(
                """
                SELECT id, course_id, title, kind, source_url, page_hint, image_rel, draft_id
                FROM materials WHERE user_id = ? ORDER BY id
                """,
                (user_id,),
            ).fetchall()
        else:
            owned = connection.execute(
                "SELECT id FROM courses WHERE id = ? AND user_id = ?",
                (course_id, user_id),
            ).fetchone()
            if owned is None:
                raise LookupError("找不到这门课。")
            rows = connection.execute(
                """
                SELECT id, course_id, title, kind, source_url, page_hint, image_rel, draft_id
                FROM materials WHERE user_id = ? AND course_id = ? ORDER BY id
                """,
                (user_id, course_id),
            ).fetchall()
        links = connection.execute(
            "SELECT material_id, record_id FROM image_links WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    finally:
        connection.close()
    by_material: dict[int, list[int]] = {}
    for link in links:
        by_material.setdefault(int(link["material_id"]), []).append(int(link["record_id"]))
    return [_material(row, by_material.get(int(row["id"]), [])) for row in rows]


def add_material(user_id: int, raw: dict) -> dict:
    title = str(raw.get("title", "") or "").strip()
    if not title:
        raise ValueError("资料标题不能为空。")
    kind = str(raw.get("kind", "") or "").strip()
    source_url = str(raw.get("source_url", "") or "").strip()
    if source_url and not source_url.startswith(("http://", "https://")):
        raise ValueError("资料链接必须是 http(s)，或留空。")
    page_hint = str(raw.get("page_hint", "") or "").strip()
    course_id = raw.get("course_id")
    if course_id in ("", None):
        course_id = None
    else:
        try:
            course_id = int(course_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("course_id 必须是整数或留空。") from exc
    connection = connect()
    try:
        if course_id is not None:
            owned = connection.execute(
                "SELECT id FROM courses WHERE id = ? AND user_id = ?",
                (course_id, user_id),
            ).fetchone()
            if owned is None:
                raise LookupError("找不到这门课。")
        cursor = connection.execute(
            """
            INSERT INTO materials (user_id, course_id, title, kind, source_url, page_hint, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, course_id, title, kind, source_url, page_hint, _now()),
        )
        material_id = int(cursor.lastrowid)
        connection.commit()
    finally:
        connection.close()
    return {
        "id": material_id,
        "course_id": course_id,
        "title": title,
        "kind": kind,
        "source_url": source_url,
        "page_hint": page_hint,
        "has_image": False,
        "draft_id": None,
        "record_ids": [],
    }


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _course(row) -> dict:
    return {"id": int(row["id"]), "name": row["name"], "term": row["term"], "notes": row["notes"]}


def material_file(user_id: int, material_id: int):
    from src.bitmap import private_file

    connection = connect()
    try:
        row = connection.execute(
            "SELECT image_rel FROM materials WHERE id = ? AND user_id = ?",
            (material_id, user_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None or not str(row["image_rel"] or "").strip():
        return None
    try:
        return private_file(row["image_rel"])
    except FileNotFoundError:
        return None


def _material(row, record_ids: list[int] | None = None) -> dict:
    course_id = row["course_id"]
    image_rel = ""
    draft_id = None
    if "image_rel" in row.keys():
        image_rel = str(row["image_rel"] or "")
        draft_id = None if row["draft_id"] is None else int(row["draft_id"])
    return {
        "id": int(row["id"]),
        "course_id": None if course_id is None else int(course_id),
        "title": row["title"],
        "kind": row["kind"],
        "source_url": row["source_url"],
        "page_hint": row["page_hint"],
        "has_image": bool(image_rel),
        "draft_id": draft_id,
        "record_ids": record_ids or [],
    }
