"""device log entries

Revision ID: c71beb5be2ea
Revises: 2df8bf16ea01
Create Date: 2026-09-18T00:30:00.000000

Compact per-device debug log (device configuration page): a device may POST
short warning/error lines (`code`/`detail`, no stack traces or payloads) for
live debugging. Pruned to the last 10 days on every ingest -- this is not an
audit trail, so no long-term retention concern here, but the index on
(device_id, occurred_at) keeps both the ingest-time prune and the page's
read query cheap.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c71beb5be2ea"
down_revision = "2df8bf16ea01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_log_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("detail", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_device_log_entries_device_id"), "device_log_entries", ["device_id"], unique=False)
    op.create_index(op.f("ix_device_log_entries_occurred_at"), "device_log_entries", ["occurred_at"], unique=False)
    op.create_index(
        "ix_device_log_entries_device_id_occurred_at", "device_log_entries", ["device_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_device_log_entries_device_id_occurred_at", table_name="device_log_entries")
    op.drop_index(op.f("ix_device_log_entries_occurred_at"), table_name="device_log_entries")
    op.drop_index(op.f("ix_device_log_entries_device_id"), table_name="device_log_entries")
    op.drop_table("device_log_entries")
