"""Telemetry diagnostics, health history and notification outbox.

Revision ID: c72b0e951ad4
Revises: 8132ff168bd4
"""

import sqlalchemy as sa

from alembic import op

revision = "c72b0e951ad4"
down_revision = "8132ff168bd4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "telemetry_raw", sa.Column("diagnostics", sa.JSON(), server_default="{}", nullable=False)
    )
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "legacy_health_states" in existing:
        op.rename_table("legacy_health_states", "health_states")
    else:
        op.execute(
            """
CREATE TABLE health_states (
    station_id UUID NOT NULL,
    rule VARCHAR(48) NOT NULL,
    subject VARCHAR(64) NOT NULL,
    alert_id UUID,
    evaluated_at TIMESTAMP WITH TIME ZONE,
    healthy_windows INTEGER NOT NULL,
    cooldown_until TIMESTAMP WITH TIME ZONE,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_health_state UNIQUE (station_id, rule, subject),
    FOREIGN KEY(station_id) REFERENCES stations (id) ON DELETE CASCADE,
    FOREIGN KEY(alert_id) REFERENCES alerts (id) ON DELETE SET NULL
)
"""
        )
        op.execute("CREATE INDEX ix_health_states_station_id ON health_states (station_id)")
    if "legacy_health_evaluations" in existing:
        op.rename_table("legacy_health_evaluations", "health_evaluations")
    else:
        op.execute(
            """
CREATE TABLE health_evaluations (
    station_id UUID NOT NULL,
    rule VARCHAR(48) NOT NULL,
    subject VARCHAR(64) NOT NULL,
    version INTEGER NOT NULL,
    window_end TIMESTAMP WITH TIME ZONE NOT NULL,
    verdict VARCHAR(16) NOT NULL,
    evidence JSON NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_health_evaluation UNIQUE (station_id, rule, subject, version, window_end),
    FOREIGN KEY(station_id) REFERENCES stations (id) ON DELETE CASCADE
)
"""
        )
        op.execute(
            "CREATE INDEX ix_health_evaluations_station_id ON health_evaluations (station_id)"
        )
        op.execute(
            "CREATE INDEX ix_health_evaluations_window_end ON health_evaluations (window_end)"
        )
    if "legacy_alert_events" in existing:
        op.rename_table("legacy_alert_events", "alert_events")
    else:
        op.execute(
            """
CREATE TABLE alert_events (
    alert_id UUID NOT NULL,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    status VARCHAR(16) NOT NULL,
    actor_user_id UUID,
    reason VARCHAR(500) NOT NULL,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(alert_id) REFERENCES alerts (id) ON DELETE CASCADE,
    FOREIGN KEY(actor_user_id) REFERENCES users (id)
)
"""
        )
        op.execute("CREATE INDEX ix_alert_events_alert_id ON alert_events (alert_id)")
        op.execute("CREATE INDEX ix_alert_events_occurred_at ON alert_events (occurred_at)")
    if "legacy_diagnostic_grants" in existing:
        op.rename_table("legacy_diagnostic_grants", "diagnostic_grants")
    else:
        op.execute(
            """
CREATE TABLE diagnostic_grants (
    station_id UUID NOT NULL,
    user_id UUID NOT NULL,
    granted_by UUID NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_diagnostic_grant UNIQUE (station_id, user_id),
    FOREIGN KEY(station_id) REFERENCES stations (id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(granted_by) REFERENCES users (id)
)
"""
        )
        op.execute("CREATE INDEX ix_diagnostic_grants_station_id ON diagnostic_grants (station_id)")
        op.execute("CREATE INDEX ix_diagnostic_grants_user_id ON diagnostic_grants (user_id)")
    if "legacy_notification_preferences" in existing:
        op.rename_table("legacy_notification_preferences", "notification_preferences")
    else:
        op.execute(
            """
CREATE TABLE notification_preferences (
    user_id UUID NOT NULL,
    organization_id UUID NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    quiet_start INTEGER NOT NULL,
    quiet_end INTEGER NOT NULL,
    matrix JSON NOT NULL,
    weekly_report BOOLEAN NOT NULL,
    escalation_minutes INTEGER NOT NULL,
    verified_email VARCHAR(320),
    verification_hash VARCHAR(64),
    verification_expires_at TIMESTAMP WITH TIME ZONE,
    encrypted_push_subscription TEXT,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_notification_preference UNIQUE (user_id, organization_id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE
)
"""
        )
        op.execute(
            "CREATE INDEX ix_notification_preferences_user_id ON notification_preferences (user_id)"
        )
    if "legacy_notifications" in existing:
        op.rename_table("legacy_notifications", "notifications")
    else:
        op.execute(
            """
CREATE TABLE notifications (
    user_id UUID NOT NULL,
    station_id UUID NOT NULL,
    event_id UUID NOT NULL,
    title VARCHAR(300) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    category VARCHAR(48) NOT NULL,
    link VARCHAR(200) NOT NULL,
    read_at TIMESTAMP WITH TIME ZONE,
    routed_at TIMESTAMP WITH TIME ZONE,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_notification_event_user UNIQUE (user_id, event_id),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(station_id) REFERENCES stations (id) ON DELETE CASCADE,
    FOREIGN KEY(event_id) REFERENCES alert_events (id) ON DELETE CASCADE
)
"""
        )
        op.execute("CREATE INDEX ix_notifications_event_id ON notifications (event_id)")
        op.execute("CREATE INDEX ix_notifications_station_id ON notifications (station_id)")
        op.execute("CREATE INDEX ix_notifications_user_id ON notifications (user_id)")
    if "legacy_notification_deliveries" in existing:
        op.rename_table("legacy_notification_deliveries", "notification_deliveries")
    else:
        op.execute(
            """
CREATE TABLE notification_deliveries (
    user_id UUID NOT NULL,
    organization_id UUID NOT NULL,
    channel VARCHAR(16) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    dedupe_key VARCHAR(200) NOT NULL,
    status VARCHAR(16) NOT NULL,
    due_at TIMESTAMP WITH TIME ZONE NOT NULL,
    attempts INTEGER NOT NULL,
    notification_ids JSON NOT NULL,
    encrypted_payload TEXT,
    failure_code VARCHAR(48),
    delivered_at TIMESTAMP WITH TIME ZONE,
    id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_notification_delivery_key UNIQUE (dedupe_key),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE
)
"""
        )
        op.execute(
            "CREATE INDEX ix_notification_deliveries_due_at ON notification_deliveries (due_at)"
        )
        op.execute(
            "CREATE INDEX ix_notification_deliveries_status ON notification_deliveries (status)"
        )
        op.execute(
            "CREATE INDEX ix_notification_deliveries_user_id ON notification_deliveries (user_id)"
        )


def downgrade():
    # Preserve typed diagnostics in the legacy payload before removing the
    # additive column. NULL canonical measurements are never rewritten.
    op.execute(
        "UPDATE telemetry_raw SET raw_payload = (raw_payload::jsonb || jsonb_build_object('extended', diagnostics))::json WHERE diagnostics::jsonb <> '{}'::jsonb"
    )
    op.drop_column("telemetry_raw", "diagnostics")
    op.rename_table("notification_deliveries", "legacy_notification_deliveries")
    op.rename_table("notifications", "legacy_notifications")
    op.rename_table("notification_preferences", "legacy_notification_preferences")
    op.rename_table("diagnostic_grants", "legacy_diagnostic_grants")
    op.rename_table("alert_events", "legacy_alert_events")
    op.rename_table("health_evaluations", "legacy_health_evaluations")
    op.rename_table("health_states", "legacy_health_states")
