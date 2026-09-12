"""tariff version cost components

Revision ID: e2a4c8f1d9b3
Revises: b7d3f6a9c1e4
Create Date: 2026-09-12T05:00:00.000000

Part of issue #46: componente de cost explicite pentru `TariffVersion`
(distributie/transport/alte taxe reglementate, plus o cota de TVA optionala)
-- distincte de `variable_component_lei_per_kwh` (pastrat, backward-compatibil).
Toate coloanele NUMERIC noi au default de server 0 (respectiv NULL pentru
`vat_rate_percent`, explicit "TVA neinclus", nu 0%) ca sa ramana NOT NULL
fara sa rupa versiunile existente -- comportamentul de calcul ramane identic
pentru randurile deja existente (toate componentele noi sunt 0).
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e2a4c8f1d9b3"
down_revision = "b7d3f6a9c1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tariff_versions", sa.Column("distribution_lei_per_kwh", sa.Numeric(10, 5), server_default="0", nullable=False))
    op.add_column("tariff_versions", sa.Column("transport_lei_per_kwh", sa.Numeric(10, 5), server_default="0", nullable=False))
    op.add_column("tariff_versions", sa.Column("other_regulated_lei_per_kwh", sa.Numeric(10, 5), server_default="0", nullable=False))
    op.add_column("tariff_versions", sa.Column("vat_rate_percent", sa.Numeric(5, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("tariff_versions", "vat_rate_percent")
    op.drop_column("tariff_versions", "other_regulated_lei_per_kwh")
    op.drop_column("tariff_versions", "transport_lei_per_kwh")
    op.drop_column("tariff_versions", "distribution_lei_per_kwh")
