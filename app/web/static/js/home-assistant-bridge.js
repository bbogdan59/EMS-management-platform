(() => {
  const root = document.querySelector('[data-ha-bridge]');
  if (!root) return;
  const message = root.querySelector('[data-ha-message]');
  const code = root.querySelector('[data-ha-code]');
  const live = root.querySelector('[data-ha-live]');
  const notice = root.querySelector('[data-ha-refresh]');
  let codeTimer;
  let refreshController;
  async function call(method, suffix = '') {
    const result = await fetch(root.dataset.url + suffix, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: {'X-CSRF-Token': root.dataset.csrf, Accept: 'application/json'},
    });
    if (!result.ok) throw new Error('Solicitarea nu a reusit. Verifica accesul si reincearca.');
    return result.json();
  }
  async function refresh() {
    if (document.hidden) return;
    refreshController?.abort();
    const controller = new AbortController();
    refreshController = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(root.dataset.statusUrl, {
        credentials: 'same-origin', cache: 'no-store',
        headers: {Accept: 'text/html'}, signal: controller.signal,
      });
      if (!response.ok || response.redirected) throw new Error('unavailable');
      const parsed = new DOMParser().parseFromString(await response.text(), 'text/html');
      const snapshot = parsed.querySelector('[data-ha-snapshot]');
      if (!snapshot) throw new Error('unavailable');
      if (refreshController !== controller) return;
      live.replaceChildren(snapshot);
      notice.textContent = 'Actualizare automata la fiecare 30 de secunde.';
    } catch {
      if (refreshController !== controller) return;
      live.querySelectorAll('[data-ha-value]').forEach(value => {
        value.textContent = 'Indisponibil';
        value.classList.add('is-missing');
      });
      const status = live.querySelector('[data-ha-status]');
      if (status) {
        status.textContent = 'Actualizare indisponibila';
        status.dataset.state = 'offline';
      }
      notice.textContent = 'Nu putem verifica datele. Verifica accesul si conexiunea; reincercam automat.';
    } finally { clearTimeout(timeout); }
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
  let timer = setInterval(refresh, 30000);
  refresh();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('pagehide', () => {
    clearInterval(timer);
    clearTimeout(codeTimer);
    const controller = refreshController;
    refreshController = null;
    controller?.abort();
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) { timer = setInterval(refresh, 30000); refresh(); }
  });
})();
