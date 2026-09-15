/* global echarts, emsConnectSSE */

const EMS_FLOW_DEADBAND_KW = 0.05;
const EMS_FLOW_ACTIVE_STATES = new Set(["measured", "estimated", "simulated"]);
const EMS_FLOW_EDGES = {
  pv_bus: { metric: "pv_power_kw", direction: "PV -> bus", positive: "active" },
  bus_load: { metric: "load_power_kw", direction: "bus -> consum", positive: "active" },
  battery_bus: { metric: "battery_power_kw", direction: "baterie -> bus", negative: "active" },
  bus_battery: { metric: "battery_power_kw", direction: "bus -> baterie", positive: "active" },
  grid_bus: { metric: "grid_power_kw", direction: "retea -> bus", positive: "active" },
  bus_grid: { metric: "grid_power_kw", direction: "bus -> retea", negative: "active" },
  bus_ev: { metric: "ev_power_kw", direction: "bus -> EV", positive: "active" },
};
const EMS_FLOW_METRIC_TO_EDGES = Object.entries(EMS_FLOW_EDGES).reduce((acc, [edge, cfg]) => {
  if (!acc[cfg.metric]) acc[cfg.metric] = [];
  acc[cfg.metric].push(edge);
  return acc;
}, {});

function emsBuildFlowState(metrics) {
  const quality = metrics.data_quality || "missing";
  const values = {};
  const active = [];
  for (const [edge, cfg] of Object.entries(EMS_FLOW_EDGES)) {
    const raw = metrics[cfg.metric];
    let state = "missing";
    let valueKw = null;
    if (raw !== null && raw !== undefined && Number.isFinite(Number(raw))) {
      valueKw = Math.abs(Number(raw));
      const signed = Number(raw);
      if (Math.abs(signed) <= EMS_FLOW_DEADBAND_KW) {
        state = "idle";
        valueKw = 0;
      } else if ((signed > 0 && cfg.positive === "active") || (signed < 0 && cfg.negative === "active")) {
        state = EMS_FLOW_ACTIVE_STATES.has(quality) ? "active" : quality;
        active.push(edge);
      } else {
        state = "idle";
        valueKw = 0;
      }
    }
    values[edge] = {
      metric: cfg.metric,
      direction: cfg.direction,
      state,
      value_kw: valueKw,
      label: valueKw === null ? "-" : `${valueKw.toFixed(2)} kW`,
    };
  }
  return { quality, values, active };
}

window.emsBuildFlowState = emsBuildFlowState;

function emsChartTheme() {
  return document.documentElement.classList.contains("dark") ? "dark" : undefined;
}

function emsInitDashboard(stationId) {
  const $ = (id) => document.getElementById(id);

  function fmt(v, digits = 2) {
    return v === null || v === undefined ? "-" : Number(v).toFixed(digits);
  }

  // Fiecare functie actualizeaza un SINGUR widget KPI, din starea live
  // completa (`s` = `liveMetrics`, mereu la zi). Extrase din fostul `setKpis`
  // monolitic (issue #50, addendum) ca sa poata fi apelate INDIVIDUAL la un
  // `delta` -- un update de `pv_power_kw` nu mai atinge DOM-ul celorlalte 10
  // widget-uri KPI neschimbate. `setKpis` (mai jos) ramane folosit doar la
  // `snapshot`/`replace`, cand chiar toate au nevoie de randare.
  function updatePvKpi(s) { $("kpi-pv").textContent = s.pv_power_kw !== null ? fmt(s.pv_power_kw) + " kW" : "-"; }
  function updateLoadKpi(s) { $("kpi-load").textContent = s.load_power_kw !== null ? fmt(s.load_power_kw) + " kW" : "-"; }
  function updateGridKpi(s) {
    const grid = s.grid_power_kw;
    $("kpi-grid").textContent = grid === null || grid === undefined ? "-" : (grid >= 0 ? "Import " : "Export ") + fmt(Math.abs(grid)) + " kW";
  }
  function updateSocKpi(s) { $("kpi-soc").textContent = s.battery_soc_percent !== null ? fmt(s.battery_soc_percent, 1) + " %" : "-"; }
  function updateBatteryKpi(s) {
    const batt = s.battery_power_kw;
    $("kpi-battery").textContent = batt === null || batt === undefined ? "-" : (batt >= 0 ? "Incarcare " : "Descarcare ") + fmt(Math.abs(batt)) + " kW";
  }
  function updateEvKpi(s) {
    $("kpi-ev").textContent = s.ev_connected === null || s.ev_connected === undefined ? "necunoscut" : (s.ev_connected ? ("conectat" + (s.ev_power_kw ? ", " + fmt(s.ev_power_kw) + " kW" : "")) : "neconectat");
  }
  function updatePriceBuyKpi(s) { $("kpi-price-buy").textContent = s.price_buy_lei_kwh !== null ? fmt(s.price_buy_lei_kwh, 4) + " lei/kWh" : "indisponibil"; }
  function updatePriceSellKpi(s) { $("kpi-price-sell").textContent = s.price_sell_lei_kwh !== null ? fmt(s.price_sell_lei_kwh, 4) + " lei/kWh" : "indisponibil"; }
  function updateAutomationKpi(s) {
    $("kpi-automation").textContent = s.execution_mode === "shadow" ? "Mod shadow (informativ)" : (s.has_active_plan ? "Activa" : "Fara plan activ");
  }
  function updateLastUpdateKpi(s) { $("kpi-last-update").textContent = s.last_update ? new Date(s.last_update).toLocaleString("ro-RO") : "niciodata"; }
  function updateQualityKpi(s) {
    const qualityBadge = $("kpi-quality");
    qualityBadge.className = "badge-" + ({ measured: "ok", estimated: "warn", simulated: "warn", stale: "error", missing: "muted" }[s.data_quality] || "muted");
    qualityBadge.textContent = { measured: "masurat", estimated: "estimat", simulated: "simulat", stale: "invechit", missing: "lipsa" }[s.data_quality] || s.data_quality;
  }

  function updateSourceKpi(s) {
    const sourceBadge = $("kpi-source");
    if (sourceBadge) {
      if (s.telemetry_source === "deye_cloud") {
        sourceBadge.className = "badge-muted";
        sourceBadge.textContent = "sursa: Deye Cloud (doar citire)";
        sourceBadge.title = "Telemetrie importata din contul Deye Cloud, fara dispozitiv EMS local activ. Latenta/rezolutia pot fi diferite fata de un dispozitiv local.";
        sourceBadge.hidden = false;
      } else {
        sourceBadge.hidden = true;
      }
    }
  }

  // Metrica SSE (issue #50) -> widget-ul KPI pe care il afecteaza. Mai multe
  // metrici pot alimenta acelasi widget compus (ex. `ev_connected` +
  // `ev_power_kw` -> "kpi-ev"), dar niciun update nu mai atinge widget-uri
  // neafectate de metrica primita.
  const kpiWidgetByMetric = {
    pv_power_kw: updatePvKpi,
    load_power_kw: updateLoadKpi,
    grid_power_kw: updateGridKpi,
    battery_soc_percent: updateSocKpi,
    battery_power_kw: updateBatteryKpi,
    ev_connected: updateEvKpi,
    ev_power_kw: updateEvKpi,
    price_buy_lei_kwh: updatePriceBuyKpi,
    price_sell_lei_kwh: updatePriceSellKpi,
    execution_mode: updateAutomationKpi,
    has_active_plan: updateAutomationKpi,
    last_update: updateLastUpdateKpi,
    data_quality: updateQualityKpi,
    telemetry_source: updateSourceKpi,
  };
  const FLOW_DIAGRAM_METRICS = new Set(["pv_power_kw", "load_power_kw", "battery_power_kw", "grid_power_kw", "ev_power_kw", "data_quality", "telemetry_source"]);

  function setKpis(s) {
    for (const widget of new Set(Object.values(kpiWidgetByMetric))) widget(s);
    updateFlowDiagram(s);
  }

  const flowController = {
    edges: new Map(),
    labels: new Map(),
    runners: new Map(),
    reducedMotion: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    initialized: false,
  };

  function initFlowDiagram() {
    if (flowController.initialized) return;
    const svg = $("energy-flow-diagram");
    if (!svg) return;
    for (const edgeEl of svg.querySelectorAll("[data-flow-edge]")) {
      flowController.edges.set(edgeEl.dataset.flowEdge, window.SVG ? SVG(edgeEl) : null);
    }
    for (const labelEl of svg.querySelectorAll("[data-flow-label]")) {
      flowController.labels.set(labelEl.dataset.flowLabel, labelEl);
    }
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    motion.addEventListener("change", (ev) => {
      flowController.reducedMotion = ev.matches;
      if (ev.matches) stopFlowAnimations();
      else updateFlowDiagram(liveMetrics);
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopFlowAnimations();
      else if (!flowController.reducedMotion) updateFlowDiagram(liveMetrics);
    });
    window.addEventListener("beforeunload", stopFlowAnimations);
    flowController.initialized = true;
  }

  function stopFlowAnimation(edge) {
    const runner = flowController.runners.get(edge);
    if (runner && typeof runner.unschedule === "function") runner.unschedule();
    else if (runner && typeof runner.stop === "function") runner.stop();
    flowController.runners.delete(edge);
  }

  function stopFlowAnimations() {
    for (const edge of flowController.runners.keys()) stopFlowAnimation(edge);
  }

  function setFlowAnimation(edge, isActive, speedMs) {
    const item = flowController.edges.get(edge);
    if (!item) return;
    stopFlowAnimation(edge);
    if (!isActive || flowController.reducedMotion || document.hidden) return;
    item.attr({ "stroke-dasharray": "8 10", "stroke-dashoffset": 0 });
    const runner = item.animate(speedMs).attr({ "stroke-dashoffset": -36 }).loop();
    flowController.runners.set(edge, runner);
  }

  function updateFlowDiagram(s, changedMetrics = null) {
    initFlowDiagram();
    const diagram = $("energy-flow-diagram");
    if (!diagram) return;
    const state = emsBuildFlowState(s);
    const affectedEdges = changedMetrics
      ? new Set([...changedMetrics].flatMap((metric) => EMS_FLOW_METRIC_TO_EDGES[metric] || []))
      : new Set(Object.keys(EMS_FLOW_EDGES));
    if (changedMetrics && (changedMetrics.has("data_quality") || changedMetrics.has("telemetry_source"))) {
      for (const edge of Object.keys(EMS_FLOW_EDGES)) affectedEdges.add(edge);
    }

    diagram.style.filter = ["stale", "missing"].includes(state.quality) ? "grayscale(1)" : "";
    for (const edge of affectedEdges) {
      const edgeState = state.values[edge];
      const item = flowController.edges.get(edge);
      const label = flowController.labels.get(edge);
      if (!edgeState || !item) continue;
      const isActive = edgeState.state === "active";
      const isMissing = edgeState.state === "missing" || edgeState.state === "stale";
      const width = isActive ? Math.min(7, 3 + edgeState.value_kw * 0.45) : 3;
      const opacity = isActive ? 1 : (isMissing ? 0.1 : 0.22);
      item.animate(400).attr({ opacity, "stroke-width": width });
      if (!isActive) item.attr({ "stroke-dasharray": "4 8", "stroke-dashoffset": 0 });
      else setFlowAnimation(edge, true, Math.max(650, 1800 - edgeState.value_kw * 90));
      if (label) {
        label.textContent = edgeState.label;
        label.setAttribute("opacity", isMissing ? "0.55" : "1");
      }
    }

    const summaryEl = $("energy-flow-summary");
    if (summaryEl) {
      const activeText = state.active.map((edge) => `${state.values[edge].direction}: ${state.values[edge].label}`);
      summaryEl.textContent = activeText.length ? activeText.join(" | ") : "Fara fluxuri active peste pragul de 0.05 kW.";
    }
    const provenance = $("energy-flow-provenance");
    if (provenance) {
      const special = s.data_quality === "estimated" || s.data_quality === "simulated" || s.data_quality === "stale";
      provenance.hidden = !special;
      provenance.textContent = special ? `date: ${s.data_quality}` : "";
      provenance.className = s.data_quality === "stale" ? "badge-error" : "badge-warn";
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
    const widget = widgetCard("chart-prices");
    if (!widget) return;
    wireRetry(widget, loadPricesChart);
    try {
      const [today, tomorrow] = await Promise.all([
        fetchJson(`/stations/${stationId}/data/prices?day=today`),
        fetchJson(`/stations/${stationId}/data/prices?day=tomorrow`),
      ]);
      if (!today.intervals.length && !tomorrow.intervals.length) { showWidgetState(widget, "empty"); return; }
      showWidgetState(widget, "ok");
      const chart = echarts.init(widget.chartEl, emsChartTheme());
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
    } catch (e) {
      console.error(e);
      showWidgetState(widget, "error");
    }
  }

  async function loadPlanChart() {
    const widget = widgetCard("chart-plan");
    if (!widget) return;
    wireRetry(widget, loadPlanChart);
    try {
      const data = await fetchJson(`/stations/${stationId}/data/plan`);
      if (!data.plan) { showWidgetState(widget, "empty"); return; }
      showWidgetState(widget, "ok");
      $("plan-status-badge").textContent = `${data.plan.status} (${data.plan.execution_mode})`;
      const chart = echarts.init(widget.chartEl, emsChartTheme());
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
    } catch (e) {
      console.error(e);
      showWidgetState(widget, "error");
    }
  }

  async function loadForecastChart(metric) {
    const widget = widgetCard("chart-forecast-" + metric);
    if (!widget) return;
    wireRetry(widget, () => loadForecastChart(metric));
    try {
      const data = await fetchJson(`/stations/${stationId}/data/forecast-vs-actual?metric=${metric}&range=24h`);
      if (!data.length) { showWidgetState(widget, "empty"); return; }
      showWidgetState(widget, "ok");
      lineChart(widget.chartEl, [
        { name: "Prognoza", type: "line", showSymbol: false, data: data.map((d) => [d.t, d.forecast_kw]) },
        { name: "Realizat", type: "line", showSymbol: false, data: data.map((d) => [d.t, d.actual_kw]) },
      ], { yName: "kW" });
    } catch (e) {
      console.error(e);
      showWidgetState(widget, "error");
    }
  }

  async function loadHeatmap() {
    const widget = widgetCard("chart-heatmap");
    if (!widget) return;
    wireRetry(widget, loadHeatmap);
    try {
      const data = await fetchJson(`/stations/${stationId}/data/heatmap`);
      if (!data.length) { showWidgetState(widget, "empty"); return; }
      showWidgetState(widget, "ok");
      const days = ["Luni", "Marti", "Miercuri", "Joi", "Vineri", "Sambata", "Duminica"];
      const chart = echarts.init(widget.chartEl, emsChartTheme());
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
    } catch (e) {
      console.error(e);
      showWidgetState(widget, "error");
    }
  }

  async function loadDailyEnergyTotals() {
    const widget = widgetCard("chart-energy-daily");
    if (!widget) return;
    wireRetry(widget, loadDailyEnergyTotals);
    try {
      const daily = await fetchJson(`/stations/${stationId}/data/energy-totals?granularity=day&periods=30`);
      if (!daily.length) { showWidgetState(widget, "empty"); return; }
      showWidgetState(widget, "ok");
      const chart = echarts.init(widget.chartEl, emsChartTheme());
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
    } catch (e) {
      console.error(e);
      showWidgetState(widget, "error");
    }
  }

  async function loadMonthlyEnergyTotals() {
    const widget = widgetCard("chart-energy-monthly");
    if (!widget) return;
    wireRetry(widget, loadMonthlyEnergyTotals);
    try {
      const monthly = await fetchJson(`/stations/${stationId}/data/energy-totals?granularity=month&periods=12`);
      if (!monthly.length) { showWidgetState(widget, "empty"); return; }
      showWidgetState(widget, "ok");
      const chart = echarts.init(widget.chartEl, emsChartTheme());
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
    } catch (e) {
      console.error(e);
      showWidgetState(widget, "error");
    }
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
    const diagram = $("energy-flow-diagram");
    if (diagram && (status === "stale" || status === "offline")) diagram.style.filter = "grayscale(1)";
    else if (diagram && liveMetrics.data_quality !== "stale" && liveMetrics.data_quality !== "missing") diagram.style.filter = "";
  }

  function applyMetrics(metrics, { replace = false } = {}) {
    if (replace) liveMetrics = {};
    for (const m of metrics) liveMetrics[m.metric] = m.value;

    if (replace) {
      // Snapshot-ul de la (re)conectare acopera oricum toate metricile --
      // singurul caz in care o randare completa e proportionala cu datele.
      setKpis(liveMetrics);
    } else {
      // Delta (issue #50 addendum): actualizeaza DOAR widget-urile afectate
      // de metricile chiar primite in acest eveniment, nu toate cele 11.
      const widgetsToUpdate = new Set();
      let flowDiagramAffected = false;
      const changedMetrics = new Set();
      for (const m of metrics) {
        changedMetrics.add(m.metric);
        const widget = kpiWidgetByMetric[m.metric];
        if (widget) widgetsToUpdate.add(widget);
        if (FLOW_DIAGRAM_METRICS.has(m.metric)) flowDiagramAffected = true;
      }
      for (const widget of widgetsToUpdate) widget(liveMetrics);
      if (flowDiagramAffected) updateFlowDiagram(liveMetrics, changedMetrics);
    }

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
  loadDailyEnergyTotals();
  loadMonthlyEnergyTotals();
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
