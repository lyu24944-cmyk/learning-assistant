"""给 8 段抽取考试打分，并把结果写入表格。

通过线（日期项）：
- 6 段明确公历日期里，截止日期至少对 5 段
- 2 段相对日期或没有公历日期的样本，只要出现一个公历日期，日期项整项失败
- 输出里的公历日期必须能在原文的明确日期中找到，否则也算猜测
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from src.extract import explicit_dates, extract_notice
from src.records import write_csv
from src.schema import DRAFT, NOT_FOUND, REMIND_NO

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLES = ROOT / "data" / "exam" / "samples.json"
DEFAULT_RESULTS_JSON = ROOT / "data" / "exam" / "results.json"
DEFAULT_RESULTS_CSV = ROOT / "data" / "exam" / "results.csv"

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")
RESULT_FIELDS = (
    "样本编号",
    "种类",
    "截止日期对",
    "猜测日期",
    "标题对",
    "类型对",
    "链接对",
    "日期原句对",
    "报名条件对",
    "默认草稿",
    "期望截止日期",
    "实际截止日期",
    "实际日期原句",
)


def load_samples(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    samples = data["samples"]
    explicit = [item for item in samples if item["kind"] == "explicit"]
    relative = [item for item in samples if item["kind"] == "relative"]
    if len(samples) != 8 or len(explicit) != 6 or len(relative) != 2:
        raise ValueError("考试必须是 8 段：6 段明确日期 + 2 段相对或缺失日期。")
    return samples


def score(samples: list[dict], predictions: dict[str, dict]) -> dict:
    rows = []
    for sample in samples:
        output = predictions.get(sample["id"])
        if output is None:
            raise KeyError(f"缺少样本 {sample['id']} 的抽取结果。")
        rows.append(_score_one(sample, output))
    explicit_rows = [row for row in rows if row["种类"] == "explicit"]
    explicit_correct = sum(1 for row in explicit_rows if row["截止日期对"] == "是")
    hallucinations = sum(1 for row in rows if row["猜测日期"] == "是")
    date_section_pass = explicit_correct >= 5 and hallucinations == 0
    link_section_pass = all(row["链接对"] == "是" for row in rows)
    draft_default_pass = all(row["默认草稿"] == "是" for row in rows)
    passed = date_section_pass and link_section_pass and draft_default_pass
    return {
        "passed": passed,
        "date_section_pass": date_section_pass,
        "explicit_date_correct": explicit_correct,
        "explicit_date_total": len(explicit_rows),
        "hallucinated_dates": hallucinations,
        "link_section_pass": link_section_pass,
        "draft_default_pass": draft_default_pass,
        "rule": "明确日期至少 5/6，且猜测日期数为 0；链接必须来自原文；抽取结果必须是草稿且不参与提醒。",
        "rows": rows,
    }


def reference_predictions(samples: list[dict]) -> dict[str, dict]:
    return {sample["id"]: extract_notice(sample["notice"]) for sample in samples}


def write_results(result: dict, json_path: Path, csv_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    write_csv(csv_path, result["rows"], RESULT_FIELDS)


def _score_one(sample: dict, output: dict) -> dict[str, str]:
    expected = sample["expected"]
    notice = sample["notice"]
    actual_deadline = str(output.get("截止日期", "")).strip()
    iso_in_output = _ISO.findall(actual_deadline)
    allowed = set(explicit_dates(notice))
    hallucinated = any(iso not in allowed for iso in iso_in_output) or (
        expected["截止日期"] == NOT_FOUND and bool(iso_in_output)
    )
    deadline_ok = (not hallucinated) and actual_deadline == expected["截止日期"]
    quote = str(output.get("日期原句", "")).strip()
    return {
        "样本编号": sample["id"],
        "种类": sample["kind"],
        "截止日期对": _yes(deadline_ok),
        "猜测日期": _yes(hallucinated),
        "标题对": _yes(_title_ok(expected["标题"], output.get("标题", ""))),
        "类型对": _yes(str(output.get("类型", "")).strip() == expected["类型"]),
        "链接对": _yes(_link_ok(notice, expected["来源链接"], output.get("来源链接", ""))),
        "日期原句对": _yes(_quote_ok(notice, expected["日期原句包含"], quote)),
        "报名条件对": _yes(_condition_ok(notice, expected["报名条件"], output.get("报名条件", ""))),
        "默认草稿": _yes(
            str(output.get("核对状态", "")).strip() == DRAFT
            and str(output.get("是否参与提醒", "")).strip() == REMIND_NO
        ),
        "期望截止日期": expected["截止日期"],
        "实际截止日期": actual_deadline,
        "实际日期原句": quote,
    }


def _title_ok(expected: str, actual: object) -> bool:
    text = str(actual).strip()
    return bool(text) and (text == expected or expected in text)


def _link_ok(notice: str, expected: str, actual: object) -> bool:
    text = str(actual).strip()
    if text == NOT_FOUND:
        return expected == NOT_FOUND
    if text not in notice:
        return False
    return text == expected


def _quote_ok(notice: str, must_contain: str, actual: str) -> bool:
    if not actual or actual == NOT_FOUND:
        return False
    compact_notice = _compact(notice)
    if _compact(actual) not in compact_notice:
        return False
    return _compact(must_contain) in _compact(actual)


def _condition_ok(notice: str, expected: str, actual: object) -> bool:
    text = str(actual).strip()
    if expected == NOT_FOUND:
        return text == NOT_FOUND
    if not text or text == NOT_FOUND:
        return False
    return text in notice and (expected in text or text in expected)


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _yes(flag: bool) -> str:
    return "是" if flag else "否"


def _load_predictions(path: Path, samples: list[dict]) -> dict[str, dict]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict) and "samples" in data:
        data = data["samples"]
    mapping: dict[str, dict] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            mapping[str(key)] = value.get("output", value)
        return mapping
    for item in data:
        mapping[str(item["id"])] = item.get("output", item)
    missing = [sample["id"] for sample in samples if sample["id"] not in mapping]
    if missing:
        raise KeyError("预测文件缺少样本：" + "、".join(missing))
    return mapping


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行 8 段抽取考试并写结果表")
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--predictions", type=Path, default=None, help="Coze/Dify 导出的 JSON。省略则用本地纯函数。")
    parser.add_argument("--json", type=Path, default=DEFAULT_RESULTS_JSON)
    parser.add_argument("--csv", type=Path, default=DEFAULT_RESULTS_CSV)
    args = parser.parse_args(argv)
    samples = load_samples(args.samples)
    predictions = (
        reference_predictions(samples)
        if args.predictions is None
        else _load_predictions(args.predictions, samples)
    )
    result = score(samples, predictions)
    source = "本地纯函数" if args.predictions is None else str(args.predictions)
    result["source"] = source
    write_results(result, args.json, args.csv)
    print(
        f"来源：{source}\n"
        f"日期项：{'通过' if result['date_section_pass'] else '失败'} "
        f"（明确日期 {result['explicit_date_correct']}/{result['explicit_date_total']}，"
        f"猜测 {result['hallucinated_dates']}）\n"
        f"链接项：{'通过' if result['link_section_pass'] else '失败'}\n"
        f"默认草稿：{'通过' if result['draft_default_pass'] else '失败'}\n"
        f"总评：{'通过' if result['passed'] else '失败'}\n"
        f"结果表：{args.csv}"
    )
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
