"""读取子进程输出。

约定只作用于我们启动的子进程，不改当前进程或用户的区域设置：

- Python：子进程环境里的 PYTHONIOENCODING 设为 utf-8，标准输出按 UTF-8 写。
  这样 Windows 默认 GBK 时，CLI 里的中文仍是「剩余11天」，不是转义。
- npm / node：管道输出本身是 UTF-8，按 UTF-8 严格解码。
- 真正不是 UTF-8 的字节：在当前线程抛 UnicodeDecodeError，带上位置。
  不用替换或反斜杠转义把合法中文改掉。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path


def run_text(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    if env is not None:
        child_env.update(env)
    if _is_python(args):
        child_env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        list(args),
        cwd=cwd,
        env=child_env,
        capture_output=True,
        check=False,
    )
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        decode_utf8(completed.stdout),
        decode_utf8(completed.stderr),
    )


def decode_utf8(data: bytes | None) -> str:
    if not data:
        return ""
    return data.decode("utf-8")


def _is_python(args: Sequence[str]) -> bool:
    name = Path(args[0]).name.lower()
    stem = name.split(".", 1)[0]
    return stem.startswith("python") or stem == "py"
