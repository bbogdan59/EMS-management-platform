"""membership is_active

Revision ID: f1c7a9e2b4d6
Revises: a3941290d3e9
Create Date: 2026-09-11 20:10:00.000000

Part of issue #23: dezactivare nedistructiva a unei apartenente (`Membership`)
la o organizatie, distincta de eliminarea completa (hard delete). Coloana e
adaugata cu default de server (true) ca sa ramana NOT NULL fara sa rupa
randurile deja existente -- toate membership-urile curente devin active.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f1c7a9e2b4d6"
down_revision = "a3941290d3e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memberships", sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False))


def downgrade() -> None:
    op.drop_column("memberships", "is_active")
