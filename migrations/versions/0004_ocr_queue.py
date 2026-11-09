"""识图异步化：草稿上的识别任务状态。

`ocr_state` 默认 `'done'` 是这一批唯一需要想清楚的默认值：

- 0003 之前的草稿都是同步识别完才入库的，语义上就是已完成，回填变成「什么都不用做」。
- 回滚到批次 2 的代码时，旧 INSERT 不写这一列，落库自动取 `'done'`，行为不变。

代价写在 models.py 的注释里：新代码必须显式写 `'queued'`，不能依赖默认值。

降级只丢队列状态（queued/running/failed 的进度），不动用户、记录、草稿内容和原图。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "image_drafts",
        sa.Column("ocr_state", sa.Text(), nullable=False, server_default=sa.text("'done'")),
    )
    op.add_column(
        "image_drafts",
        sa.Column("ocr_queued_at", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "image_drafts",
        sa.Column("ocr_started_at", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "image_drafts",
        sa.Column("ocr_finished_at", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "image_drafts",
        sa.Column("ocr_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "image_drafts",
        sa.Column("ocr_provider", sa.Text(), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("image_drafts", "ocr_provider")
    op.drop_column("image_drafts", "ocr_attempts")
    op.drop_column("image_drafts", "ocr_finished_at")
    op.drop_column("image_drafts", "ocr_started_at")
    op.drop_column("image_drafts", "ocr_queued_at")
    op.drop_column("image_drafts", "ocr_state")
