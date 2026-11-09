"""前端源码约束和一次生产构建。不读用户数据库。"""

from __future__ import annotations

import shutil
import sys
import unittest
from pathlib import Path

from tests.run_text import decode_utf8, run_text

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "frontend" / "src" / "App.jsx"
CLIENT = ROOT / "frontend" / "src" / "api" / "client.js"
CSS = ROOT / "frontend" / "src" / "styles.css"


class FrontendSourceTest(unittest.TestCase):
    def test_pages_and_safety_markers_are_present(self) -> None:
        app = APP.read_text(encoding="utf-8")
        client = CLIENT.read_text(encoding="utf-8")
        css = CSS.read_text(encoding="utf-8")
        self.assertNotIn("readJson(fetch(", app)
        self.assertIn("X-CSRF-Token", client)
        self.assertIn("apiFetch", app)
        self.assertIn("账号与安全", app)
        self.assertIn("重置密码", app)
        self.assertIn("minLength={8}", app)
        self.assertIn('window.confirm("确定删除这条私人记录？', app)
        self.assertIn("复制今日待提醒", app)
        self.assertIn("导出纯文本", app)
        self.assertIn("没有发送邮件", app)
        self.assertIn('to="/settings/reminders"', app)
        self.assertIn('to="/mine"', app)
        self.assertIn('to="/review"', app)
        self.assertIn('to="/courses"', app)
        self.assertIn('to="/materials"', app)
        self.assertIn('to="/recognize"', app)
        self.assertIn("确认写入我的记录", app)
        self.assertIn("人工补录，不是识别结果", app)
        self.assertIn("title={item.来源链接}", app)
        self.assertIn("\n          来源\n", app)
        self.assertIn("已过期，建议下线", app)
        self.assertIn("min-width: 0", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn(".card-row a", css)

    def test_recognize_page_polls_instead_of_blocking(self) -> None:
        app = APP.read_text(encoding="utf-8")
        self.assertIn("ocr_state", app)
        self.assertIn("poll_after_ms", app)
        # 递归 setTimeout 而不是 setInterval：请求比间隔慢时不会堆积。
        self.assertIn("setTimeout(tick", app)
        self.assertNotIn("setInterval(tick", app)
        self.assertIn("revokeObjectURL", app)
        self.assertIn("重新识别", app)
        self.assertIn("识别中，稍等", app)
        self.assertIn("已等 ${elapsed} 秒", app)
        self.assertIn("queue_position", app)
        self.assertIn("前面还有", app)
        # 防双击：请求还没回来时输入框禁用，onFile 开头再兜一句。
        self.assertIn("disabled={uploading}", app)
        self.assertIn("if (!file || uploading)", app)
        # 准入被拒的三种文案分开，不能一个「失败了」打天下。
        self.assertIn("draft_busy", app)
        self.assertIn("upload_rate_limited", app)
        self.assertIn("ocr_queue_full", app)

    def test_frontend_production_build(self) -> None:
        npm = _npm_command()
        install = run_text([*npm, "install"], cwd=ROOT / "frontend")
        self.assertEqual(install.returncode, 0, install.stderr or install.stdout)
        build = run_text([*npm, "run", "build"], cwd=ROOT / "frontend")
        self.assertEqual(build.returncode, 0, build.stderr or build.stdout)
        self.assertTrue((ROOT / "frontend" / "dist" / "index.html").is_file())

    def test_npm_resolver_uses_the_real_executable(self) -> None:
        found = _npm_command()
        self.assertTrue(found[0])
        self.assertTrue(Path(found[0]).is_file(), found)
        self.assertNotEqual(found, ["npm"])


    def test_node_chinese_stdout_stays_chinese(self) -> None:
        node = shutil.which("node")
        if not node:
            raise AssertionError("PATH 里找不到 node。构建输出按 UTF-8 读取，中文不能被转义。")
        result = run_text([node, "-e", "process.stdout.write('剩余11天')"])
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("剩余11天", result.stdout)
        self.assertNotIn("\\x", result.stdout)

    def test_utf8_text_containing_byte_93_survives_where_gbk_fails(self) -> None:
        # 合法 UTF-8。位置 110 的 0x93 会让 GBK 读取线程崩溃，按 UTF-8 必须得到原字符。
        character = "ࠓ"
        raw = b"A" * 108 + character.encode("utf-8")
        self.assertEqual(raw[110], 0x93)
        with self.assertRaises(UnicodeDecodeError) as caught:
            raw.decode("gbk")
        self.assertIn("0x93", str(caught.exception))
        self.assertEqual(decode_utf8(raw), "A" * 108 + character)

    def test_illegal_utf8_raises_in_the_caller(self) -> None:
        code = "import sys; sys.stdout.buffer.write(b'A' * 110 + bytes([0x93]))"
        with self.assertRaises(UnicodeDecodeError) as caught:
            run_text([sys.executable, "-c", code])
        self.assertEqual(caught.exception.start, 110)


def _npm_command() -> list[str]:
    npm = shutil.which("npm")
    if not npm:
        raise AssertionError("PATH 里找不到 npm。Windows 上需要解析到 npm.cmd，不能直接写 npm。")
    return [npm]


if __name__ == "__main__":
    unittest.main()
