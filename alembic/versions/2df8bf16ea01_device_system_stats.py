"""device system stats

Revision ID: 2df8bf16ea01
Revises: a7e9c4d2b6f1
Create Date: 2026-09-18T00:00:00.000000

Admin device fleet dashboard needs a live resource snapshot (CPU load,
memory, temperature, disk) alongside the existing firmware_version/
last_heartbeat_at columns. Stored as free-form JSON, same pattern as
`capabilities`, since the set of reportable stats may grow and an unknown/
unavailable field must stay absent, never a fabricated zero. Existing rows
default to an empty object until their next heartbeat.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "2df8bf16ea01"
down_revision = "a7e9c4d2b6f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("system_stats", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("devices", "system_stats")
