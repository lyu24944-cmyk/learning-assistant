"""python3 -m src 时打印可用命令。"""

from src.config import load_settings

COMMANDS = """
可用命令（在仓库根目录执行，不需要飞书）：

  python3 -m src.timeline
  python3 -m src.timeline --today 2026-11-02
  python3 -m src.reminder --today 2026-11-02
  python3 -m uvicorn src.api:app --host 127.0.0.1 --port 8010
  python3 -m src.validate_records data/records.csv --min-rows 3
  python3 -m src.validate_records
  python3 -m src.score_exam
  python3 -m src.export_feishu
""".strip()


def main() -> None:
    settings = load_settings()
    print(f"当前信息类型：{settings['info_type']}（改 config/settings.json）")
    print(COMMANDS)


if __name__ == "__main__":
    main()
