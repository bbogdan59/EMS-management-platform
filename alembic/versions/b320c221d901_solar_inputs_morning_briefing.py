"""Isolated MPPT diagnostics and opt-in morning delivery preferences.

Revision ID: b320c221d901
Revises: a216b40c912e
"""

import sqlalchemy as sa

from alembic import op

revision = "b320c221d901"
down_revision = "a216b40c912e"
branch_labels = None
depends_on = None


def entity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def measurements():
    return [
        sa.Column(name, sa.Numeric(14, 4), nullable=True)
        for name in ("voltage_v", "current_a", "power_w")
    ]


def upgrade():
    op.add_column(
        "notification_preferences",
        sa.Column("morning_briefing", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "notification_preferences",
        sa.Column("briefing_start_hour", sa.Integer(), nullable=False, server_default="7"),
    )
    op.add_column(
        "notification_preferences",
        sa.Column("briefing_end_hour", sa.Integer(), nullable=False, server_default="11"),
    )
    op.add_column(
        "notification_preferences",
        sa.Column("briefing_clock", sa.String(16), nullable=False, server_default="station"),
    )
    op.create_check_constraint(
        "ck_briefing_window",
        "notification_preferences",
        "briefing_start_hour >= 0 AND briefing_start_hour < briefing_end_hour AND briefing_end_hour <= 24 AND briefing_clock IN ('station', 'user')",
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    tables = ("solar_inverters", "solar_trackers", "solar_input_samples", "solar_input_aggregates")
    if sa.inspect(op.get_bind()).has_table("legacy_solar_inverters"):
        for table in tables:
            op.rename_table("legacy_" + table, table)
        return
    op.create_table(
        "solar_inverters",
        *entity(),
        sa.Column(
            "station_id",
            sa.Uuid(),
            sa.ForeignKey("stations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "device_id", sa.Uuid(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_key", sa.String(128), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("model_name", sa.String(120)),
        sa.Column("ac_observation", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "station_id", "device_id", "source", "source_key", name="uq_solar_inverter_source"
        ),
    )
    op.create_index("ix_solar_inverters_station_id", "solar_inverters", ["station_id"])
    op.create_table(
        "solar_trackers",
        *entity(),
        sa.Column(
            "inverter_id",
            sa.Uuid(),
            sa.ForeignKey("solar_inverters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("input_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("supported_metrics", sa.JSON(), nullable=False),
        sa.Column("last_reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.UniqueConstraint("inverter_id", "input_index", name="uq_solar_tracker_index"),
    )
    op.create_index("ix_solar_trackers_inverter_id", "solar_trackers", ["inverter_id"])
    op.create_table(
        "solar_input_samples",
        *entity(),
        sa.Column(
            "tracker_id",
            sa.Uuid(),
            sa.ForeignKey("solar_trackers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        *measurements(),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.UniqueConstraint("tracker_id", "measured_at", name="uq_solar_sample_time"),
    )
    for field in ("tracker_id", "measured_at"):
        op.create_index("ix_solar_input_samples_" + field, "solar_input_samples", [field])
    op.create_table(
        "solar_input_aggregates",
        *entity(),
        sa.Column(
            "tracker_id",
            sa.Uuid(),
            sa.ForeignKey("solar_trackers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_type", sa.String(16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        *measurements(),
        sa.Column("energy_kwh", sa.Numeric(16, 6), nullable=True),
        sa.Column("coverage", sa.JSON(), nullable=False),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.UniqueConstraint(
            "tracker_id", "period_type", "period_start", name="uq_solar_aggregate_period"
        ),
    )
    for field in ("tracker_id", "period_start"):
        op.create_index("ix_solar_input_aggregates_" + field, "solar_input_aggregates", [field])


def downgrade():
    # Preserve new diagnostic history as well as legacy station energy on rollback.
    for table in (
        "solar_input_aggregates",
        "solar_input_samples",
        "solar_trackers",
        "solar_inverters",
    ):
        op.rename_table(table, "legacy_" + table)
    op.drop_column("notification_deliveries", "expires_at")
    op.drop_constraint("ck_briefing_window", "notification_preferences", type_="check")
    for column in (
        "briefing_clock",
        "briefing_end_hour",
        "briefing_start_hour",
        "morning_briefing",
    ):
        op.drop_column("notification_preferences", column)
