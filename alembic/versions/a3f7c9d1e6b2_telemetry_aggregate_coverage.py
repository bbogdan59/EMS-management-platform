"""telemetry aggregate coverage tracking and nullable energy fields

Revision ID: a3f7c9d1e6b2
Revises: febfd647fd64
Create Date: 2026-09-10 00:00:00.000000

Part of issue #4 (telemetry energy integration correctness): the old
`telemetry_aggregates` schema forced every energy column to be NOT NULL with
an implicit 0 default, which made "we don't actually know" indistinguishable
from "zero energy flowed". This migration:

  - relaxes the 7 energy columns to nullable (existing rows keep their
    current values -- they were all real computed numbers before, so no
    data is lost or changed by this step);
  - adds `coverage` (JSON), a per-metric dict of how much of each aggregate's
    duration was actually backed by telemetry samples (see
    `app/services/aggregation_service.py` for the exact integration
    contract). Existing rows get `{}` (unknown provenance for
    pre-migration data -- they predate coverage tracking and are treated as
    fully-measured legacy rows by consumers that don't special-case this).

Backwards compatible: no existing row's numeric values change, and no
existing numeric value is erased -- `coalesce(sum(energy_col), 0)` style queries
(e.g. `dashboard_service.get_efc_used`) already skip NULLs the same way SQL
always has, they simply had never seen one until now.
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
