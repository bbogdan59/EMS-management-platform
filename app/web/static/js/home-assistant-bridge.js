(() => {
  const root = document.querySelector('[data-ha-bridge]');
  if (!root) return;
  const message = root.querySelector('[data-ha-message]');
  const code = root.querySelector('[data-ha-code]');
  let codeTimer;
  async function call(method, suffix = '') {
    const result = await fetch(root.dataset.url + suffix, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: {'X-CSRF-Token': root.dataset.csrf, Accept: 'application/json'},
    });
    if (!result.ok) throw new Error('Solicitarea nu a reusit. Verifica accesul si reincearca.');
    return result.json();
  }
  async function refresh() {
    try {
      const data = await call('GET');
      root.querySelector('[data-ha-status]').textContent = data.status;
      root.querySelector('[data-ha-instance]').textContent = data.instance_name || 'Nicio instanta confirmata';
      root.querySelector('[data-ha-seen]').textContent = data.last_seen_at || '—';
    } catch (error) { message.textContent = error.message; }
  }
  root.querySelector('[data-ha-pair]')?.addEventListener('click', async (event) => {
    event.target.disabled = true;
    try {
      const data = await call('POST', '/pairing');
      code.textContent = data.code;
      root.querySelector('[data-ha-expires]').textContent = `Cod de unica folosinta, expira la ${new Date(data.expires_at).toLocaleTimeString()}.`;
      clearTimeout(codeTimer);
      codeTimer = setTimeout(() => { code.textContent = ''; }, Math.max(0, Date.parse(data.expires_at) - Date.now()));
      message.textContent = 'Continua conectarea in Home Assistant.';
      await refresh();
    } catch (error) { message.textContent = error.message; }
    finally { event.target.disabled = false; }
  });
  root.querySelector('[data-ha-disconnect]')?.addEventListener('click', async (event) => {
    event.target.disabled = true;
    try {
      await call('DELETE');
      if (code) code.textContent = '';
      message.textContent = 'Conexiunea a fost revocata si contextul sters.';
      await refresh();
    } catch (error) { message.textContent = error.message; }
    finally { event.target.disabled = false; }
  });
  const timer = setInterval(refresh, 30000);
  window.addEventListener('pagehide', () => { clearInterval(timer); clearTimeout(codeTimer); });
})();
