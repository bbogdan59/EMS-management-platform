"""organization lifecycle status

Revision ID: a3941290d3e9
Revises: 3b37b0147548
Create Date: 2026-09-11 19:02:04.944986

Part of issue #24: stari active/suspended/archived pentru `Organization`,
cu metadate ale ultimei tranzitii de suspendare/arhivare (motiv, actor,
timestamp) pastrate ca istoric -- vezi docstring-ul modelului si
`app/services/organization_service.py` pentru tranzitiile validate.
Coloana `status` e adaugata cu default de server ('active') ca sa ramana
NOT NULL fara sa rupa randurile deja existente.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a3941290d3e9"
down_revision = "3b37b0147548"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("status", sa.String(length=16), server_default="active", nullable=False))
    op.add_column("organizations", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organizations", sa.Column("suspended_reason", sa.String(length=500), nullable=True))
    op.add_column("organizations", sa.Column("suspended_by_user_id", sa.Uuid(), nullable=True))
    op.add_column("organizations", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("organizations", sa.Column("archived_reason", sa.String(length=500), nullable=True))
    op.add_column("organizations", sa.Column("archived_by_user_id", sa.Uuid(), nullable=True))
    op.add_column("organizations", sa.Column("billing_email", sa.String(length=320), nullable=True))
    op.add_column("organizations", sa.Column("notes", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_organizations_archived_by_user_id", "organizations", "users", ["archived_by_user_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_organizations_suspended_by_user_id", "organizations", "users", ["suspended_by_user_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_organizations_suspended_by_user_id", "organizations", type_="foreignkey")
    op.drop_constraint("fk_organizations_archived_by_user_id", "organizations", type_="foreignkey")
    op.drop_column("organizations", "notes")
    op.drop_column("organizations", "billing_email")
    op.drop_column("organizations", "archived_by_user_id")
    op.drop_column("organizations", "archived_reason")
    op.drop_column("organizations", "archived_at")
    op.drop_column("organizations", "suspended_by_user_id")
    op.drop_column("organizations", "suspended_reason")
    op.drop_column("organizations", "suspended_at")
    op.drop_column("organizations", "status")
