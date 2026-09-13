/* global echarts, emsConnectSSE */

function emsChartTheme() {
  return document.documentElement.classList.contains("dark") ? "dark" : undefined;
}

function emsInitDashboard(stationId) {
  const $ = (id) => document.getElementById(id);

  function fmt(v, digits = 2) {
    return v === null || v === undefined ? "-" : Number(v).toFixed(digits);
  }

  function setKpis(s) {
    $("kpi-pv").textContent = s.pv_power_kw !== null ? fmt(s.pv_power_kw) + " kW" : "-";
    $("kpi-load").textContent = s.load_power_kw !== null ? fmt(s.load_power_kw) + " kW" : "-";
    const grid = s.grid_power_kw;
    if (grid === null || grid === undefined) {
      $("kpi-grid").textContent = "-";
    } else {
      $("kpi-grid").textContent = (grid >= 0 ? "Import " : "Export ") + fmt(Math.abs(grid)) + " kW";
    }
    $("kpi-soc").textContent = s.battery_soc_percent !== null ? fmt(s.battery_soc_percent, 1) + " %" : "-";
    const batt = s.battery_power_kw;
    $("kpi-battery").textContent = batt === null || batt === undefined ? "-" : (batt >= 0 ? "Incarcare " : "Descarcare ") + fmt(Math.abs(batt)) + " kW";
    $("kpi-ev").textContent = s.ev_connected === null || s.ev_connected === undefined ? "necunoscut" : (s.ev_connected ? ("conectat" + (s.ev_power_kw ? ", " + fmt(s.ev_power_kw) + " kW" : "")) : "neconectat");
    $("kpi-price-buy").textContent = s.price_buy_lei_kwh !== null ? fmt(s.price_buy_lei_kwh, 4) + " lei/kWh" : "indisponibil";
    $("kpi-price-sell").textContent = s.price_sell_lei_kwh !== null ? fmt(s.price_sell_lei_kwh, 4) + " lei/kWh" : "indisponibil";
    $("kpi-automation").textContent = s.execution_mode === "shadow" ? "Mod shadow (informativ)" : (s.has_active_plan ? "Activa" : "Fara plan activ");
    $("kpi-last-update").textContent = s.last_update ? new Date(s.last_update).toLocaleString("ro-RO") : "niciodata";

    const qualityBadge = $("kpi-quality");
    qualityBadge.className = "badge-" + ({ measured: "ok", estimated: "warn", simulated: "warn", stale: "error", missing: "muted" }[s.data_quality] || "muted");
    qualityBadge.textContent = { measured: "masurat", estimated: "estimat", simulated: "simulat", stale: "invechit", missing: "lipsa" }[s.data_quality] || s.data_quality;

    updateFlowDiagram(s);
  }

  function updateFlowDiagram(s) {
    const flows = {
      "flow-pv-home": s.pv_power_kw && s.pv_power_kw > 0.05,
      "flow-battery-home": s.battery_power_kw && s.battery_power_kw < -0.05,
      "flow-home-battery": s.battery_power_kw && s.battery_power_kw > 0.05,
      "flow-grid-home": s.grid_power_kw && s.grid_power_kw > 0.05,
      "flow-home-grid": s.grid_power_kw && s.grid_power_kw < -0.05,
      "flow-home-ev": s.ev_power_kw && s.ev_power_kw > 0.05,
    };
    for (const [id, active] of Object.entries(flows)) {
      const el = document.getElementById(id);
      if (el) el.style.opacity = active ? "1" : "0.12";
    }
  }

  async function fetchJson(url, { signal, timeoutMs = 15000 } = {}) {
    // Timeout propriu, independent de un eventual `signal` extern (folosit
    // pentru cancel la schimbarea intervalului) -- oricare din cele doua
    // poate opri cererea, fara sa blocheze restul paginii (issue #33).
    const timeoutController = new AbortController();
    const timer = setTimeout(() => timeoutController.abort(), timeoutMs);
    const onExternalAbort = () => timeoutController.abort();
    if (signal) signal.addEventListener("abort", onExternalAbort);
    try {
      const res = await fetch(url, { headers: { Accept: "application/json" }, signal: timeoutController.signal });
      if (!res.ok) throw new Error("HTTP " + res.status);
      return await res.json();
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onExternalAbort);
    }
  }

  // Widget independent: incarcare proprie, empty-state gri distinct de "zero",
  // eroare izolata cu retry -- fara sa blocheze restul paginii (issue #33).
  function widgetCard(chartElId) {
    const chartEl = $(chartElId);
    if (!chartEl) return null;
    const card = chartEl.closest(".card");
    return {
      chartEl,
      emptyEl: card ? card.querySelector(".empty-state") : null,
      errorEl: card ? card.querySelector(".error-state") : null,
    };
  }

  function showWidgetState(widget, state) {
    if (!widget) return;
    const { chartEl, emptyEl, errorEl } = widget;
    if (emptyEl) emptyEl.hidden = state !== "empty";
    if (errorEl) errorEl.hidden = state !== "error";
    chartEl.hidden = state === "empty" || state === "error";
  }

  function wireRetry(widget, loadFn) {
    if (!widget || !widget.errorEl) return;
    const btn = widget.errorEl.querySelector(".retry-btn");
    if (btn && !btn.dataset.wired) {
      btn.dataset.wired = "1";
      btn.addEventListener("click", () => loadFn());
    }
  }

  function lineChart(el, series, opts = {}) {
    const chart = echarts.init(el, emsChartTheme());
    chart.setOption({
      grid: { left: 48, right: 16, top: 24, bottom: 32 },
      tooltip: { trigger: "axis" },
      legend: opts.legend !== false ? {} : undefined,
      xAxis: { type: "time" },
      yAxis: { type: "value", name: opts.yName || "" },
      series: series,
    });
    return chart;
  }

  // Cache scurt pe URL exacta (interval inclus) -- evita re-fetch-uri
  // redundante la re-render-uri apropiate, fara sa pretinda date "live".
  const timeseriesCache = new Map(); // url -> { data, ts }
  const TIMESERIES_CACHE_TTL_MS = 30000;
  let powerChartController = null;
  let socChartController = null;

  async function fetchTimeseries(range, controller) {
    const url = `/stations/${stationId}/data/timeseries?range=${range}`;
    const cached = timeseriesCache.get(url);
    if (cached && Date.now() - cached.ts < TIMESERIES_CACHE_TTL_MS) return cached.data;
    const data = await fetchJson(url, { signal: controller.signal });
    timeseriesCache.set(url, { data, ts: Date.now() });
    return data;
  }

  function describeResolution(data) {
    const pct = data.coverage !== null && data.coverage !== undefined ? Math.round(data.coverage * 100) : null;
    const method = (data.aggregation && data.aggregation.pv_kw) || (data.aggregation && data.aggregation.soc_pct) || "medie";
    return `rezolutie ${data.resolution} (${method})` + (pct !== null ? ` · acoperire ${pct}%` : "");
  }

  async function loadPowerChart(range) {
    const widget = widgetCard("chart-power");
    if (!widget) return;
    wireRetry(widget, () => loadPowerChart(range));
    // Cancel la schimbarea intervalului (issue #33): o cerere veche, inca in
    // zbor, nu trebuie sa mai apuce sa deseneze peste raspunsul cererii noi.
    if (powerChartController) powerChartController.abort();
    const controller = new AbortController();
    powerChartController = controller;
    try {
      const data = await fetchTimeseries(range, controller);
      if (controller !== powerChartController) return; // inlocuita intre timp
      const points = data.points || [];
      const resEl = $("chart-power-resolution");
      if (resEl) resEl.textContent = describeResolution(data);
      if (!points.length) { showWidgetState(widget, "empty"); return; }
      showWidgetState(widget, "ok");
      const mk = (key, name) => ({ name, type: "line", showSymbol: false, data: points.map((d) => [d.t, d[key]]) });
      lineChart(widget.chartEl, [mk("pv_kw", "PV"), mk("load_kw", "Consum"), mk("battery_kw", "Baterie"), mk("grid_kw", "Retea")], { yName: "kW" });
    } catch (e) {
      if (controller !== powerChartController) return; // inlocuita/anulata intre timp, nu e o eroare de afisat
      console.error(e);
      showWidgetState(widget, "error");
    }
  }

  async function loadSocChart(range) {
    const widget = widgetCard("chart-soc");
    if (!widget) return;
    wireRetry(widget, () => loadSocChart(range));
    if (socChartController) socChartController.abort();
    const controller = new AbortController();
    socChartController = controller;
    try {
      const data = await fetchTimeseries(range, controller);
      if (controller !== socChartController) return;
      const points = data.points || [];
      const resEl = $("chart-soc-resolution");
      if (resEl) resEl.textContent = describeResolution(data);
      if (!points.length) { showWidgetState(widget, "empty"); return; }
      showWidgetState(widget, "ok");
      lineChart(widget.chartEl, [{ name: "SOC", type: "line", showSymbol: false, areaStyle: {}, data: points.map((d) => [d.t, d.soc_pct]) }], { yName: "%", legend: false });
    } catch (e) {
      if (controller !== socChartController) return;
      console.error(e);
      showWidgetState(widget, "error");
    }
  }

  async function loadPricesChart() {
    const el = $("chart-prices");
    if (!el) return;
    try {
      const [today, tomorrow] = await Promise.all([
        fetchJson(`/stations/${stationId}/data/prices?day=today`),
        fetchJson(`/stations/${stationId}/data/prices?day=tomorrow`),
      ]);
      const chart = echarts.init(el, emsChartTheme());
      const mkBar = (payload, name) => ({
        name,
        type: "bar",
        data: payload.intervals.map((i) => [i.t, i.price_lei_kwh]),
      });
      chart.setOption({
        grid: { left: 48, right: 16, top: 32, bottom: 32 },
        tooltip: { trigger: "axis" },
        legend: {},
        xAxis: { type: "time" },
        yAxis: { type: "value", name: "lei/kWh" },
        series: [mkBar(today, "Azi"), mkBar(tomorrow, "Maine")],
      });
      $("prices-tomorrow-status").textContent = tomorrow.published ? "" : "Preturile de maine nu au fost inca publicate.";
    } catch (e) { console.error(e); }
  }

  async function loadPlanChart() {
    const el = $("chart-plan");
    if (!el) return;
    try {
      const data = await fetchJson(`/stations/${stationId}/data/plan`);
      const empty = el.closest(".card").querySelector(".empty-state");
      if (!data.plan) { empty.hidden = false; return; }
      empty.hidden = true;
      $("plan-status-badge").textContent = `${data.plan.status} (${data.plan.execution_mode})`;
      const chart = echarts.init(el, emsChartTheme());
      const series = [
        { name: "Baterie (plan)", type: "bar", data: data.intervals.map((i) => [i.t, i.battery_kw]) },
        { name: "Retea (plan)", type: "bar", data: data.intervals.map((i) => [i.t, i.grid_kw]) },
        { name: "SOC tinta", type: "line", yAxisIndex: 0, data: data.intervals.map((i) => [i.t, i.soc_target_pct]) },
      ];
      // Efectul REAL (reconciliat ulterior din telemetrie) e afisat doar daca
      // exista deja cel putin o valoare -- altfel ar aparea o serie goala in
      // legenda, inainte ca reconcilierea sa produca vreodata date.
      if (data.intervals.some((i) => i.observed_battery_kw !== null)) {
        series.push({ name: "Baterie (real)", type: "line", showSymbol: false, data: data.intervals.map((i) => [i.t, i.observed_battery_kw]) });
      }
      if (data.intervals.some((i) => i.observed_grid_kw !== null)) {
        series.push({ name: "Retea (real)", type: "line", showSymbol: false, data: data.intervals.map((i) => [i.t, i.observed_grid_kw]) });
      }
      if (data.intervals.some((i) => i.observed_soc_pct !== null)) {
        series.push({ name: "SOC (real)", type: "line", showSymbol: false, data: data.intervals.map((i) => [i.t, i.observed_soc_pct]) });
      }
      chart.setOption({
        grid: { left: 48, right: 16, top: 24, bottom: 32 },
        tooltip: { trigger: "axis" },
        legend: {},
        xAxis: { type: "time" },
        yAxis: { type: "value", name: "kW / %" },
        series,
      });
    } catch (e) { console.error(e); }
  }

  async function loadForecastChart(metric) {
    const el = $("chart-forecast-" + metric);
    if (!el) return;
    try {
      const data = await fetchJson(`/stations/${stationId}/data/forecast-vs-actual?metric=${metric}&range=24h`);
      const empty = el.closest(".card").querySelector(".empty-state");
      if (!data.length) { empty.hidden = false; return; }
      empty.hidden = true;
      lineChart(el, [
        { name: "Prognoza", type: "line", showSymbol: false, data: data.map((d) => [d.t, d.forecast_kw]) },
        { name: "Realizat", type: "line", showSymbol: false, data: data.map((d) => [d.t, d.actual_kw]) },
      ], { yName: "kW" });
    } catch (e) { console.error(e); }
  }

  async function loadHeatmap() {
    const el = $("chart-heatmap");
    if (!el) return;
    try {
      const data = await fetchJson(`/stations/${stationId}/data/heatmap`);
      const empty = el.closest(".card").querySelector(".empty-state");
      if (!data.length) { empty.hidden = false; return; }
      empty.hidden = true;
      const days = ["Luni", "Marti", "Miercuri", "Joi", "Vineri", "Sambata", "Duminica"];
      const chart = echarts.init(el, emsChartTheme());
      const values = data.map((d) => [d.hour, d.weekday, Number(d.avg_load_kwh.toFixed(3))]);
      const max = Math.max(...values.map((v) => v[2]), 0.1);
      chart.setOption({
        tooltip: { position: "top" },
        grid: { left: 60, right: 16, top: 16, bottom: 32 },
        xAxis: { type: "category", data: [...Array(24).keys()], name: "Ora" },
        yAxis: { type: "category", data: days },
        visualMap: { min: 0, max, calculable: true, orient: "horizontal", left: "center", bottom: 0 },
        series: [{ type: "heatmap", data: values, label: { show: false } }],
      });
    } catch (e) { console.error(e); }
  }

  async function loadEnergyTotals() {
    const el = $("chart-energy-daily");
    const elMonthly = $("chart-energy-monthly");
    try {
      const daily = await fetchJson(`/stations/${stationId}/data/energy-totals?granularity=day&periods=30`);
      if (el) {
        if (!daily.length) { el.closest(".card").querySelector(".empty-state").hidden = false; }
        else {
          el.closest(".card").querySelector(".empty-state").hidden = true;
          const chart = echarts.init(el, emsChartTheme());
          chart.setOption({
            grid: { left: 48, right: 16, top: 24, bottom: 48 },
            tooltip: { trigger: "axis" },
            legend: {},
            xAxis: { type: "category", data: daily.map((d) => d.period_start.slice(0, 10)), axisLabel: { rotate: 45 } },
            yAxis: { type: "value", name: "kWh" },
            series: [
              { name: "PV", type: "bar", stack: "e", data: daily.map((d) => d.pv_kwh) },
              { name: "Import", type: "bar", stack: "i", data: daily.map((d) => d.grid_import_kwh) },
              { name: "Export", type: "bar", stack: "x", data: daily.map((d) => -d.grid_export_kwh) },
            ],
          });
        }
      }
      const monthly = await fetchJson(`/stations/${stationId}/data/energy-totals?granularity=month&periods=12`);
      if (elMonthly) {
        if (!monthly.length) { elMonthly.closest(".card").querySelector(".empty-state").hidden = false; }
        else {
          elMonthly.closest(".card").querySelector(".empty-state").hidden = true;
          const chart = echarts.init(elMonthly, emsChartTheme());
          chart.setOption({
            grid: { left: 48, right: 16, top: 24, bottom: 48 },
            tooltip: { trigger: "axis" },
            legend: {},
            xAxis: { type: "category", data: monthly.map((d) => d.period_start.slice(0, 7)) },
            yAxis: { type: "value", name: "kWh" },
            series: [
              { name: "PV", type: "bar", data: monthly.map((d) => d.pv_kwh) },
              { name: "Consum", type: "bar", data: monthly.map((d) => d.load_kwh) },
            ],
          });
        }
      }
    } catch (e) { console.error(e); }
  }

  async function loadEfcAndSavings() {
    try {
      const efc = await fetchJson(`/stations/${stationId}/data/efc?range=30d`);
      $("kpi-efc").textContent = efc.efc_used !== null && efc.efc_used !== undefined ? fmt(efc.efc_used, 2) + " cicluri (30 zile)" : "indisponibil";
    } catch (e) { console.error(e); }
    try {
      const savings = await fetchJson(`/stations/${stationId}/data/savings?range=30d`);
      if (savings.available) {
        $("kpi-savings").textContent = fmt(savings.whole_system_benefit_lei) + " lei (30 zile)";
        $("kpi-savings-note").textContent = savings.whole_system_baseline_description;
        $("kpi-ems-benefit").textContent = fmt(savings.ems_incremental_benefit_lei) + " lei (30 zile)";
        $("kpi-ems-benefit-note").textContent = savings.ems_incremental_baseline_description;
        const coveragePct = savings.coverage_ratio !== null ? Math.round(savings.coverage_ratio * 100) : null;
        $("kpi-savings-coverage").textContent = coveragePct !== null
          ? `Acoperire date: ${coveragePct}% din interval (${savings.hours_priced}/${savings.hours_expected} ore).`
          : "";

        // Detaliere financiara (issue #49) -- fiecare card isi arata formula in
        // tooltip (title), ca sa nu fie confundate una cu alta sau cu "economia totala".
        $("kpi-gross-pv-value").textContent = fmt(savings.gross_pv_value_lei) + " lei (30 zile)";
        $("kpi-gross-pv-value-label").title = savings.gross_pv_value_description || "";
        $("kpi-self-consumption").textContent = fmt(savings.self_consumption_savings_lei) + " lei (30 zile)";
        $("kpi-self-consumption-label").title = savings.self_consumption_savings_description || "";
        let exportText = fmt(savings.export_revenue_lei) + " lei (30 zile)";
        if (savings.hours_export_price_missing > 0) {
          exportText += ` (${savings.hours_export_price_missing} ore cu export excluse: tarif necunoscut)`;
        }
        $("kpi-export-revenue").textContent = exportText;
        $("kpi-export-revenue-label").title = savings.export_revenue_description || "";

        const provenanceEl = $("kpi-tariff-provenance");
        if (provenanceEl) {
          const isMeasured = savings.tariff_provenance_summary === "measured";
          provenanceEl.textContent = isMeasured ? "tarif import: masurat" : "tarif import: estimat (date de test)";
          provenanceEl.className = isMeasured ? "badge-ok" : "badge-warn";
        }
      } else {
        $("kpi-savings").textContent = "indisponibil";
        $("kpi-savings-note").textContent = savings.reason || "";
        $("kpi-ems-benefit").textContent = "indisponibil";
        $("kpi-ems-benefit-note").textContent = "";
        $("kpi-savings-coverage").textContent = "";
        $("kpi-gross-pv-value").textContent = "indisponibil";
        $("kpi-self-consumption").textContent = "indisponibil";
        $("kpi-export-revenue").textContent = "indisponibil";
        const provenanceEl = $("kpi-tariff-provenance");
        if (provenanceEl) {
          provenanceEl.textContent = "-";
          provenanceEl.className = "badge-muted";
        }
      }
    } catch (e) { console.error(e); }
  }

  // Actualizare live (issue #50): fiecare eveniment SSE poarta o lista de
  // metrici versionate (metric/value/unit/measured_at/received_at/quality/
  // source), nu un rezumat monolitic -- vezi docs/adr/0001-realtime-dashboard-transport.md.
  // `snapshot` inlocuieste tot starea locala; `delta` doar suprascrie
  // metricile schimbate. Un `snapshot` soseste la FIECARE (re)conectare
  // (nu doar prima data) -- serverul nu promite continuitate de secventa
  // peste o reconectare.
  let liveMetrics = {};
  let lastMessageAt = null;
  const STALE_AFTER_MS = 3 * 5000; // 3 x POLL_INTERVAL_SECONDS (server)

  function setConnectionStatus(status) {
    const el = $("sse-status");
    if (!el) return;
    const labels = { connecting: "conectare...", live: "live", stale: "intarziat", offline: "deconectat" };
    const classes = { connecting: "badge-warn", live: "badge-ok", stale: "badge-warn", offline: "badge-error" };
    el.textContent = labels[status] || status;
    el.className = classes[status] || "badge-muted";
  }

  function applyMetrics(metrics, { replace = false } = {}) {
    if (replace) liveMetrics = {};
    for (const m of metrics) liveMetrics[m.metric] = m.value;
    setKpis(liveMetrics);
    lastMessageAt = Date.now();
    setConnectionStatus("live");
  }

  function initSSE() {
    setConnectionStatus("connecting");
    emsConnectSSE(`/stations/${stationId}/sse`, {
      onopen: () => setConnectionStatus("connecting"), // devine "live" abia la primul snapshot
      onerror: () => setConnectionStatus("connecting"),
      events: {
        snapshot: (ev) => applyMetrics(JSON.parse(ev.data).metrics, { replace: true }),
        delta: (ev) => applyMetrics(JSON.parse(ev.data).metrics),
        heartbeat: () => { lastMessageAt = Date.now(); setConnectionStatus("live"); },
      },
    });

    setInterval(() => {
      if (lastMessageAt !== null && Date.now() - lastMessageAt > STALE_AFTER_MS) {
        setConnectionStatus("stale");
      }
    }, 5000);

    window.addEventListener("offline", () => setConnectionStatus("offline"));
    window.addEventListener("online", () => setConnectionStatus("connecting"));
  }

  // Bootstrap initial
  loadPowerChart("24h");
  loadSocChart("24h");
  loadPricesChart();
  loadPlanChart();
  loadForecastChart("pv");
  loadForecastChart("load");
  loadHeatmap();
  loadEnergyTotals();
  loadEfcAndSavings();
  initSSE();

  const rangeSelect = $("range-select");
  if (rangeSelect) {
    rangeSelect.addEventListener("change", () => {
      loadPowerChart(rangeSelect.value);
      loadSocChart(rangeSelect.value);
      const exportLink = $("export-link");
      if (exportLink) exportLink.href = `/stations/${stationId}/export.csv?range=${rangeSelect.value}`;
    });
  }
}
