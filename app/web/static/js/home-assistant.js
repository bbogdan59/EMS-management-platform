(() => {
  const root = document.querySelector('[data-ha-root]');
  if (!root) return;
  const rows = root.querySelector('[data-ha-mappings]');
  const add = root.querySelector('[data-ha-add]');
  let serial = 0;
  const units = {occupancy: ['boolean'], boiler_status: ['state'], hvac_status: ['state'], power: ['W', 'kW'], flexibility: ['W', 'kW']};
  function bind(row) {
    const kind = row.querySelector('[name="kind"]');
    const unit = row.querySelector('[name="unit"]');
    function updateUnits() {
      const previous = unit.value;
      unit.replaceChildren(...units[kind.value].map(value => new Option(value, value)));
      if (units[kind.value].includes(previous)) unit.value = previous;
    }
    updateUnits();
    kind.addEventListener('change', updateUnits);
    row.querySelector('[data-ha-remove]').addEventListener('click', () => {
      if (rows.children.length > 1) row.remove();
      add.disabled = rows.children.length >= 20;
    });
  }
  rows?.querySelectorAll('[data-ha-mapping]').forEach(bind);
  add?.addEventListener('click', () => {
    if (rows.children.length >= 20) return;
    const fragment = document.querySelector('#ha-mapping-template').content.cloneNode(true);
    const key = `added-${++serial}`;
    fragment.querySelectorAll('[id]').forEach(node => { node.id = node.id.replace('__key__', key); });
    fragment.querySelectorAll('[for]').forEach(node => { node.htmlFor = node.htmlFor.replace('__key__', key); });
    const row = fragment.querySelector('[data-ha-mapping]');
    bind(row);
    rows.append(fragment);
    row.querySelector('input').focus();
    add.disabled = rows.children.length >= 20;
  });
  const labels = {pending: 'Configurat · conexiune neverificata', test_pending: 'Test in asteptarea workerului', connected: 'Conexiune MQTT verificata', retrying: 'Reconectare programata', revoked: 'Deconectat · consimtamant retras'};
  const quality = {measured: 'masurat', estimated: 'estimat', declared: 'declarat', simulated: 'simulat', stale: 'expirat', unknown: 'necunoscut'};
  const errorLabels = {auth_failed: 'Autentificare refuzata. Verifica credentialele.', access_denied: 'Acces refuzat la topicuri. Verifica ACL-ul.', tls_failed: 'Certificatul TLS nu a putut fi verificat.', unavailable: 'Broker indisponibil. Reconectare automata.', credentials_unreadable: 'Reintrodu credentialele dupa schimbarea cheii de criptare.', broker_disabled: 'Brokerul nu mai este permis.', message_limit: 'Prea multe mesaje primite.'};
  const formatDate = value => value ? new Date(value).toLocaleString('ro-RO') : 'Niciodata';
  async function refresh() {
    if (document.hidden) return;
    try {
      const response = await fetch(root.dataset.statusUrl, {headers: {Accept: 'application/json'}, cache: 'no-store'});
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      root.querySelector('[data-ha-status]').textContent = data.enabled ? labels[data.status] || data.status : 'Integrare dezactivata';
      root.querySelector('[data-ha-error]').textContent = errorLabels[data.error_code] || '';
      root.querySelector('[data-ha-connected]').textContent = formatDate(data.last_connected_at);
      const body = root.querySelector('[data-ha-observations]');
      if (body) body.replaceChildren(...data.observations.map(o => {
        const row = document.createElement('tr');
        const value = o.available ? `${typeof o.value === 'boolean' ? (o.value ? 'Ocupat' : 'Liber') : o.value} ${o.unit === 'boolean' ? '' : o.unit}` : 'Indisponibil';
        const originalQuality = o.source_quality && o.source_quality !== o.quality ? ` · ${quality[o.source_quality] || o.source_quality}` : '';
        [o.entity_id, value, `${o.source || 'Fara date'} · ${quality[o.quality] || o.quality}${originalQuality}`, formatDate(o.observed_at)].forEach(value => {
          const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
        });
        return row;
      }));
      const notice = root.querySelector('[data-ha-refresh]');
      if (notice) notice.textContent = 'Actualizare automata la fiecare 5 secunde.';
    } catch {
      const notice = root.querySelector('[data-ha-refresh]');
      if (notice) notice.textContent = 'Actualizarea este indisponibila; datele afisate pot fi invechite.';
    }
  }
  if (root.dataset.poll === 'yes') { refresh(); window.setInterval(refresh, 5000); }
})();
