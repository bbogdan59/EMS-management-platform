"""Retain independent Deye grid voltage observations.

Revision ID: b322c222d903
Revises: b321c221d902
"""

import sqlalchemy as sa

from alembic import op

revision = "b322c222d903"
down_revision = "b321c221d902"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("legacy_grid_voltage_samples"):
        op.rename_table("legacy_grid_voltage_samples", "grid_voltage_samples")
        return
    op.create_table(
        "grid_voltage_samples",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "station_id",
            sa.Uuid(),
            sa.ForeignKey("stations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("l1_v", sa.Numeric(14, 4)),
        sa.Column("l2_v", sa.Numeric(14, 4)),
        sa.Column("l3_v", sa.Numeric(14, 4)),
        sa.Column("reported_phases", sa.JSON(), nullable=False),
        sa.UniqueConstraint("station_id", "source_id", "measured_at", name="uq_grid_voltage_time"),
    )
    op.create_index(
        "ix_grid_voltage_station_source_time",
        "grid_voltage_samples",
        ["station_id", "source_id", "measured_at"],
    )
    op.create_index("ix_grid_voltage_samples_measured_at", "grid_voltage_samples", ["measured_at"])


def downgrade():
    op.rename_table("grid_voltage_samples", "legacy_grid_voltage_samples")
