"""admin jobs tracking table

Revision ID: 3b37b0147548
Revises: d17a2c9f0311
Create Date: 2026-09-11 15:20:27.530476

Part of issue #11: urmarirea unui job asincron declansat manual din panoul
admin (import OPCOM ad-hoc sau reoptimizare unei statii), distinct de
`import_runs`/`optimization_runs` (acelea raman inregistrarea de business a
rezultatului) -- vezi docstring-ul modelului `AdminJob`.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "3b37b0147548"
down_revision = "d17a2c9f0311"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("job_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("celery_task_id", sa.String(length=64), nullable=True),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("target_label", sa.String(length=200), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=True),
        sa.Column("triggered_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("result_resource_type", sa.String(length=24), nullable=True),
        sa.Column("result_resource_id", sa.Uuid(), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["station_id"], ["stations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["triggered_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_admin_jobs_job_type"), "admin_jobs", ["job_type"], unique=False)
    op.create_index(op.f("ix_admin_jobs_station_id"), "admin_jobs", ["station_id"], unique=False)
    op.create_index(op.f("ix_admin_jobs_status"), "admin_jobs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_jobs_status"), table_name="admin_jobs")
    op.drop_index(op.f("ix_admin_jobs_station_id"), table_name="admin_jobs")
    op.drop_index(op.f("ix_admin_jobs_job_type"), table_name="admin_jobs")
    op.drop_table("admin_jobs")
