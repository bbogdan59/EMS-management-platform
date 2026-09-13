"""station setup completed at

Revision ID: 3ba1401941db
Revises: edc1804cd699
Create Date: 2026-09-13 05:15:36.029739

Part of issue #41 (wizard multi-pas de configurare): marcheaza momentul in
care asistentul ghidat a fost parcurs explicit pana la pasul final
("Rezumat" -> "Activeaza statia"). Coloana e nullable -- toate statiile
existente raman NULL (nu au trecut prin acest wizard, care nu exista inca),
folosita doar ca semnal UX (afisarea unui banner de reluare pe dashboard),
niciodata ca poarta de acces la datele deja salvate.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "3ba1401941db"
down_revision = "edc1804cd699"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stations", sa.Column("setup_completed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("stations", "setup_completed_at")
