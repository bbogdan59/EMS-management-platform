"""deye cloud connector

Revision ID: e0544ddedc8b
Revises: edc1804cd699
Create Date: 2026-09-13T00:00:00.000000

Issue #43: conector Deye Cloud read-only pentru clienti fara hardware EMS
local. Adauga `TelemetryRaw.source` (provenienta -- `device_rs485` implicit
pentru toate randurile existente, `deye_cloud` pentru telemetrie importata
din contul Deye Cloud al clientului) si tabelele noi `deye_cloud_connections`
/ `deye_cloud_device_links`. Niciun endpoint de scriere/comanda catre Deye.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e0544ddedc8b"
down_revision = "edc1804cd699"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "telemetry_raw",
        sa.Column("source", sa.String(length=32), nullable=False, server_default="device_rs485"),
    )

    op.create_table(
        "deye_cloud_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("region", sa.String(length=8), nullable=False),
        sa.Column("account_email", sa.String(length=320), nullable=False),
        sa.Column("encrypted_account_password", sa.String(length=500), nullable=False),
        sa.Column("remote_station_id", sa.BigInteger(), nullable=True),
        sa.Column("remote_station_name", sa.String(length=200), nullable=True),
        sa.Column("pending_remote_stations", sa.JSON(), server_default=sa.text("'[]'::json"), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("consent_accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("encrypted_access_token", sa.String(length=2000), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_status", sa.String(length=16), nullable=True),
        sa.Column("last_sync_message", sa.String(length=500), nullable=True),
        sa.Column("consecutive_failure_count", sa.Integer(), nullable=False),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_by_user_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["station_id"], ["stations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["disconnected_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("station_id", name="uq_deye_cloud_connection_station"),
    )
    op.create_index(
        "ix_deye_cloud_connections_station_id", "deye_cloud_connections", ["station_id"]
    )

    op.create_table(
        "deye_cloud_device_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("remote_device_sn", sa.String(length=64), nullable=False),
        sa.Column("remote_device_type", sa.String(length=64), nullable=True),
        sa.Column("raw_snapshot", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["deye_cloud_connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "remote_device_sn", name="uq_deye_cloud_device_link"),
    )
    op.create_index(
        "ix_deye_cloud_device_links_connection_id", "deye_cloud_device_links", ["connection_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_deye_cloud_device_links_connection_id", table_name="deye_cloud_device_links")
    op.drop_table("deye_cloud_device_links")
    op.drop_index("ix_deye_cloud_connections_station_id", table_name="deye_cloud_connections")
    op.drop_table("deye_cloud_connections")
    op.drop_column("telemetry_raw", "source")
