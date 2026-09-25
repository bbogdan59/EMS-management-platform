"""Durable late telemetry reaggregation queue.

Revision ID: f83d4b0a19c2
Revises: c72b0e951ad4
"""

import sqlalchemy as sa

from alembic import op

revision = "f83d4b0a19c2"
down_revision = "c72b0e951ad4"
branch_labels = None
depends_on = None


def upgrade():
    if "legacy_telemetry_backfill" in sa.inspect(op.get_bind()).get_table_names():
        op.rename_table("legacy_telemetry_backfill", "telemetry_backfill")
    else:
        op.create_table(
            "telemetry_backfill",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "station_id",
                sa.Uuid(),
                sa.ForeignKey("stations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("hour_start", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("station_id", "hour_start", name="uq_telemetry_backfill_hour"),
        )
        op.create_index("ix_telemetry_backfill_station_id", "telemetry_backfill", ["station_id"])


def downgrade():
    op.rename_table("telemetry_backfill", "legacy_telemetry_backfill")
