"""Closed-loop control, canonical EV and recommendation history.

Revision ID: b05f7d23a6c1
Revises: a94e8c12f6b0
"""

import sqlalchemy as sa

from alembic import op

revision = "b05f7d23a6c1"
down_revision = "a94e8c12f6b0"
branch_labels = None
depends_on = None

TABLES = [
    ("vehicles", [
        """
CREATE TABLE vehicles (
	organization_id UUID NOT NULL,
	alias VARCHAR(100) NOT NULL,
	battery_capacity_kwh NUMERIC(8, 3),
	consumption_kwh_100km NUMERIC(8, 3),
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE
)
""",
        """
CREATE INDEX ix_vehicles_organization_id ON vehicles (organization_id)
""",
    ]),
    ("control_policies", [
        """
CREATE TABLE control_policies (
	station_id UUID NOT NULL,
	version INTEGER NOT NULL,
	limits JSON NOT NULL,
	created_by UUID NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_control_policy_version UNIQUE (station_id, version),
	FOREIGN KEY(station_id) REFERENCES stations (id) ON DELETE CASCADE,
	FOREIGN KEY(created_by) REFERENCES users (id)
)
""",
        """
CREATE INDEX ix_control_policies_station_id ON control_policies (station_id)
""",
    ]),
    ("evses", [
        """
CREATE TABLE evses (
	station_id UUID NOT NULL,
	name VARCHAR(100) NOT NULL,
	device_id UUID,
	source VARCHAR(32) NOT NULL,
	capabilities JSON NOT NULL,
	max_power_kw NUMERIC(8, 3),
	config_snapshot JSON NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE,
	retention_days INTEGER NOT NULL,
	vehicle_data_consent BOOLEAN NOT NULL,
	vacation_until TIMESTAMP WITH TIME ZONE,
	is_active BOOLEAN NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(station_id) REFERENCES stations (id) ON DELETE CASCADE,
	FOREIGN KEY(device_id) REFERENCES devices (id)
)
""",
        """
CREATE INDEX ix_evses_station_id ON evses (station_id)
""",
    ]),
    ("station_controls", [
        """
CREATE TABLE station_controls (
	station_id UUID NOT NULL,
	mode VARCHAR(16) NOT NULL,
	revision INTEGER NOT NULL,
	policy_id UUID,
	reason VARCHAR(500) NOT NULL,
	changed_by UUID,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (station_id),
	FOREIGN KEY(station_id) REFERENCES stations (id) ON DELETE CASCADE,
	FOREIGN KEY(policy_id) REFERENCES control_policies (id),
	FOREIGN KEY(changed_by) REFERENCES users (id)
)
""",
    ]),
    ("ev_connectors", [
        """
CREATE TABLE ev_connectors (
	evse_id UUID NOT NULL,
	number INTEGER NOT NULL,
	state VARCHAR(20) NOT NULL,
	last_observed_at TIMESTAMP WITH TIME ZONE,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_ev_connector_number UNIQUE (evse_id, number),
	FOREIGN KEY(evse_id) REFERENCES evses (id) ON DELETE CASCADE
)
""",
        """
CREATE INDEX ix_ev_connectors_evse_id ON ev_connectors (evse_id)
""",
    ]),
    ("charging_sessions", [
        """
CREATE TABLE charging_sessions (
	station_id UUID NOT NULL,
	connector_id UUID NOT NULL,
	vehicle_id UUID,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	ended_at TIMESTAMP WITH TIME ZONE,
	state VARCHAR(20) NOT NULL,
	meter_start_kwh NUMERIC(16, 6),
	meter_end_kwh NUMERIC(16, 6),
	energy_kwh NUMERIC(16, 6),
	source VARCHAR(32) NOT NULL,
	quality VARCHAR(20) NOT NULL,
	reason_codes JSON NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(station_id) REFERENCES stations (id) ON DELETE CASCADE,
	FOREIGN KEY(connector_id) REFERENCES ev_connectors (id) ON DELETE CASCADE,
	FOREIGN KEY(vehicle_id) REFERENCES vehicles (id) ON DELETE SET NULL
)
""",
        """
CREATE INDEX ix_charging_sessions_connector_id ON charging_sessions (connector_id)
""",
        """
CREATE INDEX ix_charging_sessions_started_at ON charging_sessions (started_at)
""",
        """
CREATE INDEX ix_charging_sessions_station_id ON charging_sessions (station_id)
""",
        """
CREATE UNIQUE INDEX uq_ev_active_session ON charging_sessions (connector_id) WHERE ended_at IS NULL
""",
    ]),
    ("ev_requirements", [
        """
CREATE TABLE ev_requirements (
	connector_id UUID NOT NULL,
	vehicle_id UUID,
	minimum_energy_kwh NUMERIC(9, 3) NOT NULL,
	target_soc_percent NUMERIC(5, 2),
	target_range_km NUMERIC(8, 2),
	deadline TIMESTAMP WITH TIME ZONE,
	schedule JSON NOT NULL,
	status VARCHAR(20) NOT NULL,
	max_cost_lei NUMERIC(10, 4),
	created_by UUID,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(connector_id) REFERENCES ev_connectors (id) ON DELETE CASCADE,
	FOREIGN KEY(vehicle_id) REFERENCES vehicles (id) ON DELETE SET NULL,
	FOREIGN KEY(created_by) REFERENCES users (id)
)
""",
        """
CREATE INDEX ix_ev_requirements_connector_id ON ev_requirements (connector_id)
""",
    ]),
    ("plan_approvals", [
        """
CREATE TABLE plan_approvals (
	station_id UUID NOT NULL,
	plan_id UUID NOT NULL,
	device_id UUID NOT NULL,
	policy_id UUID NOT NULL,
	control_revision INTEGER NOT NULL,
	approved_by UUID,
	mode VARCHAR(16) NOT NULL,
	snapshot JSON NOT NULL,
	snapshot_hash VARCHAR(64) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(station_id) REFERENCES stations (id) ON DELETE CASCADE,
	UNIQUE (plan_id),
	FOREIGN KEY(plan_id) REFERENCES plans (id),
	FOREIGN KEY(device_id) REFERENCES devices (id),
	FOREIGN KEY(policy_id) REFERENCES control_policies (id),
	FOREIGN KEY(approved_by) REFERENCES users (id)
)
""",
        """
CREATE INDEX ix_plan_approvals_station_id ON plan_approvals (station_id)
""",
    ]),
    ("recommendations", [
        """
CREATE TABLE recommendations (
	station_id UUID NOT NULL,
	plan_id UUID,
	fingerprint VARCHAR(64) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	status VARCHAR(24) NOT NULL,
	snapshot JSON NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	snoozed_until TIMESTAMP WITH TIME ZONE,
	acted_by UUID,
	acted_at TIMESTAMP WITH TIME ZONE,
	feedback VARCHAR(500),
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_recommendation_fingerprint UNIQUE (station_id, fingerprint),
	FOREIGN KEY(station_id) REFERENCES stations (id) ON DELETE CASCADE,
	FOREIGN KEY(plan_id) REFERENCES plans (id),
	FOREIGN KEY(acted_by) REFERENCES users (id)
)
""",
        """
CREATE INDEX ix_recommendations_station_id ON recommendations (station_id)
""",
    ]),
    ("charging_plans", [
        """
CREATE TABLE charging_plans (
	requirement_id UUID NOT NULL,
	version INTEGER NOT NULL,
	status VARCHAR(20) NOT NULL,
	snapshot JSON NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_charging_plan_version UNIQUE (requirement_id, version),
	FOREIGN KEY(requirement_id) REFERENCES ev_requirements (id) ON DELETE CASCADE
)
""",
        """
CREATE INDEX ix_charging_plans_requirement_id ON charging_plans (requirement_id)
""",
    ]),
    ("ev_observations", [
        """
CREATE TABLE ev_observations (
	connector_id UUID NOT NULL,
	session_id UUID,
	event_id VARCHAR(128) NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	state VARCHAR(20) NOT NULL,
	meter_kwh NUMERIC(16, 6),
	power_kw NUMERIC(8, 3),
	vehicle_soc_percent NUMERIC(5, 2),
	meter_epoch VARCHAR(64),
	quality VARCHAR(20) NOT NULL,
	applied BOOLEAN NOT NULL,
	payload_hash VARCHAR(64) NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_ev_observation_event UNIQUE (connector_id, event_id),
	FOREIGN KEY(connector_id) REFERENCES ev_connectors (id) ON DELETE CASCADE,
	FOREIGN KEY(session_id) REFERENCES charging_sessions (id) ON DELETE CASCADE
)
""",
        """
CREATE INDEX ix_ev_observations_connector_id ON ev_observations (connector_id)
""",
        """
CREATE INDEX ix_ev_observations_observed_at ON ev_observations (observed_at)
""",
        """
CREATE INDEX ix_ev_observations_session_id ON ev_observations (session_id)
""",
    ]),
    ("plan_outcomes", [
        """
CREATE TABLE plan_outcomes (
	interval_id UUID NOT NULL,
	status VARCHAR(24) NOT NULL,
	evidence JSON NOT NULL,
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (interval_id),
	FOREIGN KEY(interval_id) REFERENCES plan_intervals (id) ON DELETE CASCADE
)
""",
    ]),
    ("command_verifications", [
        """
CREATE TABLE command_verifications (
	command_id UUID NOT NULL,
	applied_at TIMESTAMP WITH TIME ZONE,
	read_back_at TIMESTAMP WITH TIME ZONE,
	status VARCHAR(24) NOT NULL,
	evidence JSON NOT NULL,
	reason VARCHAR(64),
	id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (command_id),
	FOREIGN KEY(command_id) REFERENCES commands (id) ON DELETE CASCADE
)
""",
    ]),
]


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for name, statements in TABLES:
        if "legacy_" + name in existing:
            op.rename_table("legacy_" + name, name)
        else:
            for statement in statements:
                op.execute(statement)
    migrate_legacy_settings()
    op.execute("""
        UPDATE station_controls SET mode = 'suspended', revision = revision + 1,
            reason = 'restored_history_requires_policy_review'
        WHERE mode IN ('assisted', 'automatic')
    """)


def downgrade():
    # Never silently resume physical control after a rollback.
    op.execute("UPDATE stations SET execution_mode = 'shadow' WHERE execution_mode = 'live'")
    for name, _ in reversed(TABLES):
        op.rename_table(name, "legacy_" + name)


def migrate_legacy_settings():
    op.execute("""
        INSERT INTO station_controls (id, station_id, mode, revision, reason)
        SELECT gen_random_uuid(), s.id,
            CASE WHEN s.execution_mode = 'live' THEN 'suspended' ELSE 'shadow' END,
            1, 'migration_requires_explicit_policy_approval'
        FROM stations s WHERE NOT EXISTS (SELECT 1 FROM station_controls c WHERE c.station_id = s.id)
    """)
    op.execute("UPDATE stations SET execution_mode = 'shadow' WHERE execution_mode = 'live'")
    op.execute("""
        INSERT INTO evses (id, station_id, name, source, capabilities, max_power_kw,
            config_snapshot, retention_days, vehicle_data_consent, is_active)
        SELECT gen_random_uuid(), c.station_id, 'EV importat din configuratia existenta',
            'legacy_settings', '{}', c.ev_max_charge_power_kw,
            json_build_object('config_version_id', c.id, 'config_version', c.version,
                              'ev_battery_capacity_kwh', c.ev_battery_capacity_kwh),
            365, false, true
        FROM station_config_versions c
        WHERE c.ev_enabled AND c.version = (SELECT max(v.version) FROM station_config_versions v WHERE v.station_id = c.station_id)
            AND NOT EXISTS (SELECT 1 FROM evses e WHERE e.station_id = c.station_id AND e.source = 'legacy_settings')
    """)
    op.execute("""
        INSERT INTO ev_connectors (id, evse_id, number, state)
        SELECT gen_random_uuid(), e.id, 1, 'unknown' FROM evses e
        WHERE e.source = 'legacy_settings' AND NOT EXISTS (SELECT 1 FROM ev_connectors c WHERE c.evse_id = e.id)
    """)
    op.execute("""
        INSERT INTO ev_requirements (id, connector_id, minimum_energy_kwh, schedule, status)
        SELECT gen_random_uuid(), c.id, p.ev_required_energy_kwh,
            json_build_object('weekdays', json_build_array(0,1,2,3,4,5,6),
                              'local_time', substring(p.ev_departure_time::text from 1 for 5), 'fold', 1),
            'legacy_imported'
        FROM ev_connectors c JOIN evses e ON c.evse_id = e.id
        JOIN preference_versions p ON p.station_id = e.station_id
        WHERE e.source = 'legacy_settings' AND p.ev_required_energy_kwh IS NOT NULL
            AND p.ev_departure_time IS NOT NULL
            AND p.version = (SELECT max(v.version) FROM preference_versions v WHERE v.station_id = p.station_id)
            AND NOT EXISTS (SELECT 1 FROM ev_requirements r WHERE r.connector_id = c.id)
    """)
