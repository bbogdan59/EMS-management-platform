(() => {
  const dialog = document.getElementById('station-notifications');
  const bell = document.querySelector('[data-notifications-open]');
  if (!dialog || !bell || !dialog.showModal) return;
  const find = selector => dialog.querySelector(selector);
  const list = find('[data-notification-list]');
  const feedback = find('[data-notification-feedback]');
  const retry = find('[data-notification-retry]');
  const more = find('[data-notification-more]');
  const kind = find('[data-notification-kind]');
  const unread = find('[data-notification-unread]');
  const endpoint = `/stations/${dialog.dataset.station}/notifications`;
  let items = [], context = {}, controller, countController;
  const number = value => new Intl.NumberFormat('ro-RO', {maximumFractionDigits: 2}).format(Number(value));
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function counts(count) {
    const badge = bell.querySelector('[data-notification-count]');
    badge.textContent = count > 99 ? '99+' : count;
    badge.hidden = !count;
    bell.setAttribute('aria-label', count ? `Notificari, ${count} necitite` : 'Notificari');
    find('[data-panel-count]').textContent = count ? `${count} necitite` : '';
  }
  function dateLabel(payload, created) {
    if (payload.day) {
      const today = new Date(`${context.today}T12:00:00Z`);
      today.setUTCDate(today.getUTCDate() - 1);
      const date = new Date(`${payload.day}T12:00:00Z`).toLocaleDateString('ro-RO', {day: 'numeric', month: 'long', timeZone: 'UTC'});
      return `${payload.day === today.toISOString().slice(0, 10) ? 'Ieri · ' : ''}${date}`;
    }
    return new Date(payload.occurred_at || created).toLocaleString('ro-RO', {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: context.timezone});
  }
  function render() {
    list.replaceChildren();
    items.forEach(item => {
      const p = item.payload, summary = p.kind === 'summary';
      const card = el('article', `notification-item ${item.read ? '' : 'is-unread'} ${summary ? 'is-summary' : 'is-alert'} ${item.severity === 'critical' ? 'is-critical' : ''}`);
      const heading = el('div', 'notification-item-heading');
      const symbol = el('span', 'notification-symbol', summary ? '☀' : p.state === 'resolved' ? '✓' : '!');
      symbol.setAttribute('aria-hidden', 'true');
      heading.append(symbol);
      const titles = el('div');
      titles.append(el('h3', '', item.title));
      const meta = el('div', 'notification-meta');
      meta.append(el('span', '', dateLabel(p, item.created_at)));
      const qualities = {simulated: 'Date simulate', stale: 'Date intarziate', derived: 'Date derivate', mixed: 'Date mixte', missing: 'Fara date'};
      const label = summary ? (p.complete ? 'Zi completa' : qualities[p.quality] || 'Date incomplete') : p.state === 'resolved' ? 'Rezolvat' : {critical: 'Critic', error: 'Incident', warning: 'Atentie', info: 'Informare'}[item.severity] || 'Alerta';
      meta.append(el('span', `notification-tag ${summary ? p.complete ? '' : 'is-warning' : p.state === 'resolved' ? '' : item.severity === 'critical' ? 'is-danger' : 'is-warning'}`, label));
      titles.append(meta); heading.append(titles);
      if (!item.read) { const dot = el('span', 'notification-unread-dot'); dot.title = 'Necitita'; heading.append(dot); }
      card.append(heading, el('p', 'notification-body', p.body));
      if (summary) {
        const metrics = el('dl', 'notification-metrics');
        p.metrics.forEach(metric => {
          const group = el('div'); group.append(el('dt', '', metric.label));
          const value = el('dd', '', metric.value === null ? '—' : number(metric.value));
          value.append(el('small', '', metric.unit)); group.append(value);
          if (metric.value === null) group.append(el('span', 'notification-coverage', 'Fara masuratori'));
          else if (Number(metric.coverage) < 1) group.append(el('span', 'notification-coverage', `Acoperire ${number(Number(metric.coverage) * 100)}%`));
          metrics.append(group);
        });
        card.append(metrics);
        p.highlights.forEach(highlight => {
          const box = el('div', 'notification-highlight');
          box.append(el('strong', '', highlight.title), el('p', '', highlight.body)); card.append(box);
        });
      }
      const footer = el('div', 'notification-item-footer');
      if (!summary) { const link = el('a', '', 'Vezi diagnosticul →'); link.href = item.link; footer.append(link); }
      else footer.append(el('span', '', p.timezone));
      if (item.read) footer.append(el('span', '', 'Citita'));
      else {
        const read = el('button', 'notification-text-button', 'Marcheaza citita'); read.type = 'button';
        read.addEventListener('click', async () => {
          read.disabled = true;
          try {
            const response = await fetch(`${endpoint}/${item.id}/read`, {method: 'POST', headers: {'X-CSRF-Token': dialog.dataset.csrf}});
            if (!response.ok) throw new Error();
            item.read = true;
            if (unread.value === 'unread') items = items.filter(i => i.id !== item.id);
            render(); await refreshCount();
            if (!items.length) feedback.textContent = 'Nu ai notificari necitite in aceasta categorie.';
            if (dialog.open) kind.focus();
          } catch (_) { feedback.textContent = 'Nu am putut marca notificarea. Reincearca.'; read.disabled = false; }
        }); footer.append(read);
      }
      card.append(footer); list.append(card);
    });
  }
  async function load(append = false) {
    controller?.abort(); controller = new AbortController();
    const signal = controller.signal;
    const params = new URLSearchParams({kind: kind.value, unread: unread.value === 'unread', offset: append ? items.length : 0});
    feedback.textContent = 'Se incarca notificarile…'; retry.hidden = true; more.hidden = true;
    if (!append) { items = []; list.replaceChildren(); }
    try {
      const response = await fetch(`${endpoint}?${params}`, {signal, cache: 'no-store'});
      if (!response.ok) throw new Error();
      context = await response.json(); if (signal.aborted) return;
      counts(context.unread_count);
      items = append ? [...items, ...context.items.filter(item => !items.some(old => old.id === item.id))] : context.items;
      render(); more.hidden = !context.has_more;
      feedback.textContent = items.length ? '' : 'Nu ai notificari in aceasta categorie. Rezumatul pentru ieri apare dupa procesarea datelor zilei.';
    } catch (error) {
      if (error.name === 'AbortError') return;
      feedback.textContent = 'Notificarile nu au putut fi incarcate.'; retry.hidden = false;
    }
  }
  async function refreshCount() {
    countController?.abort(); countController = new AbortController();
    try {
      const response = await fetch(`${endpoint}?unread=true`, {cache: 'no-store', signal: countController.signal});
      if (response.ok) counts((await response.json()).unread_count);
    } catch (_) { /* Keep the last known count until the next refresh. */ }
  }
  bell.addEventListener('click', event => { event.preventDefault(); dialog.showModal(); document.body.classList.add('notifications-open'); load(); });
  find('[data-notifications-close]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('keydown', event => {
    if (event.key !== 'Tab') return;
    const focusable = [...dialog.querySelectorAll('a[href], button:not(:disabled), select, [tabindex="0"]')].filter(node => node.getClientRects().length);
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
  dialog.addEventListener('click', event => {
    const r = dialog.getBoundingClientRect();
    if (event.target === dialog && (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom)) dialog.close();
  });
  dialog.addEventListener('close', () => { controller?.abort(); document.body.classList.remove('notifications-open'); bell.focus(); });
  kind.addEventListener('change', () => load()); unread.addEventListener('change', () => load());
  retry.addEventListener('click', () => load()); more.addEventListener('click', () => load(true));
  refreshCount();
  setInterval(() => { if (!document.hidden && !dialog.open) refreshCount(); }, 60000);
})();
