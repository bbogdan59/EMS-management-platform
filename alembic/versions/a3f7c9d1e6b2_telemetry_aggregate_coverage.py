"""Coverage and nullable energy; archive UTC calendar aggregates.

Revision ID: a3f7c9d1e6b2
Revises: febfd647fd64

Existing numeric data is preserved. Legacy coverage remains unknown ({}).
UTC day/month rows are archived under separate period types, so new local
calendar rollups cannot double-count them. Consumers must handle NULL.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a3f7c9d1e6b2"
down_revision = "febfd647fd64"
branch_labels = None
depends_on = None

_ENERGY_COLUMNS = [
    "pv_energy_kwh",
    "load_energy_kwh",
    "battery_charge_energy_kwh",
    "battery_discharge_energy_kwh",
    "grid_import_energy_kwh",
    "grid_export_energy_kwh",
    "ev_energy_kwh",
]


def upgrade() -> None:
    # Preserve historical values, but never mix UTC and local-calendar keys.
    op.execute("UPDATE telemetry_aggregates SET period_type = 'legacy_' || period_type WHERE period_type IN ('day', 'month')")
    for col in _ENERGY_COLUMNS:
        op.alter_column("telemetry_aggregates", col, existing_type=sa.Numeric(precision=12, scale=4), nullable=True)
    op.add_column(
        "telemetry_aggregates",
        sa.Column("coverage", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.alter_column("telemetry_aggregates", "coverage", server_default=None)


def downgrade() -> None:
    op.execute("UPDATE telemetry_aggregates SET period_type = 'local_' || period_type WHERE period_type IN ('day', 'month')")
    op.execute("UPDATE telemetry_aggregates SET period_type = substring(period_type from 8) WHERE period_type IN ('legacy_day', 'legacy_month')")
    op.drop_column("telemetry_aggregates", "coverage")
    op.execute(
        "UPDATE telemetry_aggregates SET "
        + ", ".join(f"{col} = COALESCE({col}, 0)" for col in _ENERGY_COLUMNS)
        + " WHERE " + " OR ".join(f"{col} IS NULL" for col in _ENERGY_COLUMNS)
    )
    for col in _ENERGY_COLUMNS:
        op.alter_column("telemetry_aggregates", col, existing_type=sa.Numeric(precision=12, scale=4), nullable=False)
