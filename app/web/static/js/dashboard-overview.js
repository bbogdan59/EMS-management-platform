/* global emsFormatNumber */
/* Overview tiles and detail sheets. Tiles only mirror values already rendered by the
   widget scripts; nothing here computes or fetches energy/financial data. */
(() => {
  'use strict';
  const root = document.querySelector('.energy-dashboard');
  if (!root) return;
  const sheets = [...root.querySelectorAll('dialog.ov-sheet')];
  const known = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));

  function openSheet(id, push = true) {
    const sheet = document.getElementById(id);
    if (!sheet || !sheets.includes(sheet) || sheets.some(item => item.open)) return;
    sheet.showModal();
    sheet.querySelector('.ov-sheet-body').scrollTop = 0;
    // A history entry lets the phone back gesture close the sheet instead of leaving the page.
    if (push) history.pushState({emsSheet: id}, '', '#' + id);
  }
  for (const sheet of sheets) {
    sheet.addEventListener('close', () => {
      if (history.state?.emsSheet === sheet.id) history.back();
      else if (location.hash === '#' + sheet.id) history.replaceState(null, '', location.pathname + location.search);
    });
    sheet.addEventListener('click', event => {
      if (event.target === sheet || event.target.closest('[data-sheet-close]')) sheet.close();
    });
  }
  window.addEventListener('popstate', () => {
    const id = history.state?.emsSheet;
    sheets.filter(sheet => sheet.open && sheet.id !== id).forEach(sheet => sheet.close());
    if (id) openSheet(id, false);
  });
  root.addEventListener('click', event => {
    const tile = event.target.closest('[data-sheet-open]');
    if (!tile || event.target.closest('a, select, input, summary, .history-retry')) return;
    openSheet(tile.dataset.sheetOpen);
  });

  const mirrors = [...root.querySelectorAll('[data-mirror]')];
  const haTile = root.querySelector('[data-ha-tile]');
  function sync() {
    for (const target of mirrors) {
      const source = root.querySelector(target.dataset.mirror);
      if (!source) continue;
      if (target.textContent !== source.textContent) target.replaceChildren(...[...source.childNodes].map(node => node.cloneNode(true)));
      if ('mirrorClass' in target.dataset) {
        if (target.className !== source.className) target.className = source.className;
      } else target.classList.toggle('is-missing', source.classList.contains('is-missing'));
    }
    if (!haTile) return;
    const sensors = [...root.querySelectorAll('[data-ha-entity]')];
    const signature = sensors.map(sensor => sensor.querySelector('h3').textContent + sensor.querySelector('[data-ha-value]').textContent).join('|');
    if (haTile.dataset.signature !== signature) {
      haTile.dataset.signature = signature;
      const rows = sensors.slice(0, 3).map(sensor => {
        const row = document.createElement('div'), name = document.createElement('span'), value = document.createElement('b');
        const source = sensor.querySelector('[data-ha-value]');
        name.textContent = sensor.querySelector('h3').textContent;
        value.textContent = source.textContent.trim();
        value.classList.toggle('is-missing', source.classList.contains('is-missing'));
        row.append(name, value);
        return row;
      });
      const note = document.createElement('p');
      note.className = 'ov-kicker';
      note.textContent = !sensors.length ? 'Niciun senzor partajat inca.' : sensors.length > 3 ? `+ ${sensors.length - 3} senzori in detalii` : '';
      haTile.replaceChildren(...rows, ...(note.textContent ? [note] : []));
    }
    const status = root.querySelector('[data-ha-status]'), badge = root.querySelector('[data-ha-tile-status]');
    if (status && badge.textContent !== status.textContent) badge.textContent = status.textContent;
  }
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; sync(); });
  }).observe(root, {subtree: true, childList: true, characterData: true});
  sync();

  document.addEventListener('ems:energy-kpis', event => {
    const metrics = event.detail?.today?.metrics || {};
    const rows = [...root.querySelectorAll('[data-bar]')];
    const read = row => known(metrics[row.dataset.bar]?.value) ? Math.max(0, Number(metrics[row.dataset.bar].value)) : null;
    const scale = Math.max(0, ...rows.map(row => read(row) ?? 0));
    for (const row of rows) {
      const value = read(row);
      row.querySelector('.ov-bar-track').classList.toggle('is-unknown', value === null);
      row.querySelector('.ov-bar-fill').style.width = value ? `${Math.max(2, value / scale * 100)}%` : '0';
    }
  });

  document.addEventListener('ems:savings', event => {
    const savings = event.detail || {}, box = root.querySelector('[data-money-split]');
    const parts = {self: savings.self_consumption_savings_lei, export: savings.export_revenue_lei};
    const usable = savings.available && Object.values(parts).every(value => known(value) && Number(value) >= 0)
      && Number(parts.self) + Number(parts.export) > 0;
    box.hidden = !usable;
    if (!usable) return;
    for (const [key, value] of Object.entries(parts)) {
      box.querySelector(`[data-split="${key}"]`).style.flexGrow = Number(value);
      box.querySelector(`[data-split-value="${key}"]`).textContent = `${emsFormatNumber(value)} lei`;
    }
  });

  if (location.hash.startsWith('#sheet-')) openSheet(location.hash.slice(1), false);
})();
