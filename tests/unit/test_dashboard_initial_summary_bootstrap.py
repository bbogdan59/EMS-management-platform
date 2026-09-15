from pathlib import Path


def test_station_dashboard_bootstraps_kpis_from_server_summary_before_sse():
    template = Path("app/web/templates/dashboard/station.html").read_text()
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert 'emsInitDashboard("{{ station.id }}", {{ summary | tojson }})' in template
    assert "function emsInitDashboard(stationId, initialSummary = null)" in script
    assert "if (initialSummary) setKpis(initialSummary);" in script
