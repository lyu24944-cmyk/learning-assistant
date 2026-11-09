import csv
import datetime as dt
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from src.config import resolve_today
from tests.run_text import run_text
from src.extract import extract_notice, explicit_dates
from src.records import EXAMPLE_RECORDS, REAL_RECORDS, load_records, validate_records
from src.reminder import DEFAULT_RECORDS as REMINDER_RECORDS
from src.reminder import main as reminder_main
from src.reminder import render_message, select_reminders
from src.schema import NOT_FOUND, RECORD_FIELDS
from src.score_exam import load_samples, reference_predictions, score
from src.timeline import DEFAULT_RECORDS as TIMELINE_RECORDS
from src.timeline import main as timeline_main
from src.timeline import render_timeline, select_timeline

ROOT = Path(__file__).resolve().parents[1]


class ExtractTest(unittest.TestCase):
    def test_reference_exam_passes(self) -> None:
        samples = load_samples(ROOT / "data" / "exam" / "samples.json")
        result = score(samples, reference_predictions(samples))
        self.assertEqual(result["explicit_date_correct"], 6, result["rows"])
        self.assertEqual(result["hallucinated_dates"], 0)
        self.assertTrue(result["passed"])

    def test_relative_guess_fails_the_whole_date_section(self) -> None:
        samples = load_samples(ROOT / "data" / "exam" / "samples.json")
        predictions = reference_predictions(samples)
        predictions["R1"] = dict(predictions["R1"])
        predictions["R1"]["截止日期"] = "2026-11-06"
        result = score(samples, predictions)
        self.assertEqual(result["hallucinated_dates"], 1)
        self.assertFalse(result["date_section_pass"])
        self.assertFalse(result["passed"])

    def test_injection_date_is_not_an_allowed_deadline(self) -> None:
        notice = next(
            item["notice"]
            for item in load_samples(ROOT / "data" / "exam" / "samples.json")
            if item["id"] == "E6"
        )
        self.assertNotIn("2099-01-01", explicit_dates(notice))
        self.assertEqual(extract_notice(notice)["截止日期"], "2026-12-03")
        self.assertEqual(extract_notice(notice)["核对状态"], "草稿")
        self.assertEqual(extract_notice(notice)["是否参与提醒"], "否")

    def test_extractor_source_has_no_delivery_tools(self) -> None:
        source = (ROOT / "src" / "extract.py").read_text(encoding="utf-8")
        for banned in ("urllib", "requests", "smtplib", "socket", "subprocess"):
            self.assertNotIn(banned, source)


class RecordTest(unittest.TestCase):
    def test_example_table_has_ten_valid_rows(self) -> None:
        rows = load_records(ROOT / "templates" / "records.example.json")
        csv_rows = load_records(ROOT / "templates" / "records.example.csv")
        self.assertEqual(rows, csv_rows)
        errors = validate_records(rows, min_rows=10, allowed_types=["竞赛", "实习", "考研"])
        self.assertEqual(errors, [])

    def test_draft_and_missing_date_are_not_reminded(self) -> None:
        rows = load_records(ROOT / "templates" / "records.example.json")
        picked = select_reminders(
            rows,
            today=dt.date(2026, 11, 8),
            info_type="竞赛",
            within_days=7,
        )
        titles = [row["标题"] for row in picked]
        self.assertEqual(
            titles,
            ["青柠数据分析赛", "北区算法邀请赛", "小城创新挑战赛", "化学实验赛", "湖边演讲赛"],
        )
        message = render_message(picked, info_type="竞赛", within_days=7)
        self.assertIn("截止日期：2026-11-08", message)
        self.assertIn("https://example.edu/contest/qingning", message)
        self.assertNotIn("草稿隔离赛", message)
        self.assertNotIn("本周五口头赛", message)
        self.assertNotIn("已放弃的编程赛", message)

    def test_empty_window_stays_quiet(self) -> None:
        rows = load_records(ROOT / "templates" / "records.example.json")
        picked = select_reminders(
            rows,
            today=dt.date(2026, 12, 31),
            info_type="竞赛",
            within_days=7,
        )
        self.assertEqual(render_message(picked, info_type="竞赛", within_days=7), "今日不发送")

    def test_other_info_type_is_ignored(self) -> None:
        rows = load_records(ROOT / "templates" / "records.example.json")
        rows[0] = dict(rows[0], 类型="实习")
        picked = select_reminders(
            rows,
            today=dt.date(2026, 11, 8),
            info_type="竞赛",
            within_days=7,
        )
        self.assertNotIn("青柠数据分析赛", [row["标题"] for row in picked])
        self.assertTrue(all(row["截止日期"] != NOT_FOUND for row in picked))


class SettingsTest(unittest.TestCase):
    def test_default_type_is_contest(self) -> None:
        settings = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["info_type"], "竞赛")
        self.assertEqual(settings["reminder_within_days"], 7)
        self.assertEqual(settings["timezone"], "Asia/Shanghai")

    def test_missing_timezone_data_uses_local_today(self) -> None:
        with patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("Asia/Shanghai")):
            self.assertEqual(resolve_today(None, "Asia/Shanghai"), dt.date.today())
        self.assertEqual(resolve_today("2026-11-02", "No/Such"), dt.date(2026, 11, 2))


class RealDataTest(unittest.TestCase):
    def test_defaults_read_real_records_not_examples(self) -> None:
        self.assertEqual(REMINDER_RECORDS, REAL_RECORDS)
        self.assertEqual(TIMELINE_RECORDS, REAL_RECORDS)
        self.assertEqual(EXAMPLE_RECORDS, ROOT / "templates" / "records.example.csv")
        self.assertTrue(REAL_RECORDS.is_file())

    def test_sources_are_three_public_contest_pages(self) -> None:
        path = ROOT / "templates" / "sources.csv"
        template_header = (ROOT / "templates" / "sources.template.csv").read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(path.read_text(encoding="utf-8").splitlines()[0], template_header)
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [row["网址"] for row in rows],
            [
                "http://www.apmcm.org",
                "https://www.ncccu.org.cn/index/Paper/case4.html",
                "https://www.ncccu.org.cn/index/Paper/case1.html",
            ],
        )
        for row in rows:
            self.assertEqual(row["类型"], "竞赛")
            self.assertEqual(row["更新频率"], "不定期")
            self.assertEqual(row["是否需要登录"], "否")
            self.assertEqual(row["内容在哪里"], "网页")
            self.assertEqual(row["本月录入方式"], "只手动粘贴")
            self.assertEqual(row["我能否打开"], "能打开")
            self.assertTrue(row["来源名称"].strip())
            self.assertTrue(row["备注"].strip())

    def test_real_records_validate(self) -> None:
        text = REAL_RECORDS.read_text(encoding="utf-8")
        self.assertEqual(text.splitlines()[0], ",".join(RECORD_FIELDS))
        rows = load_records(REAL_RECORDS)
        errors = validate_records(rows, min_rows=3, allowed_types=["竞赛", "实习", "考研"])
        self.assertEqual(errors, [])
        verified = [
            row
            for row in rows
            if row["类型"] == "竞赛" and row["核对状态"] == "已核对" and row["截止日期"] != NOT_FOUND
        ]
        self.assertGreaterEqual(len(verified), 3)
        by_title = {row["标题"]: row for row in rows}
        self.assertEqual(by_title["计挑赛人工智能挑战赛-区域赛作品提交截止"]["截止日期"], "2026-11-13")
        self.assertEqual(
            by_title["计挑赛人工智能挑战赛-区域赛作品提交截止"]["来源链接"],
            "https://www.ncccu.org.cn/index/Paper/case1.html",
        )
        self.assertEqual(by_title["计挑赛程序设计挑战赛-区域赛报名截止"]["截止日期"], "2026-11-25")
        self.assertEqual(
            by_title["计挑赛程序设计挑战赛-区域赛报名截止"]["来源链接"],
            "https://www.ncccu.org.cn/index/Paper/case4.html",
        )
        self.assertEqual(by_title["APMCM2026英文赛项-报名截止"]["截止日期"], "2026-11-25")
        self.assertEqual(
            by_title["APMCM2026英文赛项-报名截止"]["来源链接"],
            "https://newoaa.shisu.edu.cn/3d/92/c6353a212370/page.htm",
        )
        self.assertEqual(
            by_title["APMCM2026英文赛项-报名截止"]["日期原句"],
            "报名时间：即日起至2026年11月25日（星期三）",
        )
        exam = by_title["计挑赛程序设计挑战赛-区域赛考试"]
        self.assertEqual(exam["截止日期"], "2026-11-29")
        self.assertEqual(exam["来源链接"], "https://www.ncccu.org.cn/index/Paper/case4.html")
        self.assertEqual(exam["日期原句"], "区域赛/省赛考试：2026年11月29日")
        self.assertEqual(exam["适合专业"], "高校所有专业在校生")
        self.assertEqual(exam["核对状态"], "已核对")
        self.assertEqual(exam["是否参与提醒"], "是")
        for row in verified:
            self.assertEqual(row["是否参与提醒"], "是")
            self.assertEqual(row["更新时间"], "2026-11-02")
        for title in (
            "计挑赛人工智能挑战赛-区域赛作品提交截止",
            "计挑赛程序设计挑战赛-区域赛报名截止",
            "APMCM2026英文赛项-报名截止",
        ):
            self.assertEqual(by_title[title]["适合专业"], "未找到")

    def test_reminder_on_2026_11_02_is_quiet(self) -> None:
        rows = load_records(REAL_RECORDS)
        picked = select_reminders(
            rows,
            today=dt.date(2026, 11, 2),
            info_type="竞赛",
            within_days=7,
        )
        self.assertEqual(picked, [])
        self.assertEqual(render_message(picked, info_type="竞赛", within_days=7), "今日不发送")

    def test_reminder_includes_ai_contest_inside_seven_days(self) -> None:
        rows = load_records(REAL_RECORDS)
        picked = select_reminders(
            rows,
            today=dt.date(2026, 11, 8),
            info_type="竞赛",
            within_days=7,
        )
        self.assertEqual(
            [row["标题"] for row in picked],
            ["计挑赛人工智能挑战赛-区域赛作品提交截止"],
        )

    def test_timeline_sorts_and_counts_remaining_days(self) -> None:
        rows = load_records(REAL_RECORDS)
        today = dt.date(2026, 11, 2)
        items = select_timeline(rows, info_type="竞赛")
        text = render_timeline(items, today=today, info_type="竞赛")
        titles = [row["标题"] for _due, row in items]
        self.assertEqual(
            titles,
            [
                "计挑赛人工智能挑战赛-区域赛作品提交截止",
                "APMCM2026英文赛项-报名截止",
                "计挑赛程序设计挑战赛-区域赛报名截止",
                "计挑赛程序设计挑战赛-区域赛考试",
            ],
        )
        self.assertIn("剩余11天", text)
        self.assertIn("剩余23天", text)
        self.assertIn("剩余27天", text)
        self.assertNotIn("已过期", text)
        self.assertLess(text.index("计挑赛人工智能"), text.index("APMCM2026"))
        self.assertLess(text.index("APMCM2026"), text.index("计挑赛程序设计"))

    def test_timeline_marks_expired_and_skips_unusable_rows(self) -> None:
        rows = load_records(REAL_RECORDS)
        relative = dict(rows[0])
        relative["标题"] = "相对日期不应出现"
        relative["截止日期"] = NOT_FOUND
        relative["是否参与提醒"] = "否"
        draft = dict(rows[0])
        draft["标题"] = "草稿不应出现"
        draft["核对状态"] = "草稿"
        draft["是否参与提醒"] = "否"
        other = dict(rows[0])
        other["标题"] = "实习不应出现"
        other["类型"] = "实习"
        today = dt.date(2026, 11, 14)
        items = select_timeline([*rows, relative, draft, other], info_type="竞赛")
        text = render_timeline(items, today=today, info_type="竞赛")
        titles = [row["标题"] for _due, row in items]
        self.assertNotIn("相对日期不应出现", titles)
        self.assertNotIn("草稿不应出现", titles)
        self.assertNotIn("实习不应出现", titles)
        self.assertIn("2026-11-13（已过期）", text)
        self.assertIn("2026-11-25（剩余11天）", text)

    def test_cli_runs_without_feishu(self) -> None:
        self.assertEqual(timeline_main(["--today", "2026-11-02"]), 0)
        self.assertEqual(reminder_main(["--today", "2026-11-02"]), 0)
        commands = [
            [sys.executable, "-m", "src.timeline", "--today", "2026-11-02"],
            [sys.executable, "-m", "src.reminder", "--today", "2026-11-02"],
            [sys.executable, "-m", "src.validate_records", "data/records.csv", "--min-rows", "3"],
        ]
        for command in commands:
            result = run_text(command, cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        timeline = run_text(commands[0], cwd=ROOT)
        self.assertIn("剩余11天", timeline.stdout)
        self.assertIn("http://www.apmcm.org", (ROOT / "templates" / "sources.csv").read_text(encoding="utf-8"))
        reminder = run_text(commands[1], cwd=ROOT)
        self.assertEqual(reminder.stdout.strip(), "今日不发送")

    def test_cli_chinese_survives_gbk_parent_encoding(self) -> None:
        before = os.environ.get("PYTHONIOENCODING")
        code = "print('剩余11天')"
        raw = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "gbk"},
            check=False,
        )
        self.assertEqual(raw.stdout.decode("gbk").strip(), "剩余11天")
        with self.assertRaises(UnicodeDecodeError):
            raw.stdout.decode("utf-8")
        fixed = run_text(
            [sys.executable, "-m", "src.timeline", "--today", "2026-11-02"],
            cwd=ROOT,
            env={"PYTHONIOENCODING": "gbk"},
        )
        self.assertEqual(os.environ.get("PYTHONIOENCODING"), before)
        self.assertEqual(fixed.returncode, 0, fixed.stderr or fixed.stdout)
        self.assertIn("剩余11天", fixed.stdout)
        self.assertNotIn("\\x", fixed.stdout)


if __name__ == "__main__":
    unittest.main()
