"""deye cloud station app credentials

Revision ID: a7e9c4d2b6f1
Revises: f6d3c2b1a9e8
Create Date: 2026-09-15T20:30:00.000000

Deye appId/appSecret are configured per station connection, not only as global
platform settings. Existing rows keep NULL until the user reconnects and
supplies station-specific app credentials.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a7e9c4d2b6f1"
down_revision = "f6d3c2b1a9e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deye_cloud_connections", sa.Column("app_id", sa.String(length=128), nullable=True))
    op.add_column("deye_cloud_connections", sa.Column("encrypted_app_secret", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("deye_cloud_connections", "encrypted_app_secret")
    op.drop_column("deye_cloud_connections", "app_id")
