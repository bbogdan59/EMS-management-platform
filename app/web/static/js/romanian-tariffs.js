(() => {
  const form = document.getElementById('ro-tariff-form');
  if (!form) return;
  const find = selector => form.querySelector(selector);
  const status = find('[data-ro-status]');
  const pricePanel = find('[data-ro-prices]');
  const monthPanel = find('[data-ro-month]');
  const importKwh = find('[data-ro-import-kwh]');
  const exportKwh = find('[data-ro-export-kwh]');
  const fields = [...form.querySelectorAll('[data-ro-rate]')];
  const fmt = (value, digits = 2) => new Intl.NumberFormat('ro-RO', {minimumFractionDigits: digits, maximumFractionDigits: digits}).format(Number(value));
  let timer, controller;
  function element(tag, className, text) {
    const node = document.createElement(tag);
    node.className = className;
    node.textContent = text;
    return node;
  }
  function line(container, label, value) {
    const row = document.createElement('div');
    row.append(element('dt', '', label), element('dd', '', value));
    container.append(row);
  }
  function payload() {
    const tariff = Object.fromEntries(fields.map(input => [input.name, input.value]));
    tariff.name = form.elements.name.value;
    tariff.tg_in_active = form.elements.tg_in_active.checked;
    return {tariff, import_kwh: importKwh.value, export_kwh: exportKwh.value};
  }
  function render(data) {
    pricePanel.hidden = false;
    find('[data-ro-import-price]').textContent = fmt(data.rates.import_gross, 5);
    find('[data-ro-export-price]').textContent = fmt(data.rates.export_gross, 5);
    find('[data-ro-export-formula]').textContent = `Export fara TVA: ${form.elements.active_energy.value} − ${form.elements.tg.value} (TG) = ${fmt(data.rates.export_net, 8)} lei/kWh.`;
    monthPanel.hidden = !data.available;
    if (!data.available) { status.textContent = 'Completeaza ambele cantitati pentru simularea lunara. O valoare lipsa nu inseamna zero.'; return; }
    status.textContent = '';
    find('[data-ro-compensation]').textContent = `${fmt(data.matched_kwh)} kWh compensati 1:1 pentru energia activa. Raman ${fmt(data.remaining_active_kwh)} kWh de energie activa necompensata.`;
    const lines = find('[data-ro-lines]'); lines.replaceChildren();
    line(lines, 'Import + abonament, fara TVA', `${fmt(data.import_net)} lei`);
    line(lines, `TVA import (${form.elements.import_vat.value}%)`, `+ ${fmt(data.import_vat)} lei`);
    line(lines, 'Energie activa compensata', `− ${fmt(data.active_credit)} lei`);
    line(lines, 'TG retinut din compensare', `+ ${fmt(data.tg_retained)} lei`);
    line(lines, `TVA export (${form.elements.export_vat.value}%)`, `− ${fmt(data.credit_vat)} lei`);
    find('[data-ro-total]').textContent = `${fmt(data.payable)} lei`;
    const surplus = find('[data-ro-surplus]');
    surplus.hidden = Number(data.surplus_kwh) === 0;
    surplus.textContent = `${fmt(data.surplus_kwh)} kWh surplus pentru report, cu o valoare fara TVA de ${fmt(data.surplus_credit_net)} lei (energie activa − TG). Acesta nu reduce taxele de import in aceasta simulare.`;
    const details = find('[data-ro-detail]'); details.replaceChildren();
    data.rows.forEach(row => {
      const item = document.createElement('div'); item.className = 'ro-detail-line';
      item.append(element('span', '', `${row.label} · ${fmt(row.rate, 8)} lei/kWh`), element('span', '', row.included ? 'Deja inclus' : `${fmt(row.value)} lei`));
      details.append(item);
    });
  }
  async function update() {
    const current = new AbortController(); controller = current;
    if (fields.some(input => !input.value.trim()) || !form.elements.name.value.trim()) {
      status.textContent = 'Completeaza toate preturile si TVA. Introdu 0 doar pentru componentele care nu se aplica.'; return;
    }
    try {
      const response = await fetch(form.dataset.previewUrl, {
        method: 'POST', signal: current.signal,
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': form.elements.csrf_token.value},
        body: JSON.stringify(payload()),
      });
      if (!response.ok) {
        if (response.status === 422) {
          status.textContent = 'Verifica valorile: numere pozitive sau zero, maximum 8 zecimale; TVA intre 0 si 100; TG cel mult egal cu energia activa.';
          return;
        }
        throw new Error();
      }
      const result = await response.json();
      if (!current.signal.aborted) render(result);
    } catch (error) {
      if (error.name !== 'AbortError') status.textContent = 'Calculul nu a putut fi incarcat. Modifica un camp pentru a reincerca.';
    }
  }
  function schedule() {
    clearTimeout(timer); controller?.abort();
    pricePanel.hidden = true; monthPanel.hidden = true;
    status.textContent = 'Se actualizeaza calculul…';
    timer = setTimeout(update, 250);
  }
  form.addEventListener('input', schedule);
  find('[data-ro-example]').addEventListener('click', () => {
    const example = JSON.parse(document.getElementById('ro-tariff-example').textContent);
    Object.entries(example).forEach(([name, value]) => {
      if (name === 'tg_in_active') form.elements[name].checked = value;
      else form.elements[name].value = value;
    });
    find('[data-ro-example-note]').hidden = false;
    schedule();
  });
  update();
})();
