"""密码策略：长度、常见弱密码、邮箱本地部分。

不联网，不引第三方强度库。表是数据文件，随时可以加行。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

MIN_LENGTH = 10
MAX_LENGTH = 128
_WEAK_FILE = Path(__file__).with_name("data") / "weak_passwords.txt"


class PasswordError(ValueError):
    pass


@lru_cache(maxsize=1)
def weak_passwords() -> frozenset[str]:
    text = _WEAK_FILE.read_text(encoding="utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


def validate_password(password: str, email: str = "") -> None:
    if len(password) < MIN_LENGTH:
        raise PasswordError(f"密码至少 {MIN_LENGTH} 位。")
    if len(password) > MAX_LENGTH:
        raise PasswordError(f"密码最多 {MAX_LENGTH} 位。")
    lowered = password.lower()
    if lowered in weak_passwords():
        raise PasswordError("这个密码太常见，换一个。")
    local = email.split("@", 1)[0].strip().lower()
    if local and lowered == local:
        raise PasswordError("密码不能和邮箱前半段一样。")
