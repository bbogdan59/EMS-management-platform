function emsToggleTheme() {
  const root = document.documentElement;
  const isDark = root.classList.toggle("dark");
  emsChartInstances.forEach(({ chart }) => { if (!chart.isDisposed()) emsApplyChartAppearance(chart); });
  document.dispatchEvent(new CustomEvent("ems:theme-change"));
  try {
    localStorage.setItem("ems-theme", isDark ? "dark" : "light");
  } catch (e) {
    /* localStorage indisponibil (mod privat) -- tema ramane doar pentru sesiunea curenta */
  }
}

/**
 * Copiaza textul unui element (de regula un link de invitatie afisat o
 * singura data, issue #149) folosind Clipboard API, cu fallback prin
 * execCommand pentru browsere/contexte fara acces la API modern (ex. pagina
 * servita non-HTTPS in dezvoltare locala). Valoarea NU e trimisa niciodata
 * catre server -- doar copiata local, in clipboard-ul utilizatorului.
 * `buttonEl` primeste `data-copy-target="<id element sursa>"` si o
 * confirmare vizuala temporara la succes.
 */
function emsCopyToClipboard(buttonEl) {
  const sourceId = buttonEl.getAttribute("data-copy-target");
  const sourceEl = sourceId ? document.getElementById(sourceId) : null;
  const text = sourceEl ? sourceEl.textContent : "";
  if (!text) return;

  const showConfirmation = () => {
    const original = buttonEl.textContent;
    buttonEl.textContent = "Copiat!";
    buttonEl.disabled = true;
    setTimeout(() => {
      buttonEl.textContent = original;
      buttonEl.disabled = false;
    }, 2000);
  };

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(showConfirmation, () => _emsCopyFallback(text, showConfirmation));
  } else {
    _emsCopyFallback(text, showConfirmation);
  }
}

function _emsCopyFallback(text, onSuccess) {
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "");
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.appendChild(helper);
  helper.select();
  helper.setSelectionRange(0, text.length);
  try {
    if (document.execCommand("copy")) onSuccess();
  } catch (e) {
    /* copierea a esuat -- utilizatorul poate selecta manual textul afisat */
  }
  document.body.removeChild(helper);
}

/**
 * Conexiune SSE cu reconectare automata si backoff exponential plafonat.
 * Foloseste EventSource nativ; la eroare/inchidere reincearca la 1s, 2s, 4s...
 * pana la maxDelayMs, apoi ramane pe acel interval.
 */
function emsConnectSSE(url, handlers) {
  let delay = 1000;
  const maxDelay = 30000;
  let source = null;
  let closedByUser = false;

  function connect() {
    source = new EventSource(url);
    source.onopen = () => {
      delay = 1000;
      if (handlers.onopen) handlers.onopen();
    };
    for (const [eventName, fn] of Object.entries(handlers.events || {})) {
      source.addEventListener(eventName, (ev) => fn(ev));
    }
    source.onerror = () => {
      if (handlers.onerror) handlers.onerror();
      source.close();
      if (!closedByUser) {
        setTimeout(connect, delay);
        delay = Math.min(delay * 2, maxDelay);
      }
    };
  }

  connect();

  return {
    close() {
      closedByUser = true;
      if (source) source.close();
    },
  };
}

function emsFormatNumber(value, digits = 2) {
  if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return '—';
  return new Intl.NumberFormat('ro-RO', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(value));
}

function emsSetMetric(element, value, unit = '', digits = 2, missing = 'fara date') {
  if (!element) return;
  const unknown = value === null || value === undefined || value === '' || !Number.isFinite(Number(value));
  const display = unknown ? missing : emsFormatNumber(value, digits);
  element.classList.toggle('is-missing', unknown);
  element.classList.toggle('is-long', !unknown && display.length > 7);
  element.replaceChildren(document.createTextNode(display));
  if (!unknown && unit) {
    const suffix = document.createElement('span');
    suffix.className = 'metric-unit';
    suffix.textContent = ' ' + unit;
    element.appendChild(suffix);
  }
}

const emsChartInstances = new Map();
function emsApplyChartAppearance(chart) {
  const dark = document.documentElement.classList.contains('dark');
  const text = dark ? '#acbca4' : '#82907b';
  const line = dark ? '#344b39' : '#e8ede3';
  const options = chart.getOption();
  const axes = (items) => (items || []).map(() => ({ axisLabel: { color: text, fontSize: 10, fontFamily: 'Manrope' }, nameTextStyle: { color: text, fontSize: 10 }, axisLine: { lineStyle: { color: line } }, splitLine: { lineStyle: { color: line, type: 'dashed' } }, axisTick: { show: false } }));
  chart.setOption({ backgroundColor: 'transparent', textStyle: { fontFamily: 'Manrope', color: text }, legend: { type: 'scroll', textStyle: { color: text, fontSize: 10 }, icon: 'roundRect', itemWidth: 10, itemHeight: 5, itemGap: 17 }, xAxis: axes(options.xAxis), yAxis: axes(options.yAxis), tooltip: { backgroundColor: dark ? '#253b2c' : '#fff', borderColor: line, borderWidth: 1, padding: 12, textStyle: { color: dark ? '#eef3e8' : '#23422b', fontSize: 11, fontFamily: 'Manrope' }, extraCssText: 'border-radius:12px;box-shadow:0 8px 30px #11251615;', confine: true } });
}

function emsCreateChart(element) {
  let chart = echarts.getInstanceByDom(element);
  if (chart) {
    emsChartInstances.get(element)?.observer.disconnect();
    chart.dispose();
  }
  chart = echarts.init(element, document.documentElement.classList.contains('dark') ? 'ems-dark' : 'ems-light');
  const observer = new ResizeObserver(() => { if (element.clientWidth && element.clientHeight && !chart.isDisposed()) chart.resize(); });
  observer.observe(element);
  emsChartInstances.set(element, { chart, observer });
  chart.on('finished', () => { element.setAttribute('data-chart-ready', 'true'); });
  return chart;
}

if (typeof echarts !== 'undefined') {
  const shared = { color: ['#527c45', '#69868a', '#a1b785', '#b69359', '#28553b', '#8299ad'], backgroundColor: 'transparent', aria: { enabled: true }, textStyle: { fontFamily: 'Manrope' }, line: { symbolSize: 5, lineStyle: { width: 2.5 } }, bar: { barMaxWidth: 22, itemStyle: { borderRadius: [3, 3, 0, 0] } }, legend: { type: 'scroll', textStyle: { fontSize: 10 }, icon: 'roundRect', itemWidth: 10, itemHeight: 5, itemGap: 16 }, animationDuration: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 300 };
  for (const [name, text, line] of [['ems-light', '#71816b', '#e8ede3'], ['ems-dark', '#afc0a7', '#344b39']]) {
    const axis = { axisLabel: { color: text, fontSize: 10 }, nameTextStyle: { color: text, fontSize: 10 }, axisLine: { lineStyle: { color: line } }, axisTick: { show: false }, splitLine: { lineStyle: { color: line, type: 'dashed' } } };
    echarts.registerTheme(name, { ...shared, textStyle: { ...shared.textStyle, color: text }, categoryAxis: axis, valueAxis: axis, timeAxis: axis, tooltip: { confine: true, backgroundColor: name === 'ems-dark' ? '#253b2c' : '#fff', borderColor: line, textStyle: { color: text, fontSize: 11 }, extraCssText: 'border-radius:12px;box-shadow:0 8px 30px #11251615;' } });
  }
}

function emsInitNavigation() {
  const sidebar = document.getElementById('app-sidebar');
  if (!sidebar) return;
  const workspace = document.getElementById('app-workspace');
  const backdrop = document.querySelector('.sidebar-backdrop');
  const dock = document.querySelector('.mobile-dock');
  const toggles = document.querySelectorAll('[data-menu-toggle]');
  const mobile = window.matchMedia('(max-width: 1023px)');
  let returnFocus = null;
  const focusable = () => [...sidebar.querySelectorAll('a[href],button,select,input:not([type=hidden])')].filter(el => !el.disabled && el.getClientRects().length);
  function setMenu(open) {
    const showing = open && mobile.matches;
    document.body.classList.toggle('menu-open', showing);
    sidebar.inert = mobile.matches && !showing;
    workspace.inert = showing;
    if (dock) dock.inert = showing;
    backdrop.hidden = !showing;
    toggles.forEach(el => el.setAttribute('aria-expanded', String(showing)));
    if (showing) { returnFocus = document.activeElement; sidebar.setAttribute('role', 'dialog'); sidebar.setAttribute('aria-modal', 'true'); focusable()[0]?.focus(); }
    else { sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal'); if (returnFocus && document.contains(returnFocus)) returnFocus.focus(); returnFocus = null; }
  }
  toggles.forEach(el => el.addEventListener('click', () => setMenu(!document.body.classList.contains('menu-open'))));
  document.querySelectorAll('[data-menu-close]').forEach(el => el.addEventListener('click', () => setMenu(false)));
  document.addEventListener('keydown', ev => {
    if (!document.body.classList.contains('menu-open')) return;
    if (ev.key === 'Escape') { ev.preventDefault(); setMenu(false); }
    if (ev.key === 'Tab') {
      const items = focusable(); const first = items[0]; const last = items[items.length - 1];
      if (ev.shiftKey && document.activeElement === first) { ev.preventDefault(); last?.focus(); }
      else if (!ev.shiftKey && document.activeElement === last) { ev.preventDefault(); first?.focus(); }
    }
  });
  mobile.addEventListener('change', () => setMenu(false));
  setMenu(false);
}

function emsAdaptTables(root = document) {
  root.querySelectorAll('.ems-main table').forEach(table => {
    if (table.closest('.table-scroll,.overflow-x-auto')) return;
    const wrapper = document.createElement('div');
    wrapper.className = 'table-scroll'; wrapper.tabIndex = 0; wrapper.setAttribute('role', 'region');
    wrapper.setAttribute('aria-label', table.getAttribute('aria-label') || 'Tabel — deruleaza orizontal pentru toate coloanele');
    table.before(wrapper); wrapper.append(table);
  });
}

document.addEventListener('DOMContentLoaded', () => { emsInitNavigation(); emsAdaptTables(); });
document.addEventListener('htmx:afterSwap', () => emsAdaptTables());
window.addEventListener('beforeunload', () => { emsChartInstances.forEach(({ observer }) => observer.disconnect()); });
