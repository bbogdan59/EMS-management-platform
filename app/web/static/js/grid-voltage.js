(() => {
  'use strict';
  const root = document.querySelector('[data-grid-voltage]');
  if (!root) return;
  const find = name => root.querySelector(`[data-voltage-${name}]`);
  const day = find('day'), source = find('source'), status = find('status');
  const content = find('content'), error = find('error'), canvas = find('chart');
  const colors = ['#d59b28', '#41a28c', '#648eda'];
  const qualities = {measured:'masurat', derived:'derivat', simulated:'simulat', stale:'neactualizat', missing:'fara date'};
  const node = (tag, text, cls) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; };
  const format = value => emsFormatNumber(value, 1);
  const escape = value => String(value).replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  let chart, data, controller, followToday = true, selection = null;
  const time = (value, offset = false) => new Date(value).toLocaleTimeString('ro-RO', {
    timeZone:data.timezone, hour:'2-digit', minute:'2-digit', ...(offset ? {timeZoneName:'shortOffset'} : {}),
  });
  function renderTable() {
    const body = find('rows');
    body.replaceChildren();
    if (!data || !root.querySelector('.voltage-table').open) return;
    const fragment = document.createDocumentFragment();
    data.phases[0].points.forEach((point, index) => {
      if (!data.phases.some(phase => phase.points[index].samples)) return;
      const row = node('tr'); row.append(node('td', time(point.start, true)));
      data.phases.forEach(phase => {
        const p = phase.points[index];
        row.append(node('td', p.samples ? `${format(p.mean_v)} [${format(p.min_v)} – ${format(p.max_v)}] · ${qualities[p.quality]} · ${p.samples} citiri` : 'Fara date'));
      });
      fragment.append(row);
    });
    body.append(fragment);
  }
  function draw() {
    const summary = find('phases'); summary.replaceChildren();
    data.phases.forEach((phase, index) => {
      const box = node('article', undefined, 'voltage-phase');
      box.style.setProperty('--phase-color', colors[index]);
      const value = node('p', format(phase.latest.value_v), 'voltage-value'); value.append(node('small', ' V'));
      box.append(node('h3', phase.phase), value);
      box.append(node('p', phase.latest.measured_at ? `Ultima citire · ${time(phase.latest.measured_at, true)} · ${qualities[phase.latest.quality]}` : 'Faza fara citiri'));
      if (phase.samples) {
        box.append(node('p', `Min ${format(phase.min_v)} · Max ${format(phase.max_v)} V`, 'voltage-range'));
        box.append(node('p', `${phase.observed_minutes}/${data.elapsed_minutes} minute cu citiri · ${qualities[phase.quality]}`));
      }
      summary.append(box);
    });
    const hasData = data.phases.some(phase => phase.samples > 0);
    find('empty').hidden = hasData;
    const provider = data.sources.find(item => item.id === data.source_id)?.provider;
    status.textContent = `${data.timezone} · ${data.day} · ${provider === 'deye_cloud' ? 'Deye Cloud' : provider ? 'Dispozitiv EMS' : 'Nicio sursa disponibila'} · actualizat ${time(data.generated_at)}`;
    root.querySelector('.voltage-live').textContent = followToday ? '↻ Actualizare la 60 s' : 'Istoric zilnic';
    content.hidden = false;
    chart ||= emsCreateChart(canvas);
    const zoom = chart.getOption()?.dataZoom?.[0];
    const selected = chart.getOption()?.legend?.[0]?.selected;
    chart.setOption({
      animation:false,
      grid:{left:48, right:16, top:50, bottom:75},
      legend:{data:data.phases.map(p => p.phase), selected},
      tooltip:{trigger:'axis', formatter: params => {
        const index = params[0]?.dataIndex;
        if (index === undefined) return '';
        const lines = [escape(time(data.phases[0].points[index].start, true))];
        data.phases.forEach(phase => {
          const p = phase.points[index];
          lines.push(escape(`${phase.phase}: ${format(p.mean_v)} V · min ${format(p.min_v)} / max ${format(p.max_v)} · ${p.samples} citiri · ${qualities[p.quality]}`));
        });
        return lines.join('<br>');
      }},
      xAxis:{type:'time', min:Date.parse(data.start), max:Date.parse(data.end), axisLabel:{formatter:value => time(value)}},
      yAxis:{type:'value', name:'V', scale:true,
        min:bounds => Number.isFinite(bounds.min) ? Math.max(0, Math.floor(bounds.min - 5)) : 0,
        max:bounds => Number.isFinite(bounds.max) ? Math.ceil(bounds.max + 5) : 260},
      dataZoom:[{type:'slider', start:zoom?.start ?? 0, end:zoom?.end ?? 100, height:22, bottom:10,
        fillerColor:'rgba(213,155,40,.14)', borderColor:'#8a957d', handleStyle:{color:'#d59b28', borderColor:'#d59b28'}}],
      series:data.phases.flatMap((phase, index) => ['mean_v', 'min_v', 'max_v'].map(field => ({
        name:phase.phase, type:'line', connectNulls:false, smooth:false,
        showSymbol:true, showAllSymbol:true, symbolSize:field === 'mean_v' ? 3.5 : 2,
        lineStyle:{color:colors[index], width:field === 'mean_v' ? 2 : 1, opacity:field === 'mean_v' ? 1 : .45},
        itemStyle:{color:colors[index], opacity:field === 'mean_v' ? 1 : .45},
        data:phase.points.map(p => [p.start, p[field] === null ? null : Number(p[field])]),
      }))),
    }, true);
    emsApplyChartAppearance(chart); chart.resize(); renderTable();
  }
  async function load() {
    controller?.abort(); controller = new AbortController(); const signal = controller.signal;
    error.hidden = true; status.textContent = 'Se actualizeaza tensiunea...';
    const params = new URLSearchParams();
    if (!followToday && day.value) params.set('day', day.value);
    if (selection) params.set('source_id', selection);
    try {
      const response = await fetch(`/api/v1/stations/${root.dataset.station}/grid-voltage?${params}`, {credentials:'same-origin', cache:'no-store', signal});
      if (!response.ok) throw new Error('voltage_unavailable');
      const result = await response.json();
      if (signal.aborted) return;
      data = result;
      selection = data.source_id;
      day.value = data.day; day.min = data.earliest_day; day.max = data.today; day.disabled = false;
      source.replaceChildren();
      data.sources.forEach(item => { const option = node('option', `${item.label} · ${item.provider === 'deye_cloud' ? 'Deye Cloud' : 'EMS'}`); option.value = item.id; source.append(option); });
      if (!data.sources.length) source.append(node('option', 'Nicio sursa disponibila'));
      source.disabled = !data.sources.length; source.value = data.source_id || '';
      draw();
    } catch (problem) {
      if (problem.name === 'AbortError') return;
      content.hidden = true; error.hidden = false; status.textContent = '';
    }
  }
  day.addEventListener('change', () => { if (day.validity.valid && day.value) { followToday = day.value === data?.today; content.hidden = true; load(); } });
  source.addEventListener('change', () => { selection = source.value; content.hidden = true; load(); });
  find('today').addEventListener('click', () => { followToday = true; content.hidden = true; load(); });
  find('retry').addEventListener('click', load);
  root.querySelector('.voltage-table').addEventListener('toggle', renderTable);
  document.addEventListener('visibilitychange', () => { if (!document.hidden && followToday) load(); });
  setInterval(() => { if (!document.hidden && followToday) load(); }, 60000);
  load();
})();
