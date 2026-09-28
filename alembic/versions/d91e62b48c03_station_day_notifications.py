"""In-app daily summaries alongside incident notifications.

Revision ID: d91e62b48c03
Revises: b05f7d23a6c1
"""

import sqlalchemy as sa

from alembic import op

revision = "d91e62b48c03"
down_revision = "b05f7d23a6c1"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("notifications", "event_id", nullable=True)
    op.add_column("notifications", sa.Column("source_key", sa.String(80)))
    op.add_column(
        "notifications", sa.Column("payload", sa.JSON(), nullable=False, server_default="{}")
    )
    op.create_unique_constraint(
        "uq_notification_source_user", "notifications", ["user_id", "station_id", "source_key"]
    )
    op.create_check_constraint(
        "ck_notification_source", "notifications", "(event_id IS NULL) <> (source_key IS NULL)"
    )
    if "legacy_day_notifications" in sa.inspect(op.get_bind()).get_table_names():
        # Restore only surviving recipients/stations/events after running older code.
        op.execute("""
            INSERT INTO notifications SELECT n.* FROM legacy_day_notifications n
            JOIN users u ON u.id = n.user_id JOIN stations s ON s.id = n.station_id
            WHERE n.event_id IS NULL OR EXISTS (SELECT 1 FROM alert_events e WHERE e.id = n.event_id)
            ON CONFLICT DO NOTHING
        """)
        op.execute("""
            UPDATE notifications n SET payload = a.payload
            FROM legacy_day_notifications a WHERE n.id = a.id AND n.event_id IS NOT NULL
        """)
        op.drop_table("legacy_day_notifications")


def downgrade():
    # Preserve daily reports, their read state, and incident snapshots on rollback.
    op.execute("CREATE TABLE legacy_day_notifications AS SELECT * FROM notifications")
    op.execute("DELETE FROM notifications WHERE event_id IS NULL")
    op.drop_constraint("ck_notification_source", "notifications", type_="check")
    op.drop_constraint("uq_notification_source_user", "notifications", type_="unique")
    op.drop_column("notifications", "payload")
    op.drop_column("notifications", "source_key")
    op.alter_column("notifications", "event_id", nullable=False)
