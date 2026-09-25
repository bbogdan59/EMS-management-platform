"""Repair retained telemetry provenance without rewriting energy history.

Revision ID: a94e8c12f6b0
Revises: f83d4b0a19c2
"""

from alembic import op

revision = "a94e8c12f6b0"
down_revision = "f83d4b0a19c2"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        UPDATE telemetry_raw SET is_simulated = true
        WHERE NOT is_simulated AND (
            quality_flags->>'simulated' = 'true' OR raw_payload->>'simulated' = 'true'
        )
    """)
    # Retention may have removed part of an interval, so only repair metadata.
    # The five-minute carry-in matches the integration contract at this revision.
    op.execute("""
        WITH affected AS (
            SELECT a.id,
                bool_or(r.is_simulated) AS simulated,
                bool_or(r.quality_flags->>'stale' = 'true') AS stale,
                bool_or(r.quality_flags->>'derived' = 'true') AS derived
            FROM telemetry_aggregates a JOIN telemetry_raw r
              ON r.station_id = a.station_id
             AND r.measured_at >= a.period_start - interval '5 minutes'
             AND r.measured_at < a.period_end
            WHERE r.is_simulated OR r.quality_flags->>'stale' = 'true'
                OR r.quality_flags->>'derived' = 'true'
            GROUP BY a.id
        )
        UPDATE telemetry_aggregates a SET data_quality = CASE
            WHEN a.data_quality = 'simulated' OR x.simulated THEN 'simulated'
            WHEN a.data_quality = 'stale' OR x.stale THEN 'stale'
            WHEN a.data_quality = 'measured' AND x.derived THEN 'estimated'
            ELSE a.data_quality END
        FROM affected x WHERE a.id = x.id
    """)
    # Existing child aggregates can still prove taint after raw data is pruned.
    op.execute("""
        WITH affected AS (
            SELECT parent.id,
                bool_or(child.data_quality = 'simulated') AS simulated,
                bool_or(child.data_quality = 'stale') AS stale
            FROM telemetry_aggregates parent JOIN telemetry_aggregates child
              ON child.station_id = parent.station_id
             AND child.period_start >= parent.period_start
             AND child.period_end <= parent.period_end
             AND child.period_end - child.period_start
                 < parent.period_end - parent.period_start
            WHERE child.data_quality IN ('simulated', 'stale')
            GROUP BY parent.id
        )
        UPDATE telemetry_aggregates a SET data_quality = CASE
            WHEN a.data_quality = 'simulated' OR x.simulated THEN 'simulated'
            ELSE 'stale' END
        FROM affected x WHERE a.id = x.id
    """)
    # Historical profiles have no per-source row IDs. Conservatively taint any
    # profile issued after potentially contributing untrusted retained history.
    op.execute("""
        WITH history AS (
            SELECT station_id, min(period_start) AS first_untrusted,
                min(period_start) FILTER (WHERE data_quality = 'simulated') AS first_simulated
            FROM telemetry_aggregates
            WHERE period_type = 'interval_15m' AND data_quality <> 'measured'
                AND load_energy_kwh IS NOT NULL
            GROUP BY station_id
        )
        UPDATE consumption_forecasts f SET
            confidence = 'low',
            is_synthetic = f.is_synthetic OR coalesce(h.first_simulated < f.issued_at, false),
            source_version = CASE WHEN right(coalesce(f.source_version, ''), 10) = '_untrusted'
                THEN f.source_version
                ELSE left(coalesce(f.source_version, 'legacy'), 54) || '_untrusted' END
        FROM history h WHERE f.station_id = h.station_id
            AND f.source = 'historical_profile' AND h.first_untrusted < f.issued_at
    """)


def downgrade():
    # Corrected provenance remains true on rollback; numeric/NULL values never change.
    pass
