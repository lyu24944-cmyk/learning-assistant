"""记录与抽取结果的字段约定。"""

from __future__ import annotations

NOT_FOUND = "未找到"
DRAFT = "草稿"
VERIFIED = "已核对"
REMIND_YES = "是"
REMIND_NO = "否"

INFO_TYPES = ("竞赛", "实习", "考研")
STATUSES = ("未处理", "已报名", "已交", "放弃", "已放弃")
MINE_STATUSES = ("未处理", "已报名", "已交", "放弃")
REVIEW_STATES = (DRAFT, VERIFIED)
REMIND_FLAGS = (REMIND_YES, REMIND_NO)
DEADLINE_MARKS = ("已识别", NOT_FOUND)

RECORD_FIELDS = (
    "标题",
    "类型",
    "截止日期",
    "来源链接",
    "更新时间",
    "适合专业",
    "状态",
    "核对状态",
    "日期原句",
    "是否参与提醒",
    "报名条件",
)

EXTRACT_FIELDS = (
    "标题",
    "类型",
    "截止日期",
    "来源链接",
    "报名条件",
    "日期原句",
    "适合专业",
    "核对状态",
    "是否参与提醒",
)

FEISHU_IMPORT_FIELDS = (
    "标题",
    "类型",
    "截止日期",
    "截止日期标注",
    "来源链接",
    "更新时间",
    "适合专业",
    "状态",
    "核对状态",
    "日期原句",
    "是否参与提醒",
    "报名条件",
)
