"""task execution audit

Revision ID: 9c14e7b2a5d0
Revises: a7e9c4d2b6f1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9c14e7b2a5d0"
down_revision: str | None = "a7e9c4d2b6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_executions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("celery_task_id", sa.String(64), nullable=False),
        sa.Column("task_name", sa.String(200), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("triggered_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["triggered_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("celery_task_id", "task_name", "source", "status"):
        op.create_index(f"ix_task_executions_{column}", "task_executions", [column], unique=column == "celery_task_id")


def downgrade() -> None:
    op.drop_table("task_executions")
