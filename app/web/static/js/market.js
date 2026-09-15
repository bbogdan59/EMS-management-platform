/* global echarts, emsChartTheme */

const EMS_YEAR_COLORS = ["#2f9354", "#4a86e8", "#f5a524", "#a479e2", "#f691b2", "#43d692"];
const TIMELINE_RANGE_OPTIONS = [30, 90, 180, 365];
const TIMELINE_DEFAULT_DAYS = 30;
const YEARLY_OVERLAY_DEFAULT_YEARS = [2024, 2025, 2026];

function emsInitMarket(availableYears) {
  const $ = (id) => document.getElementById(id);
  let selectedYears = new Set(defaultYearlyOverlayYears(availableYears));
  let selectedTimelineDays = TIMELINE_DEFAULT_DAYS;
  let timelineController = null;
  let yearlyOverlayController = null;
  let forecastController = null;
  let monthlyController = null;
  const marketDataCache = new Map();
  const MARKET_DATA_CACHE_TTL_MS = 30000;

  function defaultYearlyOverlayYears(years) {
    const preferred = YEARLY_OVERLAY_DEFAULT_YEARS.filter((year) => years.includes(year));
    return preferred.length ? preferred : years.slice(-3);
  }

  function isLeapYear(year) {
    return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  }

  function buildCalendarDayLabels(years) {
    const includeLeapDay = years.some((year) => isLeapYear(year));
    const labels = [];
    for (let month = 1; month <= 12; month += 1) {
      const daysInMonth = new Date(2025, month, 0).getDate();
      for (let day = 1; day <= daysInMonth; day += 1) {
        labels.push(`${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`);
      }
      if (month === 2 && includeLeapDay) labels.push("02-29");
    }
    return labels;
  }

  async function fetchJson(url, { signal, timeoutMs = 15000 } = {}) {
    const cached = marketDataCache.get(url);
    if (cached && Date.now() - cached.ts < MARKET_DATA_CACHE_TTL_MS) return cached.data;
    const timeoutController = new AbortController();
    const timer = setTimeout(() => timeoutController.abort(), timeoutMs);
    const onExternalAbort = () => timeoutController.abort();
    if (signal) signal.addEventListener("abort", onExternalAbort);
    try {
      const res = await fetch(url, { headers: { Accept: "application/json" }, signal: timeoutController.signal });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      marketDataCache.set(url, { data, ts: Date.now() });
      return data;
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener("abort", onExternalAbort);
    }
  }

  function showChartState(el, state) {
    const card = el.closest(".card");
    const empty = card.querySelector(".empty-state");
    const error = card.querySelector(".error-state");
    if (empty) empty.hidden = state !== "empty";
    if (error) error.hidden = state !== "error";
    el.hidden = state === "empty" || state === "error";
  }

  function wireRetry(el, loadFn) {
    const retry = el.closest(".card").querySelector(".retry-btn");
    if (!retry || retry.dataset.wired) return;
    retry.dataset.wired = "1";
    retry.addEventListener("click", loadFn);
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
    wireRetry(el, loadTimeline);
    if (timelineController) timelineController.abort();
    const controller = new AbortController();
    timelineController = controller;
    try {
      // Fiecare fereastra e o cerere separata, declansata la cerere (buton) --
      // pagina nu incarca niciodata tot istoricul dintr-o singura cerere
      // initiala, indiferent cat de mare e intervalul selectat (issue #33).
      const payload = await fetchJson("/market/data/timeline?days=" + selectedTimelineDays, { signal: controller.signal });
      if (controller !== timelineController) return;
      const data = payload.points || [];
      showChartState(el, data.length === 0 ? "empty" : "ok");
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
    } catch (e) {
      if (controller !== timelineController) return;
      console.error(e);
      showChartState(el, "error");
    }
  }

  async function loadYearlyOverlay() {
    const el = $("chart-yearly-overlay");
    if (!el) return;
    wireRetry(el, loadYearlyOverlay);
    if (yearlyOverlayController) yearlyOverlayController.abort();
    const controller = new AbortController();
    yearlyOverlayController = controller;
    try {
      const years = [...selectedYears].sort();
      if (!years.length) {
        showChartState(el, "empty");
        echarts.init(el, emsChartTheme()).clear();
        return;
      }
      const overlay = await fetchJson("/market/data/yearly-overlay?years=" + years.join(","), { signal: controller.signal });
      if (controller !== yearlyOverlayController) return;
      const keys = Object.keys(overlay);
      showChartState(el, keys.length === 0 ? "empty" : "ok");
      if (!keys.length) {
        echarts.init(el, emsChartTheme()).clear();
        return;
      }

      const chart = echarts.init(el, emsChartTheme());
      const dayLabels = buildCalendarDayLabels(years);
      const series = keys.map((year) => {
        const pointsByDay = new Map(overlay[year].map((p) => [p.month_day, p.avg_price_lei_mwh]));
        return {
          name: year,
          type: "line",
          showSymbol: false,
          smooth: true,
          color: EMS_YEAR_COLORS[Math.max(availableYears.indexOf(Number(year)), 0) % EMS_YEAR_COLORS.length],
          data: dayLabels.map((monthDay) => pointsByDay.get(monthDay) ?? null),
        };
      });
      chart.setOption({
        grid: { left: 56, right: 16, top: 24, bottom: 40 },
        tooltip: { trigger: "axis" },
        legend: {},
        xAxis: { type: "category", name: "Zi (luna-zi)", data: dayLabels, axisLabel: { interval: 29 } },
        yAxis: { type: "value", name: "lei/MWh" },
        series,
      });
    } catch (e) {
      if (controller !== yearlyOverlayController) return;
      console.error(e);
      showChartState(el, "error");
    }
  }

  async function loadForecast() {
    const el = $("chart-forecast");
    if (!el) return;
    wireRetry(el, loadForecast);
    if (forecastController) forecastController.abort();
    const controller = new AbortController();
    forecastController = controller;
    try {
      const forecast = await fetchJson("/market/data/forecast", { signal: controller.signal });
      if (controller !== forecastController) return;
      if (!forecast.points || !forecast.points.length) { showChartState(el, "empty"); return; }
      showChartState(el, "ok");

      $("forecast-note").textContent = forecast.note +
        (forecast.trend_ratio ? ` (raport tendinta recenta: ${forecast.trend_ratio}x fata de baza sezoniera)` : "");

      const currentYear = forecast.target_year;
      const actual = await fetchJson(`/market/data/yearly-overlay?years=${currentYear}`, { signal: controller.signal });
      if (controller !== forecastController) return;
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
    } catch (e) {
      if (controller !== forecastController) return;
      console.error(e);
      showChartState(el, "error");
    }
  }

  async function loadMonthly() {
    const el = $("chart-monthly");
    if (!el) return;
    wireRetry(el, loadMonthly);
    if (monthlyController) monthlyController.abort();
    const controller = new AbortController();
    monthlyController = controller;
    try {
      const years = [...selectedYears].sort();
      const monthly = await fetchJson("/market/data/monthly?years=" + years.join(","), { signal: controller.signal });
      if (controller !== monthlyController) return;
      const keys = Object.keys(monthly);
      showChartState(el, keys.length === 0 ? "empty" : "ok");
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
    } catch (e) {
      if (controller !== monthlyController) return;
      console.error(e);
      showChartState(el, "error");
    }
  }

  renderYearToggles();
  renderTimelineRangeToggles();
  loadTimeline();
  loadYearlyOverlay();
  loadForecast();
  loadMonthly();
}
