"""Opt-in, minimised Home Assistant context over MQTT.

Revision ID: f189a27c803e
Revises: e62a19d04b73
"""

import sqlalchemy as sa

from alembic import op

revision = "f189a27c803e"
down_revision = "e62a19d04b73"
branch_labels = None
depends_on = None


def _identity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade():
    op.create_table(
        "home_assistant_connections",
        *_identity(),
        sa.Column(
            "station_id",
            sa.Uuid(),
            sa.ForeignKey("stations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("broker_key", sa.String(80), nullable=False),
        sa.Column("encrypted_credentials", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("publish_enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("stream_id", sa.Uuid(), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(32), nullable=True),
        sa.Column("rejected_messages", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_ha_revision"),
    )
    op.create_table(
        "home_assistant_mappings",
        *_identity(),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("home_assistant_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("entity_id", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("unit", sa.String(12), nullable=False),
        sa.Column("max_age_seconds", sa.Integer(), nullable=False),
        sa.Column("value", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_id", sa.String(80), nullable=True),
        sa.Column("source", sa.String(24), nullable=True),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("connection_id", "entity_id", name="uq_ha_mapping_entity"),
        sa.CheckConstraint("max_age_seconds BETWEEN 30 AND 3600", name="ck_ha_mapping_age"),
    )
    op.create_index(
        "ix_home_assistant_mappings_connection_id", "home_assistant_mappings", ["connection_id"]
    )


def downgrade():
    # New opt-in context only; no canonical energy history or legacy schema is altered.
    op.drop_table("home_assistant_mappings")
    op.drop_table("home_assistant_connections")
