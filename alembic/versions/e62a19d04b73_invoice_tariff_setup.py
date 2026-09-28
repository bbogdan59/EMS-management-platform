"""Invoice breakdowns and precision for Romanian per-kWh components.

Revision ID: e62a19d04b73
Revises: d91e62b48c03
"""

import sqlalchemy as sa

from alembic import op

revision = "e62a19d04b73"
down_revision = "d91e62b48c03"
branch_labels = None
depends_on = None

PRICE_COLUMNS = (
    "fixed_price_lei_per_kwh",
    "opcom_margin_lei_per_kwh",
    "variable_component_lei_per_kwh",
    "distribution_lei_per_kwh",
    "transport_lei_per_kwh",
    "other_regulated_lei_per_kwh",
)


def upgrade():
    for name in PRICE_COLUMNS:
        op.alter_column("tariff_versions", name, type_=sa.Numeric(16, 8))
    op.add_column("tariff_versions", sa.Column("invoice_breakdown", sa.JSON(), nullable=True))
    if "legacy_tariff_breakdowns" in sa.inspect(op.get_bind()).get_table_names():
        op.execute("""
            UPDATE tariff_versions v SET invoice_breakdown = l.invoice_breakdown
            FROM legacy_tariff_breakdowns l WHERE v.id = l.id
        """)
        op.drop_table("legacy_tariff_breakdowns")


def downgrade():
    op.execute(
        "CREATE TABLE legacy_tariff_breakdowns AS SELECT id, invoice_breakdown FROM tariff_versions"
    )
    op.drop_column("tariff_versions", "invoice_breakdown")
    # The wider NUMERIC remains compatible with older code. Shrinking it would
    # irreversibly round CfD/certificate rates already saved by users.
