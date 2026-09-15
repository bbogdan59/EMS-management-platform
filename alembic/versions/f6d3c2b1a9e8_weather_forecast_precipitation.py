"""weather forecast precipitation

Revision ID: f6d3c2b1a9e8
Revises: e0544ddedc8b
Create Date: 2026-09-15T18:20:00.000000

Part of issue #53: Open-Meteo exposes hourly `precipitation`, which is one of
the minimum weather inputs needed for safety/load context. Existing forecast
rows keep NULL precipitation instead of being backfilled with zero; unknown is
not zero.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f6d3c2b1a9e8"
down_revision = "e0544ddedc8b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("weather_forecasts", sa.Column("precipitation_mm", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("weather_forecasts", "precipitation_mm")
