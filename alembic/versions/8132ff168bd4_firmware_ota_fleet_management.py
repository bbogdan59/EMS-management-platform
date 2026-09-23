"""firmware OTA fleet management (issue #168)

Revision ID: 8132ff168bd4
Revises: e0c1f9256eb2
Create Date: 2026-09-18T00:00:00.000000

Adds the release registry (firmware_releases), rollout campaigns
(firmware_rollouts), per-device deployment state machine
(firmware_deployments) with an immutable event log
(firmware_deployment_events), and typed inventory columns on `devices`
(build_id/hardware_platform/architecture/os_version/last_seen_at/
firmware_version_source/firmware_reported_at). `firmware_version` itself
already existed (heartbeat-reported agent version) and is unchanged.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "8132ff168bd4"
down_revision = "e0c1f9256eb2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("firmware_version_source", sa.String(length=16), nullable=True))
    op.add_column("devices", sa.Column("firmware_reported_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("devices", sa.Column("build_id", sa.String(length=64), nullable=True))
    op.add_column("devices", sa.Column("hardware_platform", sa.String(length=64), nullable=True))
    op.add_column("devices", sa.Column("architecture", sa.String(length=32), nullable=True))
    op.add_column("devices", sa.Column("os_version", sa.String(length=64), nullable=True))
    op.add_column("devices", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "firmware_releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("hardware_platform", sa.String(length=64), nullable=False),
        sa.Column("architecture", sa.String(length=32), nullable=False),
        sa.Column("protocol_schema_version", sa.Integer(), nullable=False),
        sa.Column("min_compatible_agent_version", sa.String(length=32), nullable=True),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("artifact_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256_hex", sa.String(length=64), nullable=False),
        sa.Column("signature_ed25519_hex", sa.String(length=128), nullable=False),
        sa.Column("signing_key_id", sa.String(length=64), nullable=False),
        sa.Column("release_notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_index(op.f("ix_firmware_releases_version"), "firmware_releases", ["version"], unique=True)

    op.create_table(
        "firmware_rollouts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("max_concurrent", sa.Integer(), nullable=False),
        sa.Column("failure_threshold_percent", sa.Integer(), nullable=False),
        sa.Column("allow_downgrade", sa.Boolean(), nullable=False),
        sa.Column("downgrade_reason", sa.String(length=500), nullable=True),
        sa.Column("auto_paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_paused_reason", sa.String(length=500), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["release_id"], ["firmware_releases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_firmware_rollouts_release_id"), "firmware_rollouts", ["release_id"], unique=False)

    op.create_table(
        "firmware_deployments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("rollout_id", sa.Uuid(), nullable=False),
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("from_version", sa.String(length=32), nullable=True),
        sa.Column("target_version", sa.String(length=32), nullable=False),
        sa.Column("is_downgrade", sa.Boolean(), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("offer_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmation_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("boot_id_before", sa.String(length=64), nullable=True),
        sa.Column("boot_id_after", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(["rollout_id"], ["firmware_rollouts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["release_id"], ["firmware_releases.id"]),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_id", "idempotency_key", name="uq_firmware_deployment_idempotency"),
    )
    op.create_index(op.f("ix_firmware_deployments_rollout_id"), "firmware_deployments", ["rollout_id"], unique=False)
    op.create_index(op.f("ix_firmware_deployments_release_id"), "firmware_deployments", ["release_id"], unique=False)
    op.create_index(op.f("ix_firmware_deployments_device_id"), "firmware_deployments", ["device_id"], unique=False)

    op.create_table(
        "firmware_deployment_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deployment_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=24), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=True),
        sa.ForeignKeyConstraint(["deployment_id"], ["firmware_deployments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_firmware_deployment_events_deployment_id"), "firmware_deployment_events", ["deployment_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_firmware_deployment_events_deployment_id"), table_name="firmware_deployment_events")
    op.drop_table("firmware_deployment_events")
    op.drop_index(op.f("ix_firmware_deployments_device_id"), table_name="firmware_deployments")
    op.drop_index(op.f("ix_firmware_deployments_release_id"), table_name="firmware_deployments")
    op.drop_index(op.f("ix_firmware_deployments_rollout_id"), table_name="firmware_deployments")
    op.drop_table("firmware_deployments")
    op.drop_index(op.f("ix_firmware_rollouts_release_id"), table_name="firmware_rollouts")
    op.drop_table("firmware_rollouts")
    op.drop_index(op.f("ix_firmware_releases_version"), table_name="firmware_releases")
    op.drop_table("firmware_releases")
    op.drop_column("devices", "last_seen_at")
    op.drop_column("devices", "os_version")
    op.drop_column("devices", "architecture")
    op.drop_column("devices", "hardware_platform")
    op.drop_column("devices", "build_id")
    op.drop_column("devices", "firmware_reported_at")
    op.drop_column("devices", "firmware_version_source")
