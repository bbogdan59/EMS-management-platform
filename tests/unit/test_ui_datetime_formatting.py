from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.web.templating import fmt_local_dt


def test_local_dt_filter_converts_and_labels_station_timezone():
    value = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    assert fmt_local_dt(value, "Europe/Bucharest") == "02.01.2026 05:04 Europe/Bucharest"


def test_local_dt_filter_labels_utc_fallback():
    value = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

    assert fmt_local_dt(value, None) == "02.01.2026 03:04 UTC"


def test_issue_133_templates_use_shared_datetime_filter():
    rendered_templates = {
        "app/web/templates/admin/audit.html": ["e.occurred_at | local_dt(None)"],
        "app/web/templates/admin/operations.html": [
            "j.created_at | local_dt(None)",
            "r.created_at | local_dt(None)",
            "d.last_heartbeat_at | local_dt(None)",
            "c.created_at | local_dt(None)",
        ],
        "app/web/templates/admin/overview.html": [
            "last_opcom.created_at | local_dt(None)",
            "last_optimization.created_at | local_dt(None)",
            "a.created_at | local_dt(None)",
        ],
        "app/web/templates/admin/devices_assigned.html": [
            "d.last_heartbeat_at | local_dt(None)",
        ],
        "app/web/templates/admin/devices_pending.html": [
            "d.enrolled_at | local_dt(None)",
            "d.enrollment_expires_at | local_dt(None)",
        ],
        "app/web/templates/admin/optimization_confirm.html": [
            "active_plan.published_at | local_dt(station.timezone)",
        ],
        "app/web/templates/admin/organization_detail.html": [
            "invitation_reveal.expires_at | local_dt(None)",
        ],
        "app/web/templates/admin/users.html": [
            "row.user.last_login_at | local_dt(None)",
        ],
        "app/web/templates/stations/config.html": [
            "config.created_at | local_dt(station.timezone)",
            "c.created_at | local_dt(station.timezone)",
        ],
        "app/web/templates/stations/deye_integration.html": [
            "connection.last_sync_at | local_dt(station.timezone)",
        ],
        "app/web/templates/stations/devices.html": [
            "d.last_heartbeat_at | local_dt(station.timezone)",
        ],
        "app/web/templates/stations/preferences.html": [
            "automation_suspended_until_local | local_dt(station.timezone)",
        ],
        "app/web/templates/stations/setup_summary.html": [
            "station.setup_completed_at | local_dt(station.timezone)",
        ],
        "app/web/templates/stations/tariffs.html": [
            "v.valid_from | local_dt(station.timezone)",
            "v.valid_to | local_dt(station.timezone)",
        ],
        "app/web/templates/stations/inverter_config.html": [
            "state.command.expires_at | local_dt(station.timezone)",
            "state.reported.measured_at | local_dt(station.timezone)",
            "item.created_at | local_dt(station.timezone)",
        ],
    }

    for template_name, expected_fragments in rendered_templates.items():
        template = Path(template_name).read_text()
        for fragment in expected_fragments:
            assert fragment in template, template_name
        assert "{{ state.command.expires_at }}" not in template
        assert "{{ state.reported.measured_at }}" not in template
        assert "{{ item.created_at }}" not in template
