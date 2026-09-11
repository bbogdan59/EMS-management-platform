"""device enrollment: nullable station_id, provisioning identity

Revision ID: b8e2f4a1c9d7
Revises: febfd647fd64
Create Date: 2026-09-10 00:00:00.000000

Part of issue #16 (automatic device enrollment): a device can now exist
without a station, in `status='pending_claim'`, created by the NEW
self-service enrollment endpoint (`POST /api/v1/devices/enroll`) rather than
only via the existing operator-generated `ClaimCode` flow (which still
creates the device with its station already set, unchanged).

Adds the provisioning identity used to authenticate an unallocated device's
repeated/idempotent enrollment calls (`installation_uuid` +
`provisioning_secret_hash`, both device-declared/device-generated -- NEVER
authoritative for tenant/station assignment, only an admin's explicit
allocation is), a short-lived plaintext staging column for a newly-issued
credential secret so a lost allocation response is recoverable without
reprovisioning, and audit timestamps for enrollment/expiry/allocation.

NOTE for integration: this branches from the same parent revision
(`febfd647fd64`) as issue #4's telemetry migration
(`a3f7c9d1e6b2_telemetry_aggregate_coverage.py`), which was developed in
parallel and is not yet on `main` as of this commit. Whichever merges
second will need an Alembic merge migration (`alembic merge heads`) at
integration time -- this is expected, not an error to fix here.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b8e2f4a1c9d7"
down_revision = "febfd647fd64"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("devices", "station_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column("devices", sa.Column("installation_uuid", sa.String(length=64), nullable=True))
    op.add_column("devices", sa.Column("provisioning_secret_hash", sa.String(length=128), nullable=True))
    op.add_column("devices", sa.Column("pending_credential_secret", sa.String(length=128), nullable=True))
    op.add_column("devices", sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("devices", sa.Column("enrollment_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("devices", sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("devices", sa.Column("allocated_by_user_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint("uq_devices_installation_uuid", "devices", ["installation_uuid"])
    op.create_foreign_key(
        "fk_devices_allocated_by_user_id", "devices", "users", ["allocated_by_user_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_devices_allocated_by_user_id", "devices", type_="foreignkey")
    op.drop_constraint("uq_devices_installation_uuid", "devices", type_="unique")
    op.drop_column("devices", "allocated_by_user_id")
    op.drop_column("devices", "allocated_at")
    op.drop_column("devices", "enrollment_expires_at")
    op.drop_column("devices", "enrolled_at")
    op.drop_column("devices", "pending_credential_secret")
    op.drop_column("devices", "provisioning_secret_hash")
    op.drop_column("devices", "installation_uuid")
    op.alter_column("devices", "station_id", existing_type=sa.Uuid(), nullable=False)
