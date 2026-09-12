/* global echarts, emsChartTheme */

const EMS_YEAR_COLORS = ["#2f9354", "#4a86e8", "#f5a524", "#a479e2", "#f691b2", "#43d692"];
const TIMELINE_RANGE_OPTIONS = [30, 90, 180, 365];
const TIMELINE_DEFAULT_DAYS = 30;

function emsInitMarket(availableYears) {
  const $ = (id) => document.getElementById(id);
  let selectedYears = new Set(availableYears.slice(-3));
  let selectedTimelineDays = TIMELINE_DEFAULT_DAYS;

  async function fetchJson(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  function showEmpty(el, isEmpty) {
    const empty = el.closest(".card").querySelector(".empty-state");
    if (empty) empty.hidden = !isEmpty;
  }

  function renderYearToggles() {
    const container = $("year-toggles");
    if (!container) return;
    container.innerHTML = "";
    availableYears.forEach((year, idx) => {
      const label = document.createElement("label");
      label.className = "flex items-center gap-1";
      const color = EMS_YEAR_COLORS[idx % EMS_YEAR_COLORS.length];
      label.innerHTML = `<input type="checkbox" data-year="${year}" ${selectedYears.has(year) ? "checked" : ""} />
        <span style="color:${color}">&#9679;</span> ${year}`;
      container.appendChild(label);
      label.querySelector("input").addEventListener("change", (ev) => {
        if (ev.target.checked) selectedYears.add(year);
        else selectedYears.delete(year);
        loadYearlyOverlay();
        loadMonthly();
      });
    });
  }

  function renderTimelineRangeToggles() {
    const container = $("timeline-range-toggles");
    if (!container) return;
    container.innerHTML = "";
    TIMELINE_RANGE_OPTIONS.forEach((days) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = days + " zile";
      btn.className = days === selectedTimelineDays ? "btn-primary !px-2 !py-1 text-xs" : "btn-secondary !px-2 !py-1 text-xs";
      btn.addEventListener("click", () => {
        if (days === selectedTimelineDays) return;
        selectedTimelineDays = days;
        renderTimelineRangeToggles();
        loadTimeline();
      });
      container.appendChild(btn);
    });
  }

  async function loadTimeline() {
    const el = $("chart-timeline");
    if (!el) return;
    try {
      // Fiecare fereastra e o cerere separata, declansata la cerere (buton) --
      // pagina nu incarca niciodata tot istoricul dintr-o singura cerere
      // initiala, indiferent cat de mare e intervalul selectat (issue #33).
      const data = await fetchJson("/market/data/timeline?days=" + selectedTimelineDays);
      showEmpty(el, data.length === 0);
      if (!data.length) return;

      const firstFutureIdx = data.findIndex((d) => d.is_future);
      const boundaryIdx = firstFutureIdx === -1 ? data.length : firstFutureIdx;

      const historical = data.map((d, i) => [d.t, i < boundaryIdx ? d.price_lei_mwh : null]);
      const forecast = data.map((d, i) => [d.t, i >= boundaryIdx - 1 && i >= 0 ? d.price_lei_mwh : null]);

      const chart = echarts.init(el, emsChartTheme());
      chart.setOption({
        grid: { left: 56, right: 16, top: 24, bottom: 32 },
        tooltip: { trigger: "axis" },
        legend: { data: ["Realizat", "Maine (punctat)"] },
        xAxis: { type: "time" },
        yAxis: { type: "value", name: "lei/MWh" },
        series: [
          { name: "Realizat", type: "line", showSymbol: false, data: historical },
          { name: "Maine (punctat)", type: "line", showSymbol: false, lineStyle: { type: "dashed" }, data: forecast },
        ],
      });
    } catch (e) { console.error(e); }
  }

  async function loadYearlyOverlay() {
    const el = $("chart-yearly-overlay");
    if (!el) return;
    try {
      const years = [...selectedYears].sort();
      const overlay = await fetchJson("/market/data/yearly-overlay?years=" + years.join(","));
      const keys = Object.keys(overlay);
      showEmpty(el, keys.length === 0);
      if (!keys.length) return;

      const chart = echarts.init(el, emsChartTheme());
      const series = keys.map((year, idx) => ({
        name: year,
        type: "line",
        showSymbol: false,
        smooth: true,
        color: EMS_YEAR_COLORS[availableYears.indexOf(Number(year)) % EMS_YEAR_COLORS.length],
        data: overlay[year].map((p) => [p.month_day, p.avg_price_lei_mwh]),
      }));
      chart.setOption({
        grid: { left: 56, right: 16, top: 24, bottom: 40 },
        tooltip: { trigger: "axis" },
        legend: {},
        xAxis: { type: "category", name: "Zi (luna-zi)", axisLabel: { interval: 29 } },
        yAxis: { type: "value", name: "lei/MWh" },
        series,
      });
    } catch (e) { console.error(e); }
  }

  async function loadForecast() {
    const el = $("chart-forecast");
    if (!el) return;
    try {
      const forecast = await fetchJson("/market/data/forecast");
      const empty = el.closest(".card").querySelector(".empty-state");
      if (!forecast.points || !forecast.points.length) { empty.hidden = false; return; }
      empty.hidden = true;

      $("forecast-note").textContent = forecast.note +
        (forecast.trend_ratio ? ` (raport tendinta recenta: ${forecast.trend_ratio}x fata de baza sezoniera)` : "");

      const currentYear = forecast.target_year;
      const actual = await fetchJson(`/market/data/yearly-overlay?years=${currentYear}`);
      const actualPoints = (actual[String(currentYear)] || []).map((p) => [`${currentYear}-${p.month_day}`, p.avg_price_lei_mwh]);
      const forecastPoints = forecast.points.map((p) => [p.date, p.predicted_price_lei_mwh]);

      const chart = echarts.init(el, emsChartTheme());
      chart.setOption({
        grid: { left: 56, right: 16, top: 24, bottom: 32 },
        tooltip: { trigger: "axis" },
        legend: { data: ["Realizat " + currentYear, "Predictie"] },
        xAxis: { type: "time" },
        yAxis: { type: "value", name: "lei/MWh" },
        series: [
          { name: "Realizat " + currentYear, type: "line", showSymbol: false, data: actualPoints.map(([d, v]) => [new Date(d).toISOString(), v]) },
          { name: "Predictie", type: "line", showSymbol: false, lineStyle: { type: "dashed" }, data: forecastPoints },
        ],
      });
    } catch (e) { console.error(e); }
  }

  async function loadMonthly() {
    const el = $("chart-monthly");
    if (!el) return;
    try {
      const years = [...selectedYears].sort();
      const monthly = await fetchJson("/market/data/monthly?years=" + years.join(","));
      const keys = Object.keys(monthly);
      showEmpty(el, keys.length === 0);
      if (!keys.length) return;

      const monthNames = ["Ian", "Feb", "Mar", "Apr", "Mai", "Iun", "Iul", "Aug", "Sep", "Oct", "Noi", "Dec"];
      const chart = echarts.init(el, emsChartTheme());
      const series = keys.map((year) => {
        const byMonth = new Map(monthly[year].map((r) => [r.month, r.avg_price_lei_mwh]));
        return {
          name: year,
          type: "bar",
          color: EMS_YEAR_COLORS[availableYears.indexOf(Number(year)) % EMS_YEAR_COLORS.length],
          data: monthNames.map((_, i) => byMonth.get(i + 1) ?? null),
        };
      });
      chart.setOption({
        grid: { left: 56, right: 16, top: 24, bottom: 32 },
        tooltip: { trigger: "axis" },
        legend: {},
        xAxis: { type: "category", data: monthNames },
        yAxis: { type: "value", name: "lei/MWh" },
        series,
      });
    } catch (e) { console.error(e); }
  }

  renderYearToggles();
  renderTimelineRangeToggles();
  loadTimeline();
  loadYearlyOverlay();
  loadForecast();
  loadMonthly();
}
