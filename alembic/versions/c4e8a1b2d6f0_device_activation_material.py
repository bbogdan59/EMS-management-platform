"""device activation material

Revision ID: c4e8a1b2d6f0
Revises: e2a4c8f1d9b3
Create Date: 2026-09-12T19:10:00.000000

Adds a public inventory serial and a one-use hash of the sealed customer
activation code. No activation secret is stored in clear text.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c4e8a1b2d6f0"
down_revision = "e2a4c8f1d9b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("serial_number", sa.String(length=64), nullable=True))
    op.add_column("devices", sa.Column("activation_code_hash", sa.String(length=128), nullable=True))
    op.add_column("devices", sa.Column("activation_claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_devices_serial_number", "devices", ["serial_number"], unique=True)
    op.create_index("ix_devices_activation_code_hash", "devices", ["activation_code_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_devices_activation_code_hash", table_name="devices")
    op.drop_index("ix_devices_serial_number", table_name="devices")
    op.drop_column("devices", "activation_claimed_at")
    op.drop_column("devices", "activation_code_hash")
    op.drop_column("devices", "serial_number")
