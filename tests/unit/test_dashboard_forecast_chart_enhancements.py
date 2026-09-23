from pathlib import Path


def test_pv_forecast_chart_requests_an_extended_horizon_with_a_now_marker():
    """Cerere client: prognoza PV pe o durata mai lunga in fata, ca sa se vada
    dimineata la ce ora incepe productia."""
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert 'lazyLoadWidget("chart-forecast-pv", () => loadForecastChart("pv", 36));' in script
    assert 'lazyLoadWidget("chart-forecast-load", () => loadForecastChart("load"));' in script
    assert "horizon_hours=${horizonHours}" in script
    assert "if (horizonHours > 0) {" in script
    assert 'label: { formatter: "acum", position: "insideEndTop" }' in script


def test_forecast_accuracy_threshold_classifies_correct_under_over():
    """Cerere client: o toleranta explicita (+-0.25 kW) in loc de a colora
    orice diferenta oricat de mica intre prognoza si realizat -- aplicata
    identic la PV si la consum."""
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert "const EMS_FORECAST_ACCURACY_THRESHOLD_KW = 0.25;" in script
    assert "function classifyForecastPoint(point, thresholdKw)" in script
    assert "if (diff > thresholdKw) return \"over\";" in script
    assert "if (diff < -thresholdKw) return \"under\";" in script
    assert 'return "correct";' in script
    # null cand nu exista Realizat -- nu clasificam un punct fara comparatie.
    assert "if (point.actual_kw === null || point.actual_kw === undefined) return null;" in script


def test_both_forecast_charts_fill_three_zones_correct_under_over():
    """Cerere client: aceleasi conditii (si aceleasi 3 culori) pe ambele
    grafice -- PV si consum -- nu doar pe cel de consum ca inainte."""
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert "function forecastAreaFillSeries(metric, data, thresholdKw)" in script
    assert "rgba(59, 130, 246, 0.35)" in script  # albastru -- in limita pragului
    assert "rgba(16, 185, 129, 0.35)" in script  # verde -- sub prognoza
    assert "rgba(239, 68, 68, 0.35)" in script  # rosu -- peste prognoza
    assert 'stack: state' in script
    assert 'stackLayer("under", baseUnder)' in script
    assert 'stackLayer("over", baseOver)' in script
    assert 'stackLayer("correct", baseCorrect)' in script
    # Aceeasi functie de umplere e apelata necondiționat de metric -- PV nu
    # mai are o ramura separata fara umplere.
    assert "series: [...forecastAreaFillSeries(metric, data, EMS_FORECAST_ACCURACY_THRESHOLD_KW), ...lineSeries]" in script
    # Punctele fara valoare reala (viitor / acoperire insuficienta) raman
    # null, nu 0 -- nu se coloreaza nicio zona unde nu exista date reale.
    assert "hasActual(d) ? d.actual_kw : null" in script


def test_forecast_labels_and_legend_are_metric_specific():
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert '"Productie in limita prognozei"' in script
    assert '"Productie sub prognoza"' in script
    assert '"Productie peste prognoza"' in script
    assert '"Consum in limita prognozei"' in script
    assert '"Consum sub prognoza"' in script
    assert '"Consum peste prognoza"' in script
    assert 'legend: { data: ["Prognoza", "Realizat", labels.correct, labels.under, labels.over] }' in script


def test_forecast_tooltip_shows_classification_and_hides_internal_helper_series():
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert "function forecastTooltipFormatter(metric, pointsByTime, allowedSeries = null)" in script
    assert '["Prognoza", "Realizat"]' in script
    # Explica starea (corect/sub/peste) direct in tooltip-ul de hover pe punct.
    assert "kW fata de prognoza" in script
    # Bug real gasit in verificarea manuala: axisValue pe un xAxis de tip
    # "time" e un timestamp numeric, nu string-ul ISO din `d.t` -- cautarea
    # in pointsByTime nu se potrivea NICIODATA (nici pentru randul de
    # clasificare, nici pentru contextul meteo PV existent dinainte).
    # row.value pastreaza insa tuplul original [d.t, ...].
    assert "Array.isArray(rows[0].value) ? rows[0].value[0] : rows[0].axisValue" in script


def test_forecast_fill_and_line_colors_do_not_collide_with_default_palette():
    """Verificare manuala: fara culori explicite, paleta implicita echarts
    repartizeaza culorile in ordinea seriilor -- odata ce umplerea are 6
    serii inaintea liniilor Prognoza/Realizat, acestea din urma ajungeau pe
    nuante de rosu/albastru identice cu zonele de stare, imposibil de
    distins. Legenda trebuie sa arate exact culoarea zonei (nu culoarea
    implicita a liniei)."""
    script = Path("app/web/static/js/dashboard.js").read_text()

    assert "itemStyle: { color: FORECAST_STATE_DOT_COLORS[state] }" in script
    assert 'itemStyle: { color: "#f59e0b" }, lineStyle: { color: "#f59e0b" }' in script  # Prognoza
    assert 'itemStyle: { color: "#7c3aed" }, lineStyle: { color: "#7c3aed" }' in script  # Realizat


def test_forecast_accuracy_percentages_rendered_with_hover_explanation():
    """Cerere client: procentul de timp in care prognoza a fost corecta/sub/
    peste, plus un tooltip la hover care explica pragul si culorile."""
    script = Path("app/web/static/js/dashboard.js").read_text()
    template = Path("app/web/templates/dashboard/station.html").read_text()

    assert "function forecastAccuracyStats(data, thresholdKw)" in script
    assert "function updateForecastAccuracy(metric, data)" in script
    assert "updateForecastAccuracy(metric, data);" in script
    assert "updateForecastAccuracy(metric, []);" in script
    # title nativ = tooltip la hover, fara elemente noi de UI.
    assert "el.title = `Comparam" in script

    assert 'id="forecast-pv-accuracy"' in template
    assert 'id="forecast-load-accuracy"' in template
