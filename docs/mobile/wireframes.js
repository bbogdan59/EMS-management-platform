/* Documentation-only wireflow. All values and transitions are fictional.
 * No EMS/provider calls, analytics, persistent storage or real mutations.
 */
"use strict";

const screenNames = [
  "welcome", "auth", "deye", "ha", "mapping", "overview", "metric", "insights",
  "forecast", "bill", "notifications", "settings", "privacy", "deletion",
  "preferences", "notice", "profile",
];
const parents = {
  auth: "welcome", deye: "settings", ha: "settings", mapping: "ha", metric: "overview",
  forecast: "insights", bill: "insights", privacy: "settings", deletion: "privacy",
  preferences: "notifications", notice: "notifications", profile: "settings",
};
const tabGroups = {
  overview: ["overview", "metric"], insights: ["insights", "forecast", "bill"],
  notifications: ["notifications", "notice", "preferences"],
  settings: ["settings", "privacy", "deletion", "profile", "ha", "mapping", "deye"],
};
const cachedScreens = new Set(["overview", "metric", "insights", "forecast", "bill", "notifications", "notice"]);
const view = {
  screen: "overview", state: "ready", locale: "ro", stations: 1, station: "1",
  ha: false, haStep: 0, haObserved: false, deyeStep: 0, deleteStep: 0,
  selected: new Set(), draftSelected: new Set(), occupancyConsent: false,
  draftOccupancyConsent: false, authMode: "login", metric: "pv",
  range: "day", forecastDay: "tomorrow", filter: "all", unread: false, read: new Set(),
  notice: "summary", metricParent: "overview", prefs: new Set(), toast: "",
};
const catalogues = {};
const $ = (id) => document.getElementById(id);
const escape = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const t = (key) => {
  const value = catalogues[view.locale][key];
  if (value === undefined) throw new Error(`Missing catalogue key: ${key}`);
  return escape(value);
};
const n = (value, places = 2) => new Intl.NumberFormat(view.locale === "ro" ? "ro-RO" : "en-GB", {
  minimumFractionDigits: places, maximumFractionDigits: places,
}).format(value);
const button = (key, action, style = "", disabled = false) =>
  `<button class="action ${style}" data-action="${action}" ${disabled ? "disabled" : ""}>${t(key)}</button>`;
const link = (key, target, style = "secondary") =>
  `<button class="action ${style}" data-go="${target}">${t(key)}</button>`;
const card = (title, body, style = "") => `<section class="card ${style}"><h3>${t(title)}</h3>${body}</section>`;
const row = (key, value) => `<div class="row"><span>${t(key)}</span><strong>${value}</strong></div>`;
const badge = (key) => `<span class="badge">${t(key)}</span>`;
const check = (id, key, checked = false, meta = "") =>
  `<label class="check"><input type="checkbox" id="${id}" ${checked ? "checked" : ""}><span>${t(key)}${meta ? `<small>${t(meta)}</small>` : ""}</span></label>`;
const stationLabel = () => t(view.station === "2" ? "common.station2" : "common.station");
const source = () => `<p class="caption">${view.state === "stale" ? t("common.source").replace("11:58", "10:40") : t("common.source")}</p>`;
const measured = () => `<div class="chips">${badge("quality.measured")}</div>${source()}`;
const money = (value) => `${n(value)} lei`;

function chart() {
  return `<svg class="chart" viewBox="0 0 320 115" role="img" aria-label="${t("metric.chartLabel")}">
    <path d="M0 20H320 M0 65H320 M0 105H320" fill="none" stroke="currentColor" opacity=".15"/>
    <path d="M0 100 L20 96 L45 78 L70 50 L96 70 L120 30 L138 20 M184 36 L210 60 L238 56 L265 80 L290 90 L320 100" fill="none" stroke="currentColor" stroke-width="3"/>
    <path d="M148 0V110 M174 0V110" fill="none" stroke="currentColor" stroke-dasharray="3 5" opacity=".4"/>
  </svg><p class="caption">${t("metric.chartLabel")}</p>`;
}

function segments(items, active, action) {
  return `<div class="segments">${items.map(([value, key]) =>
    `<button data-${action}="${value}" aria-pressed="${active === value}">${t(key)}</button>`).join("")}</div>`;
}

function metrics() {
  return `<div class="grid">${[
    ["load", "metric.load", `${n(2.93)} kW`], ["grid", "metric.grid", `${n(2.4)} kW`],
    ["soc", "metric.soc", `${n(54, 1)} %`], ["pv", "metric.production", `${n(13.7, 1)} kWh`],
  ].map(([metric, key, value]) => `<button class="metric" data-metric="${metric}"><span>${t(key)}</span><strong>${value}</strong></button>`).join("")}</div>`;
}

function today() {
  return card("common.today", row("metric.production", `${n(13.7, 1)} kWh`) +
    row("metric.consumption", `${n(9.8, 1)} kWh`) + row("metric.import", `${n(0.6, 1)} kWh`) +
    row("metric.export", `${n(4.1, 1)} kWh`) + `<p class="caption">${t("quality.coverage")}</p><p class="caption">${t("metric.compare")}</p>`);
}

function teasers() {
  return card("forecast.teaser", `<p class="value">${n(18.4, 1)} <small>kWh</small></p>${badge("quality.estimated")}${link("screen.forecast", "forecast")}`) +
    card("bill.teaser", `<p class="value">${money(185)}</p><p class="caption">${t("bill.disclaimer")}</p>${link("screen.bill", "bill")}`);
}

function haContext() {
  if (!view.ha) return "";
  if (view.state === "offline") return card("ha.connected", `<p>${t("ha.hiddenOffline")}</p>`);
  let body = `<p class="caption">${t("ha.source").replace("11:57", view.state === "stale" ? "10:40" : "11:57")}</p>`;
  if (!view.haObserved) body += `<p>${t("mapping.wait")}</p>`;
  for (const entity of view.selected) {
    if (entity === "occupancy" && !view.occupancyConsent) continue;
    const value = !view.haObserved ? "—" : entity === "boiler" ? "650 W" : t("mapping.home");
    body += row(`mapping.${entity}`, value);
  }
  body += link("ha.configure", "mapping");
  return card("ha.connected", body);
}

function readyContent() {
  switch (view.screen) {
    case "welcome":
      return `<div class="sun-mark" aria-hidden="true">☀</div><p class="lead">${t("welcome.title")}</p><p>${t("welcome.body")}</p>${button("auth.signup", "signup")}${button("auth.login", "login", "secondary")}`;
    case "auth":
      return `<p class="caption">${t("auth.note")}</p>${segments([["signup", "auth.signup"], ["login", "auth.login"]], view.authMode, "auth-mode")}
        <label class="field" for="email">${t("auth.email")}<input id="email" type="email" autocomplete="off" placeholder="demo@example.invalid"></label>
        <label class="field" for="password">${t("auth.password")}<input id="password" type="password" autocomplete="off" placeholder="••••••••"></label>
        ${view.authMode === "signup" ? check("terms", "auth.terms") + check("analytics", "auth.analytics") : ""}
        ${button("auth.submit", "auth-submit", "", view.authMode === "signup")}${button("auth.forgot", "demo", "secondary")}`;
    case "deye":
      return `<p>${t("deye.body")}</p><p class="caption">${t("deye.note")}</p>${
        view.deyeStep === 0 ? check("deye-consent", "deye.consent") + button("deye.browser", "deye-browser", "", true) :
        view.deyeStep === 1 ? card("common.station", `<p>${stationLabel()}</p>${button("deye.select", "deye-select")}`) :
        `<div class="notice">${t("deye.selected")}</div>${button("deye.first", "deye-observe")}`
      }${link("common.later", "overview")}`;
    case "ha":
      return `<p>${t("ha.body")}</p>${view.haStep === 0 ? check("ha-consent", "ha.consent") + button("ha.pair", "ha-pair", "", true) :
        card("screen.ha", `<p>${t("ha.instructions")}</p><div class="code">${t("ha.code")}</div><p class="caption">${t("ha.expiry")}</p>${button("ha.confirm", "ha-confirm")}`)}${link("common.later", "overview")}`;
    case "mapping":
      return `<p class="caption">${t("mapping.note")}</p>${
        check("entity-boiler", "mapping.boiler", view.draftSelected.has("boiler"), "mapping.boilerMeta") +
        check("entity-occupancy", "mapping.occupancy", view.draftSelected.has("occupancy"), "mapping.occupancyMeta") +
        check("occupancy-consent", "mapping.occupancyConsent", view.draftOccupancyConsent)
      }${button("common.save", "mapping-save", "", !mappingValid())}${link("common.later", "overview")}`;
    case "overview":
      return measured() + card("metric.pv", `<p class="value">${n(5.33)} <small>kW</small></p><div class="flow">${t("metric.flow")}</div><p class="caption">${t("metric.flowText")}</p>`, "hero") +
        metrics() + today() + `<p class="caption">${t("metric.ratios")}</p>` + haContext() + teasers() + button("common.refresh", "demo", "secondary");
    case "metric": {
      const isSoc = view.metric === "soc";
      const unit = isSoc ? "%" : "kW";
      const metricKey = {pv: "production", load: "consumption", grid: "export", soc: "soc"}[view.metric];
      const period = {day: "common.date", week: "metric.weekDate", month: "common.month"}[view.range];
      const values = isSoc ? ["52", "53", "—", "54"] : [n(1.6), n(2.8), "—", n(5.33)];
      const labels = view.range === "day" ? ["09:00", "10:00", "11:00", "12:00"] :
        view.range === "week" ? ["21/09", "23/09", "25/09", "27/09"] : ["01/09", "10/09", "20/09", "28/09"];
      return segments([["day", "common.day"], ["week", "common.week"], ["month", "common.monthRange"]], view.range, "range") +
        `<p class="caption">${t(period)}</p><p class="caption">${t("metric.calendarNote")}</p>` +
        card(`metric.${metricKey}`, `${badge("quality.measured")} ${unit}${chart()}<p class="caption">${t("quality.partial")}</p>
        <details><summary>${t("metric.table")}</summary><table><thead><tr><th>${t("metric.time")}</th><th>${t("metric.value")} (${unit})</th></tr></thead><tbody>${values.map((value, index) => `<tr><td>${labels[index]}</td><td>${value}</td></tr>`).join("")}</tbody></table></details>`);
    }
    case "insights": return link("screen.metric", "metric") + teasers();
    case "forecast":
      return segments([["today", "common.today"], ["tomorrow", "common.tomorrow"]], view.forecastDay, "forecast-day") +
        card("screen.forecast", `<p class="value">${n(view.forecastDay === "today" ? 20 : 18.4, 1)} <small>kWh</small></p>${badge("quality.estimated")}${chart()}
        <p class="caption">${t("forecast.issued").replace("28", view.state === "stale" ? "27" : "28")}</p><p>${t("forecast.confidence")}</p>
        ${view.forecastDay === "today" ? row("forecast.actual", `${n(13.7, 1)} kWh`) : ""}
        <details open><summary>${t("forecast.method")}</summary><p>${t("forecast.explanation")}</p></details>`);
    case "bill":
      return `<p class="caption">${t("common.month")}</p><div class="notice">${t("bill.disclaimer")}</div>` +
        card("bill.projected", `<p class="value">${money(185)}</p>${badge("quality.estimated")}${row("bill.accrued", money(165))}${row("bill.remaining", money(20))}`, "hero") +
        card("bill.previous", `<p class="caption">${t("common.previousMonth")}</p><p class="value">${money(201)}</p><p class="caption">${t("bill.previousNote")}</p>`) +
        card("bill.breakdown", row("bill.energy", money(120)) + row("bill.taxes", money(75)) + row("bill.credit", `−${money(30)}`) + `<p class="caption">${t("quality.coverage")}</p>`) +
        card("bill.prices", row("bill.contract", `${n(0.9564632, 4)} lei/kWh`) + row("bill.spot", `${n(0.35, 4)} lei/kWh`)) +
        `<details open><summary>${t("bill.method")}</summary><p>${t("bill.explanation")}</p></details>`;
    case "notifications": {
      let notices = "";
      if (view.filter !== "alerts" && !(view.unread && view.read.has("summary"))) {
        notices += card("notifications.summaryTitle", `<p class="caption">${t("notifications.summaryDate")}</p><p>${t("notifications.summaryBody")}</p>${button("common.details", "notice-summary", "secondary")}`);
      }
      if (view.filter !== "summaries" && !(view.unread && view.read.has("alert"))) {
        notices += card("notifications.alertTitle", `<p>${t("notifications.alertBody")}</p><p class="caption">${t("notifications.alertCaution")}</p>${button("common.details", "notice-alert", "secondary")}`);
      }
      return segments([["all", "notifications.all"], ["summaries", "notifications.summaries"], ["alerts", "notifications.alerts"]], view.filter, "filter") +
        check("unread", "notifications.unread", view.unread) + (notices || `<p>${t("empty.notifications")}</p>`) + link("screen.preferences", "preferences");
    }
    case "notice":
      return (view.notice === "summary" ? card("notifications.summaryTitle", `<p class="caption">${t("notifications.summaryDate")}</p>${row("metric.production", `${n(21.1, 1)} kWh`)}${row("metric.consumption", `${n(14.2, 1)} kWh`)}${row("metric.import", `${n(0.6, 1)} kWh`)}${row("metric.export", `${n(8.3, 1)} kWh`)}${badge("quality.measured")}<p class="caption">${t("notifications.daySource")}</p>`) :
        card("notifications.alertTitle", `<p>${t("notifications.alertBody")}</p><p>${t("notifications.alertCaution")}</p>${measured()}`)) +
        button(view.read.has(view.notice) ? "notifications.readDone" : "notifications.read", "read", "secondary", view.read.has(view.notice)) + link("notifications.detail", "metric");
    case "preferences":
      return `<p>${t("preferences.body")}</p>${button("preferences.push", "demo", "secondary")}` +
        ["sync", "incidents", "forecast", "cost", "summary"].map((key) => check(`pref-${key}`, `preferences.${key}`, view.prefs.has(key))).join("") +
        `<p class="caption">${t("preferences.quiet")}</p>${check("pref-occupancy", "preferences.occupancy", view.prefs.has("occupancy"))}${button("common.save", "prefs-save")}`;
    case "settings":
      return card("settings.integrations", link("screen.deye", "deye") + link("screen.ha", view.ha ? "mapping" : "ha") +
        (view.ha ? button("ha.disconnect", "ha-disconnect", "secondary") : `<p class="caption">${t("common.optional")}</p>`)) +
        link("screen.profile", "profile") + link("screen.preferences", "preferences") + link("screen.privacy", "privacy") +
        card("settings.appearance", `<p class="caption">${t("settings.appearanceNote")}</p>`);
    case "privacy":
      return `<p>${t("privacy.body")}</p>${card("privacy.inventory", `<p>${t("privacy.inventoryText")}</p>`)}${check("analytics", "auth.analytics")}${button("privacy.export", "demo", "secondary")}${view.ha ? button("privacy.withdraw", "ha-disconnect", "secondary") : ""}${link("privacy.delete", "deletion", "danger")}`;
    case "deletion":
      return `<p>${t("delete.body")}</p><div class="notice">${t("delete.admin")}</div>${view.deleteStep === 0 ? button("delete.reauth", "reauth", "secondary") :
        view.deleteStep === 1 ? button("delete.confirm", "delete", "danger") : `<div class="notice" role="status">${t("delete.accepted")}</div>`}${link("common.cancel", "privacy")}`;
    case "profile":
      return card("screen.profile", `<p>${t("profile.demo")}</p>${row("profile.session", "●")}${view.state !== "empty" ? row("profile.other", "○") + button("profile.revoke", "demo", "secondary") : ""}`) +
        link("screen.privacy", "privacy") + button("profile.logout", "logout", "secondary");
    default: throw new Error(`Unknown screen: ${view.screen}`);
  }
}

function mappingValid() {
  return view.draftSelected.size > 0 && (!view.draftSelected.has("occupancy") || view.draftOccupancyConsent);
}

function stateContent() {
  if (view.state === "ready") return readyContent();
  if (view.screen === "welcome" && ["empty", "stale", "offline"].includes(view.state)) {
    return `<p class="notice">${t("state.static")}</p>${readyContent()}`;
  }
  if (view.state === "loading") return `<div role="status"><p>${t("state.loading")}</p><div class="skeleton big"></div><div class="skeleton"></div><div class="skeleton"></div></div>`;
  if (view.state === "error") return `<div class="notice" role="alert">${t("state.error")}</div>${button("common.retry", "ready")}`;
  if (view.state === "empty") {
    const message = `<div class="notice">${t(`empty.${view.screen}`)}</div>`;
    if (["auth", "deletion", "preferences", "privacy", "profile"].includes(view.screen)) return message + readyContent();
    if (view.screen === "settings") return message + link("screen.profile", "profile") + link("screen.privacy", "privacy");
    return message + (view.screen === "overview" ? link("screen.deye", "deye") : button("common.continue", "ready", "secondary"));
  }
  if (view.state === "offline") {
    if (!cachedScreens.has(view.screen)) return `<div class="notice">${t("state.offlineBlocked")}</div>${view.screen === "profile" ? button("profile.logout", "logout", "secondary") : ""}${button("common.retry", "ready", "secondary")}`;
    // Read actions cannot report persistence while offline.
    return `<div class="notice">${t("state.offline")}</div>${readyContent()}`;
  }
  if (view.state === "stale") {
    let key = "state.stale";
    if (["metric", "notifications", "notice"].includes(view.screen)) key = "state.history";
    if (view.screen === "forecast") key = "state.forecast";
    if (view.screen === "bill") key = "state.bill";
    if (!cachedScreens.has(view.screen)) {
      key = view.screen === "ha" ? "state.pairing" : "state.revalidate";
      return `<div class="notice">${t(key)}</div>${button("common.continue", "ready", "secondary")}`;
    }
    const content = view.screen === "bill" ? readyContent().replaceAll("28 sept.", "27 sept.").replaceAll("28 Sep", "27 Sep") : readyContent();
    return `<div class="notice">${t(key)}</div>${content}`;
  }
  return "";
}

function render(moveFocus = false) {
  const number = String(screenNames.indexOf(view.screen) + 1).padStart(2, "0");
  $("screen").value = view.screen;
  $("state").value = view.state;
  $("phone").lang = view.locale;
  $("demo-banner").textContent = catalogues[view.locale].demo;
  const stationScreen = cachedScreens.has(view.screen) || view.screen === "settings";
  const station = !stationScreen ? "" : view.stations > 1 ?
    `<select class="station-select" id="station-select" aria-label="${t("common.stationPicker")}"><option value="1" ${view.station === "1" ? "selected" : ""}>${t("common.station")}</option><option value="2" ${view.station === "2" ? "selected" : ""}>${t("common.station2")}</option></select>` : `<p class="station-name">${stationLabel()}</p>`;
  $("phone-header").innerHTML = `${parents[view.screen] ? `<button class="back" data-go="${view.screen === "metric" ? view.metricParent : parents[view.screen]}" aria-label="${t("common.back")}">‹</button>` : ""}<div>${station}<h2>${t(`screen.${view.screen}`)}</h2></div>`;
  $("content").innerHTML = (view.toast ? `<div class="notice" role="status">${t(view.toast)}</div>` : "") + stateContent();
  if (view.state === "offline") $("content").querySelectorAll("button[data-action='read']").forEach((item) => { item.disabled = true; });
  const tab = Object.keys(tabGroups).find((key) => tabGroups[key].includes(view.screen));
  $("tabs").innerHTML = ["welcome", "auth"].includes(view.screen) ? "" :
    [["overview", "⌂"], ["insights", "◷"], ["notifications", "◌"], ["settings", "⚙"]].map(([name, icon]) =>
      `<button data-go="${name}" ${name === tab ? 'aria-current="page"' : ""}><span aria-hidden="true">${icon}</span>${t(`nav.${name}`)}</button>`).join("");
  $("frame-note").textContent = `S${number} · ${catalogues[view.locale][`screen.${view.screen}`]} · ${view.state} · #197`;
  if (moveFocus) { $("content").scrollTop = 0; $("content").focus({preventScroll: true}); }
}

function go(screen) {
  if (!screenNames.includes(screen)) return;
  if (screen === "metric") view.metricParent = view.screen;
  if (screen === "mapping") {
    view.draftSelected = new Set(view.selected);
    view.draftOccupancyConsent = view.occupancyConsent;
  }
  view.screen = screen;
  view.state = "ready";
  view.toast = "";
  render(true);
}

function disconnectHA() {
  view.ha = false; view.haObserved = false; view.haStep = 0;
  view.selected.clear(); view.draftSelected.clear();
  view.occupancyConsent = false; view.draftOccupancyConsent = false; $("ha").checked = false;
  view.toast = "common.demoAction";
}

document.addEventListener("click", (event) => {
  const target = event.target.closest("button");
  if (!target || target.disabled) return;
  const data = target.dataset;
  if (data.go) { go(data.go); return; }
  if (data.metric) { view.metric = data.metric; go("metric"); return; }
  if (data.range) view.range = data.range;
  if (data.filter) view.filter = data.filter;
  if (data.forecastDay) view.forecastDay = data.forecastDay;
  if (data.authMode) view.authMode = data.authMode;
  if (data.action) {
    switch (data.action) {
      case "ready": view.state = "ready"; break;
      case "signup": view.authMode = "signup"; go("auth"); return;
      case "login": view.authMode = "login"; go("auth"); return;
      case "auth-submit": go("deye"); return;
      case "deye-browser": view.deyeStep = 1; break;
      case "deye-select": view.deyeStep = 2; break;
      case "deye-observe": go("overview"); return;
      case "ha-pair": view.haStep = 1; break;
      case "ha-confirm": go("mapping"); return;
      case "mapping-save":
        view.selected = new Set(view.draftSelected); view.occupancyConsent = view.draftOccupancyConsent;
        view.ha = true; view.haObserved = false; $("ha").checked = true;
        go("overview"); view.toast = "mapping.saved"; render();
        $("content").insertAdjacentHTML("afterbegin", button("mapping.observe", "ha-observe")); return;
      case "ha-observe": view.haObserved = true; view.toast = ""; break;
      case "ha-disconnect": disconnectHA(); break;
      case "notice-summary": view.notice = "summary"; go("notice"); return;
      case "notice-alert": view.notice = "alert"; go("notice"); return;
      case "read": if (view.state !== "offline") view.read.add(view.notice); break;
      case "prefs-save": view.toast = "common.saved"; break;
      case "reauth": view.deleteStep = 1; break;
      case "delete": view.deleteStep = 2; break;
      case "logout":
        disconnectHA(); view.read.clear(); view.prefs.clear(); view.deyeStep = 0; view.deleteStep = 0;
        go("welcome"); return;
      default: view.toast = "common.demoAction";
    }
  }
  render();
});

document.addEventListener("change", (event) => {
  const {id, value, checked} = event.target;
  if (id === "screen") { go(value); return; }
  if (id === "state") { view.state = value; view.toast = ""; }
  if (id === "theme") $("phone").dataset.theme = value;
  if (id === "size") $("phone").dataset.size = value;
  if (id === "locale") { view.locale = value; populateScreens(); }
  if (id === "stations") { view.stations = Number(value); view.station = "1"; }
  if (id === "station-select") {
    view.station = value; disconnectHA(); view.toast = "";
    view.state = value === "2" ? "empty" : "ready";
  }
  if (id === "ha") {
    if (!checked) disconnectHA();
    else { view.ha = true; view.selected = new Set(["boiler"]); view.occupancyConsent = false; view.haObserved = true; }
  }
  if (id === "terms" || id === "deye-consent" || id === "ha-consent") {
    const action = {terms: "auth-submit", "deye-consent": "deye-browser", "ha-consent": "ha-pair"}[id];
    $("content").querySelector(`[data-action="${action}"]`).disabled = !checked;
    return;
  }
  if (id.startsWith("entity-")) {
    const entity = id.slice(7);
    if (checked) view.draftSelected.add(entity); else view.draftSelected.delete(entity);
  }
  if (id === "occupancy-consent") view.draftOccupancyConsent = checked;
  if (id.startsWith("entity-") || id === "occupancy-consent") {
    $("content").querySelector('[data-action="mapping-save"]').disabled = !mappingValid();
    return;
  }
  if (id === "unread") view.unread = checked;
  if (id.startsWith("pref-")) {
    if (checked) view.prefs.add(id.slice(5)); else view.prefs.delete(id.slice(5));
    return;
  }
  if (id === "analytics") return; // Visual opt-in only; no analytics exists in this prototype.
  render();
});

function populateScreens() {
  $("screen").innerHTML = screenNames.map((name, index) => `<option value="${name}">S${String(index + 1).padStart(2, "0")} · ${t(`screen.${name}`)}</option>`).join("");
}

async function start() {
  for (const locale of ["ro", "en"]) {
    const response = await fetch(`strings.${locale}.json`);
    if (!response.ok) throw new Error(`Catalogue ${locale}: ${response.status}`);
    catalogues[locale] = await response.json();
  }
  populateScreens(); render();
}
start().catch((error) => {
  $("content").textContent = "Prototip indisponibil. Servește docs/mobile prin HTTP local, conform README.md.";
  console.error(error);
});
