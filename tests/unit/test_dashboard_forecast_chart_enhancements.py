from pathlib import Path


def test_pv_forecast_chart_requests_an_extended_horizon_with_a_now_marker():
    """Cerere client: prognoza PV pe o durata mai lunga in fata, ca sa se vada
    dimineata la ce ora incepe productia."""
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert 'lazyLoadWidget("chart-forecast-pv", () => loadForecastChart("pv", 36));' in script
    assert 'lazyLoadWidget("chart-forecast-load", () => loadForecastChart("load"));' in script
    assert "horizon_hours=${horizonHours}" in script
    assert "nowLine: horizonHours > 0" in script
    assert 'label: { formatter: "acum", position: "insideEndTop" }' in script


def test_consumption_forecast_chart_fills_over_and_under_zones_with_distinct_colors():
    """Cerere client: zone distincte, cu doua culori, intre prognoza si
    realizat -- verde cand a consumat sub prognoza, rosu cand a consumat
    peste."""
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert "function forecastAreaFillSeries(data)" in script
    assert '"Consum sub prognoza"' in script
    assert '"Consum peste prognoza"' in script
    assert "rgba(16, 185, 129, 0.35)" in script  # verde -- sub prognoza
    assert "rgba(239, 68, 68, 0.35)" in script  # rosu -- peste prognoza
    assert 'stack: "under"' in script
    assert 'stack: "over"' in script
    # Punctele fara valoare reala (viitor / acoperire insuficienta) raman
    # null, nu 0 -- nu se coloreaza nicio zona unde nu exista date reale.
    assert "hasActual(d) ? d.actual_kw : null" in script
    assert "metric === \"load\"" in script


def test_forecast_tooltip_hides_internal_helper_series():
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert "function forecastTooltipFormatter(metric, pointsByTime, allowedSeries = null)" in script
    assert '["Prognoza", "Realizat"]' in script
