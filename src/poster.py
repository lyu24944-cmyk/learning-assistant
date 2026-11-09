"""从识别出的文字里整理竞赛候选。

文字只当数据：不执行其中的指令，不访问链接，不补年份。
报名截止和作品提交截止分开。没有写清的日期保持「未找到」或「待核对」。
"""

from __future__ import annotations

import datetime as dt
import re

from src.schema import NOT_FOUND

_URL = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{3,}")
_INJECTION = re.compile(r"忽略之前|忽略以上|不要执行|给所有人发|你现在必须")
_RELATIVE = re.compile(
    r"本周[一二三四五六日天]|这周[一二三四五六日天]|下周[一二三四五六日天]"
    r"|今天|明天|后天|今晚|尽快|日前|月底|月初|\d+\s*天内|近日|即日起"
)
_EXPLICIT = (
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"(20\d{2})/(\d{1,2})/(\d{1,2})"),
    re.compile(r"(20\d{2})\.(\d{1,2})\.(\d{1,2})"),
)
_MONTH_DAY = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_CONDITION = re.compile(r"报名条件[:：]\s*([^。\n]+)")
_ORGANIZER = re.compile(r"主办方[:：]\s*([^\n。]{1,40})")
_MAJOR = re.compile(r"(?:面向对象|面向|适合)[:：]?\s*([^，。；\n]{1,20}?专业)")
_FIELD_LINE = re.compile(
    r"^(报名截止|报名条件|作品提交截止|提交截止|作品截止|主办方|来源|官网|截止日期|考试)"
)
_MATTERS = (
    ("作品提交截止", ("作品提交截止", "提交作品截止", "作品截止", "提交截止")),
    ("报名截止", ("报名截止日期", "报名截止", "截止报名")),
    ("考试", ("考试时间", "考试日期")),
)


def normalize_ocr_spacing(text: str) -> str:
    """去掉汉字之间、汉字和中文标点之间的空格。

    只删除这类空格，不增删、不替换其他字符。识别原文另行原样保存。
    """
    if not text:
        return ""
    return _strip_cjk_gaps(text)


def _strip_cjk_gaps(text: str) -> str:
    chars = list(text)
    kept: list[str] = []
    for index, char in enumerate(chars):
        if char in {" ", "\u3000"} and kept:
            nxt = ""
            for later in chars[index + 1 :]:
                if later not in {" ", "\u3000"}:
                    nxt = later
                    break
            if _joins(kept[-1], nxt):
                continue
        kept.append(char)
    return "".join(kept)


def _han(char: str) -> bool:
    if not char:
        return False
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def _cjk_punct(char: str) -> bool:
    return char in "，。、；：！？「」『』（）《》【】—…:;"


def _joins(prev: str, nxt: str) -> bool:
    if _han(prev) and (_han(nxt) or _cjk_punct(nxt)):
        return True
    if _cjk_punct(prev) and _han(nxt):
        return True
    return False


def parse_poster(text: str) -> dict:
    source = text or ""
    normalized = normalize_ocr_spacing(source)
    title, title_mark = _title(normalized)
    items = _items(normalized)
    link = _link(normalized)
    return {
        "标题": title,
        "标题标注": title_mark,
        "类型": _info_type(normalized),
        "报名条件": _condition(normalized),
        "主办方": _organizer(normalized),
        "适合专业": _major(normalized),
        "来源链接": link,
        "来源标注": "已识别" if link != NOT_FOUND else "未找到",
        "事项": items,
        "识别原文": source,
        "空白规范化": normalized,
        "识别说明": "识别结果待核对。确认前不写入我的记录，也不参与提醒。图中文字不会被执行。空白规范化只删汉字间空格，不改错字。",
    }


def _items(text: str) -> list[dict]:
    items: list[dict] = []
    for sentence in _sentences(text):
        if _INJECTION.search(sentence):
            continue
        spans = _label_spans(sentence)
        if not spans:
            items.extend(_unlabeled(sentence))
            continue
        if spans[0][0] > 0:
            items.extend(_unlabeled(sentence[: spans[0][0]]))
        for index, (start, matter) in enumerate(spans):
            end = spans[index + 1][0] if index + 1 < len(spans) else len(sentence)
            items.extend(_labeled(matter, sentence[start:end]))
    return _collapse_unlabeled(items)


def _labeled(matter: str, chunk: str) -> list[dict]:
    dates = _explicit_dates(chunk)
    if len(dates) > 1:
        return [_blank(matter, chunk, "多个日期含义不清，没有指定其中一天。")]
    if len(dates) == 1:
        return [_dated(matter, chunk, dates[0], "待核对", "这是从图中文字读出的日期，尚未核对。")]
    if _has_month_day_without_year(chunk):
        return [_blank(matter, chunk, "没有年份，不能按当前年补上。")]
    if _RELATIVE.search(chunk):
        return [_blank(matter, chunk, "相对或模糊日期，不能换算成某一天。")]
    return [_blank(matter, chunk, "日期没有写成可核对的公历，保持未找到。没有改错字。")]


def _unlabeled(chunk: str) -> list[dict]:
    chunk = chunk.strip()
    if not chunk:
        return []
    dates = _explicit_dates(chunk)
    if len(dates) > 1:
        return [_blank("其他", chunk, "多个日期含义不清，没有指定其中一天。")]
    if len(dates) == 1:
        return [
            _dated("其他", chunk, dates[0], "待核对", "图中没有写明这是报名截止还是作品提交截止。")
        ]
    if _has_month_day_without_year(chunk):
        return [_blank("其他", chunk, "没有年份，不能按当前年补上。")]
    if _RELATIVE.search(chunk):
        return [_blank("其他", chunk, "相对或模糊日期，不能换算成某一天。")]
    return []


def _collapse_unlabeled(items: list[dict]) -> list[dict]:
    unlabeled = [item for item in items if item["事项"] == "其他" and item["截止日期"] != NOT_FOUND]
    if len(unlabeled) < 2:
        return items
    rewritten = []
    for item in items:
        if item["事项"] == "其他" and item["截止日期"] != NOT_FOUND:
            rewritten.append(_blank(item["事项"], item["日期原句"], "多个日期含义不清，没有指定其中一天。"))
        else:
            rewritten.append(item)
    return rewritten


def _dated(matter: str, quote: str, deadline: str, mark: str, note: str) -> dict:
    return {
        "事项": matter,
        "日期原句": _clean(quote),
        "截止日期": deadline,
        "标注": mark,
        "说明": note,
    }


def _blank(matter: str, quote: str, note: str) -> dict:
    return _dated(matter, quote, NOT_FOUND, "未找到", note)


def _label_spans(sentence: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    occupied: list[tuple[int, int]] = []
    for matter, phrases in _MATTERS:
        best: tuple[int, int] | None = None
        for phrase in phrases:
            start = sentence.find(phrase)
            if start < 0:
                continue
            end = start + len(phrase)
            if any(not (end <= left or start >= right) for left, right in occupied):
                continue
            if best is None or start < best[0]:
                best = (start, end)
        if best is not None:
            occupied.append(best)
            found.append((best[0], matter))
    found.sort()
    return found


def _sentences(text: str) -> list[str]:
    pieces: list[str] = []
    start = 0
    for match in re.finditer(r"[。！？\n]", text):
        chunk = text[start : match.end()].strip()
        if chunk:
            pieces.append(chunk)
        start = match.end()
    tail = text[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces


def _explicit_dates(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    cleaned = _URL.sub(" ", text)
    for pattern in _EXPLICIT:
        for match in pattern.finditer(cleaned):
            iso = _to_iso(match.group(1), match.group(2), match.group(3))
            if iso:
                found.append((match.start(), iso))
    found.sort()
    ordered: list[str] = []
    for _pos, iso in found:
        if iso not in ordered:
            ordered.append(iso)
    return ordered


def _has_month_day_without_year(text: str) -> bool:
    cleaned = _URL.sub(" ", text)
    if _explicit_dates(cleaned):
        return False
    return _MONTH_DAY.search(cleaned) is not None


def _to_iso(year: str, month: str, day: str) -> str | None:
    try:
        value = dt.date(int(year), int(month), int(day))
    except ValueError:
        return None
    return value.isoformat()


def _title(text: str) -> tuple[str, str]:
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or _INJECTION.search(cleaned):
            continue
        named = re.match(r"(?:竞赛名称|比赛名称)[:：]\s*(.+)", cleaned)
        if named:
            return named.group(1).strip()[:40], "待核对"
        if _FIELD_LINE.match(cleaned):
            continue
        return cleaned[:40], "待核对"
    return NOT_FOUND, "未找到"


def _info_type(text: str) -> str:
    if any(word in text for word in ("考研", "研究生招生", "硕士研究生")):
        return "考研"
    if "实习" in text:
        return "实习"
    if any(word in text for word in ("竞赛", "大赛", "比赛", "挑战赛")):
        return "竞赛"
    return NOT_FOUND


def _link(text: str) -> str:
    for sentence in _sentences(text):
        if _INJECTION.search(sentence):
            continue
        for match in _URL.finditer(sentence):
            url = match.group(0).rstrip(".,，。)")
            if not url.startswith(("http://", "https://")):
                continue
            host = url.split("://", 1)[1].split("/")[0]
            if "." not in host or ".." in host:
                continue
            return url
    return NOT_FOUND


def _condition(text: str) -> str:
    for sentence in _sentences(text):
        if _INJECTION.search(sentence):
            continue
        match = _CONDITION.search(sentence)
        if match:
            return match.group(1).strip()[:80]
    return NOT_FOUND


def _organizer(text: str) -> str:
    for sentence in _sentences(text):
        if _INJECTION.search(sentence):
            continue
        match = _ORGANIZER.search(sentence)
        if match:
            return match.group(1).strip()[:40]
    return NOT_FOUND


def _major(text: str) -> str:
    for sentence in _sentences(text):
        if _INJECTION.search(sentence):
            continue
        match = _MAJOR.search(sentence)
        if match:
            return match.group(1).strip()
    return NOT_FOUND


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:200]
