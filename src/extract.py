"""把通知原文抽成 JSON。

这是纯函数：不读时钟、不访问网络、不发消息、不改表。
相对日期一律写成「未找到」，并保留日期原句。
"""

from __future__ import annotations

import datetime as dt
import re

from src.schema import DRAFT, EXTRACT_FIELDS, NOT_FOUND, REMIND_NO

_URL = re.compile(r"https?://[^\s<>\"'，。；、）)】\]]+")
_BOOK_TITLE = re.compile(r"《([^》]{1,40})》")
_MAJOR = re.compile(r"(?:面向对象|面向|适合)[:：]?\s*([^，。；\n]{1,20}?专业)")
_CONDITION = re.compile(r"报名条件[:：]\s*([^。\n]+)")
_INJECTION = re.compile(r"忽略之前|忽略以上|不要执行|给所有人发|你现在必须")
_RELATIVE = re.compile(
    r"本周[一二三四五六日天]|这周[一二三四五六日天]|下周[一二三四五六日天]"
    r"|今天|明天|后天|今晚|尽快|日前|月底|月初|\d+\s*天内|近日"
)
_EXPLICIT = (
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"(20\d{2})/(\d{1,2})/(\d{1,2})"),
    re.compile(r"(20\d{2})\.(\d{1,2})\.(\d{1,2})"),
)
_PRIORITY = (
    "报名截止日期",
    "报名截止",
    "截止日期",
    "截止时间",
    "截止报名",
    "前完成报名",
    "完成报名",
)


def extract_notice(text: str) -> dict[str, str]:
    """从原文提取字段。核对状态固定为草稿，不参与提醒。"""
    source = text or ""
    sentences = _sentences(source)
    kept = [item for item in sentences if not _INJECTION.search(item[2])]
    chosen = _pick_deadline(kept)
    if chosen is None:
        relative = _relative_sentence(kept)
        deadline = NOT_FOUND
        quote = relative if relative else NOT_FOUND
    else:
        deadline = chosen[0]
        quote = chosen[1]
    result = {
        "标题": _title(source),
        "类型": _info_type(source),
        "截止日期": deadline,
        "来源链接": _link(source),
        "报名条件": _condition(source),
        "日期原句": quote,
        "适合专业": _major(source),
        "核对状态": DRAFT,
        "是否参与提醒": REMIND_NO,
    }
    return {key: result[key] for key in EXTRACT_FIELDS}


def explicit_dates(text: str) -> list[str]:
    """原文里真正写明的公历日期，按出现顺序。不含被指令句带出来的日期。"""
    found: list[str] = []
    without_urls = _URL.sub(" ", text or "")
    for _start, _end, sentence in _sentences(without_urls):
        if _INJECTION.search(sentence):
            continue
        for iso, _span in _dates_in(sentence):
            if iso not in found:
                found.append(iso)
    return found


def _sentences(text: str) -> list[tuple[int, int, str]]:
    pieces: list[tuple[int, int, str]] = []
    start = 0
    for match in re.finditer(r"[。！？\n]", text):
        chunk = text[start : match.end()].strip()
        if chunk:
            pieces.append((start, match.end(), chunk))
        start = match.end()
    tail = text[start:].strip()
    if tail:
        pieces.append((start, len(text), tail))
    return pieces


def _dates_in(sentence: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    cleaned = _URL.sub(" ", sentence)
    for pattern in _EXPLICIT:
        for match in pattern.finditer(cleaned):
            iso = _to_iso(match.group(1), match.group(2), match.group(3))
            if iso:
                found.append((iso, match.start()))
    found.sort(key=lambda item: item[1])
    return found


def _to_iso(year: str, month: str, day: str) -> str | None:
    try:
        value = dt.date(int(year), int(month), int(day))
    except ValueError:
        return None
    return value.isoformat()


def _pick_deadline(
    sentences: list[tuple[int, int, str]],
) -> tuple[str, str] | None:
    ranked: list[tuple[int, int, str, str]] = []
    fallback: list[tuple[int, str, str]] = []
    for index, (_start, _end, sentence) in enumerate(sentences):
        dates = _dates_in(sentence)
        if not dates:
            continue
        priority = _priority_rank(sentence)
        if priority is not None:
            ranked.append((priority, index, dates[0][0], sentence))
        else:
            fallback.append((index, dates[0][0], sentence))
    if ranked:
        ranked.sort()
        _priority, _index, iso, sentence = ranked[0]
        return iso, sentence
    if fallback:
        _index, iso, sentence = fallback[0]
        return iso, sentence
    return None


def _priority_rank(sentence: str) -> int | None:
    ranks = [index for index, word in enumerate(_PRIORITY) if word in sentence]
    if not ranks:
        return None
    return min(ranks)


def _relative_sentence(sentences: list[tuple[int, int, str]]) -> str:
    for _start, _end, sentence in sentences:
        if _RELATIVE.search(sentence):
            return sentence
    return ""


def _title(text: str) -> str:
    match = _BOOK_TITLE.search(text)
    if match:
        return match.group(1).strip()
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:40]
    return NOT_FOUND


def _info_type(text: str) -> str:
    if any(word in text for word in ("考研", "研究生招生", "硕士研究生")):
        return "考研"
    if "实习" in text:
        return "实习"
    if any(word in text for word in ("竞赛", "大赛", "比赛", "挑战赛", "邀请赛", "赛")):
        return "竞赛"
    return NOT_FOUND


def _link(text: str) -> str:
    match = _URL.search(text)
    if not match:
        return NOT_FOUND
    return match.group(0).rstrip(".,，。")


def _condition(text: str) -> str:
    match = _CONDITION.search(text)
    if not match:
        return NOT_FOUND
    return match.group(1).strip()


def _major(text: str) -> str:
    match = _MAJOR.search(text)
    if not match:
        return NOT_FOUND
    return match.group(1).strip()
