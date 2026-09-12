"""import run archival

Revision ID: b7d3f6a9c1e4
Revises: a3941290d3e9
Create Date: 2026-09-12T04:00:00.000000

Part of issue #51: arhivare nedistructiva a reviziilor OPCOM excedentare
(peste pragul de revizii active/zi) -- `is_archived`/`archived_at`/
`archived_reason` pe `import_runs`. Coloana `is_archived` are default de
server (false) ca sa ramana NOT NULL fara sa rupa randurile existente
(toate devin "active", comportament identic cu inainte de acest issue).
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b7d3f6a9c1e4"
down_revision = "a3941290d3e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_runs", sa.Column("is_archived", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("import_runs", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("import_runs", sa.Column("archived_reason", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("import_runs", "archived_reason")
    op.drop_column("import_runs", "archived_at")
    op.drop_column("import_runs", "is_archived")
