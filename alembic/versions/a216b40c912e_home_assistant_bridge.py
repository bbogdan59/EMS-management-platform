"""Add isolated HACS pairing and context; no change to energy history.

Revision ID: a216b40c912e
Revises: f189a27c803e
"""

import sqlalchemy as sa

from alembic import op

revision = "a216b40c912e"
down_revision = "f189a27c803e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "home_assistant_bridges",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "station_id",
            sa.Uuid(),
            sa.ForeignKey("stations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("consent_by", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("pairing_hash", sa.String(64), unique=True),
        sa.Column("pairing_expires_at", sa.DateTime(timezone=True)),
        sa.Column("token_hash", sa.String(64), unique=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("instance_id", sa.Uuid()),
        sa.Column("instance_name", sa.String(80)),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.Column("mappings", sa.JSON(), nullable=False),
        sa.Column("observations", sa.JSON(), nullable=False),
        sa.Column("occupancy_consent", sa.Boolean(), nullable=False),
        sa.Column("insights_consent", sa.Boolean(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_table("home_assistant_bridges")
