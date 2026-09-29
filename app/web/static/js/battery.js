/* Source-scoped battery analytics; numeric conversion is presentation-only. */
(() => {
  'use strict';
  const root = document.querySelector('[data-battery-page]');
  const widget = document.querySelector('[data-battery-widget]');
  if (!root && !widget) return;
  const number = value => value === null || value === undefined ? null : Number(value);
  const format = (value, digits = 1) => number(value) === null ? '—' : emsFormatNumber(number(value), digits);
  const qualities = {measured: 'masurat', estimated: 'estimat', declared: 'declarat', simulated: 'simulat', stale: 'neactualizat', missing: 'fara date'};
  const states = {charging: 'Incarcare', discharging: 'Descarcare', idle: 'In repaus', fault: 'Eroare raportata', unknown: 'Stare necunoscuta'};
  const $ = id => document.getElementById(`battery-${id}`);
  const text = (id, value) => { $(id).textContent = value; };
  const node = (tag, value, cls) => { const el = document.createElement(tag); if (value !== undefined) el.textContent = value; if (cls) el.className = cls; return el; };
  const quality = metric => qualities[metric?.quality] || 'fara date';
  const metricValue = metric => metric?.value == null ? (metric?.supported === false ? 'Neraportat de sursa' : 'Fara date') : `${format(metric.value)} ${metric.unit}`;
  const time = (value, timezone, options = {}) => value ? new Intl.DateTimeFormat('ro-RO', {timeZone: timezone, day:'2-digit', month:'short', ...options}).format(new Date(value)) : 'fara observatie';
  const isoDay = (value, timezone) => new Intl.DateTimeFormat('en-CA', {timeZone:timezone, year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date(value));
  const context = period => `Acoperire ${format(number(period.coverage) * 100, 1)}% · ${qualities[period.quality]}${period.flags.includes('late') ? ' · primit cu intarziere' : ''}`;
  async function request(url, signal) {
    const response = await fetch(url, {credentials:'same-origin', signal, headers:{Accept:'application/json'}});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  if (widget) {
    const target = widget.querySelector('[data-battery-widget-data]');
    async function loadWidget() {
      try {
        const data = await request(`/api/v1/stations/${widget.dataset.station}/battery-health/summary`);
        target.replaceChildren();
        if (!data.batteries.length) { target.textContent = 'Conecteaza o sursa pentru diagnosticul bateriei.'; return; }
        const battery = data.batteries[0];
        const pairs = [['SOC', metricValue(battery.soc)], ['Temperatura', metricValue(battery.temperature)],
          ['SOH', battery.soh.status === 'unavailable' ? 'Indisponibil' : `${format(battery.soh.value)}% · ${battery.soh.status === 'estimated' ? 'estimat' : 'raportat'}`],
          ['Observatie', `${time(battery.soc.measured_at, data.timezone, {hour:'2-digit',minute:'2-digit'})} · ${quality(battery.soc)}`]];
        if (data.batteries.length > 1) pairs.unshift(['Surse / baterii', `${data.batteries.length} · consultare separata`]);
        pairs.forEach(([label,value]) => { const item = node('div'); item.append(node('p',label),node('strong',value)); target.append(item); });
      } catch { target.replaceChildren(node('p','Diagnosticul nu este disponibil momentan. Deschide detaliile pentru a reincerca.')); }
    }
    loadWidget();
    setInterval(() => { if (!document.hidden) loadWidget(); }, 60000);
  }
  if (!root) return;
  let data, controller;
  const selected = {days:'14'};
  const endpoint = `/api/v1/stations/${root.dataset.station}/battery-health`;

  function renderCurrent(live) {
    const hero = root.querySelector('.battery-live');
    hero.classList.toggle('is-missing', live?.soc.value == null);
    hero.style.setProperty('--soc', `${Math.max(0, Math.min(100, number(live?.soc.value) || 0))}%`);
    text('soc', live?.soc.value == null ? '—' : `${format(live.soc.value, 0)}%`);
    text('state', live ? `${states[live.state]}${live.power.value == null ? '' : ` · ${format(Math.abs(number(live.power.value)),2)} kW`}` : 'Fara sursa conectata');
    $('state').classList.toggle('is-charging', live?.state === 'charging');
    text('stored', `${format(live?.stored_energy.value)} / ${format(live?.nominal_capacity.value)} kWh`);
    text('temperature', live?.temperature.value == null ? '— °C' : `${format(live.temperature.value)} °C`);
    text('live-meta', live ? `${live.target.label} · ${live.target.source === 'deye_cloud' ? 'Deye Cloud' : 'Dispozitiv EMS'} · ${time(live.soc.measured_at, data.timezone, {hour:'2-digit',minute:'2-digit'})} · SOC ${quality(live.soc)} · putere ${quality(live.power)} · temperatura ${quality(live.temperature)}` : 'Adauga un dispozitiv din configurarea statiei.');
  }

  function totals(prefix, period) {
    ['charge','discharge'].forEach(key => {
      const el = $(`${prefix}-${key}`);
      el.replaceChildren(document.createTextNode(format(period[`${key}_kwh`])), node('small',' kWh'));
    });
  }

  function table(id, buckets, hourly = false, temperatures = false) {
    const table = node('table'), head = node('thead'), row = node('tr'), body = node('tbody');
    const headings = temperatures ? ['Zi','Min °C','Max °C','Acoperire','Calitate'] : ['Interval','In kWh','Out kWh','SOC %','Acoperire putere / SOC','Calitate putere / SOC'];
    headings.forEach(label => { const th = node('th',label); th.scope = 'col'; row.append(th); });
    head.append(row);
    buckets.forEach(bucket => {
      const row = node('tr');
      let label = time(bucket.start, data.timezone, hourly ? {hour:'2-digit',minute:'2-digit',timeZoneName:'shortOffset'} : {});
      const first = node('td');
      if (!hourly && !temperatures) {
        const button = node('button', label); button.type = 'button'; button.addEventListener('click',() => selectDay(isoDay(bucket.start, data.timezone))); first.append(button);
      } else first.textContent = label;
      row.append(first);
      const values = temperatures ? [format(bucket.temperature_min_c),format(bucket.temperature_max_c),`${format(number(bucket.coverage.temperature)*100)}%`,qualities[bucket.quality.temperature]] :
        [format(bucket.charge_kwh,2),format(bucket.discharge_kwh,2),format(bucket.soc_percent),`${format(number(bucket.coverage.power)*100)}% / ${format(number(bucket.coverage.soc)*100)}%`,`${qualities[bucket.quality.power]} / ${qualities[bucket.quality.soc]}`];
      values.forEach(value => row.append(node('td', value))); body.append(row);
    });
    table.append(head,body); $(id).replaceChildren(table);
  }

  function charts() {
    if (!data || typeof echarts === 'undefined') return;
    const css = getComputedStyle(root), ink = css.getPropertyValue('--bat-muted').trim(), line = css.getPropertyValue('--bat-line').trim();
    const green = css.getPropertyValue('--bat-in').trim(), orange = css.getPropertyValue('--bat-out').trim();
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const common = {animation:!reduced, textStyle:{fontFamily:'inherit',color:ink}, aria:{enabled:true},
      tooltip:{trigger:'axis', confine:true, renderMode:'richText'}, axisPointer:{link:[{xAxisIndex:'all'}]}};
    const axis = {type:'category', axisTick:{show:false},axisLine:{lineStyle:{color:line}},axisLabel:{color:ink,fontSize:10}};
    const valueAxis = {type:'value',axisLabel:{color:ink,fontSize:10},splitLine:{lineStyle:{color:line,type:'dashed'}}};
    for (const [id,buckets,hourly] of [['hourly-chart',data.hourly,true],['daily-chart',data.daily,false]]) {
      const chart = emsCreateChart($(id));
      const labels = buckets.map(b => time(b.start,data.timezone,hourly ? {day:undefined,month:undefined,hour:'2-digit',minute:'2-digit',timeZoneName:'shortOffset'} : {}));
      const axisData = {...axis,data:labels};
      const gradient = color => new echarts.graphic.LinearGradient(0,0,0,1,[{offset:0,color},{offset:1,color:color+'66'}]);
      const series = [
        {name:'Incarcata (kWh)',type:'bar',data:buckets.map(b => number(b.charge_kwh)),barMaxWidth:18,stack:'energy',itemStyle:{color:gradient(green),borderRadius:[5,5,0,0]}},
        {name:'Descarcata (kWh)',type:'bar',data:buckets.map(b => b.discharge_kwh == null ? null : -number(b.discharge_kwh)),barMaxWidth:18,stack:'energy',itemStyle:{color:gradient(orange),borderRadius:[0,0,5,5]}}
      ];
      if (hourly) {
        series.forEach(s => { s.xAxisIndex=1; s.yAxisIndex=1; });
        series.unshift({name:'SOC mediu (%)',type:'bar',data:buckets.map(b => number(b.soc_percent)),barMaxWidth:17,itemStyle:{color:gradient(ink),borderRadius:[3,3,0,0]}});
      }
      const hasData = buckets.some(b => b.charge_kwh != null || (hourly && b.soc_percent != null));
      chart.setOption({...common,graphic:hasData ? [] : [{type:'text',left:'center',top:'center',style:{text:'Fara observatii in aceasta perioada',fill:ink,fontSize:11}}],
        grid:hourly ? [{left:38,right:28,top:32,height:'39%'},{left:38,right:28,top:'64%',bottom:36}] : {left:42,right:18,top:34,bottom:42},
        xAxis:hourly ? [{...axisData,axisLabel:{show:false}}, {...axisData,gridIndex:1,axisLabel:{...axis.axisLabel,formatter:label=>label.split(' ')[0],interval:5}}] : axisData,
        yAxis:hourly ? [{...valueAxis,min:0,max:100,interval:50,name:'SOC %'}, {...valueAxis,gridIndex:1,name:'kWh'}] : {...valueAxis,name:'kWh'},series});
      if (!hourly) chart.on('click',params => { if (Number.isInteger(params.dataIndex)) selectDay(isoDay(buckets[params.dataIndex].start,data.timezone)); });
    }
    const chart = emsCreateChart($('temp-chart'));
    const days = data.temperature_days;
    chart.setOption({...common,graphic:days.some(b=>b.temperature_min_c!=null) ? [] : [{type:'text',left:'center',top:'center',style:{text:'Sursa nu a raportat temperaturi',fill:ink,fontSize:11}}],
      grid:{left:36,right:15,top:30,bottom:30},xAxis:{...axis,data:days.map(b=>time(b.start,data.timezone))},yAxis:{...valueAxis,name:'°C',scale:true},
      series:[{name:'Minim °C',type:'line',showSymbol:false,connectNulls:false,data:days.map(b=>number(b.temperature_min_c)),lineStyle:{color:'#559bc9',width:3},itemStyle:{color:'#559bc9'}},
        {name:'Maxim °C',type:'line',showSymbol:false,connectNulls:false,data:days.map(b=>number(b.temperature_max_c)),lineStyle:{color:orange,width:3},itemStyle:{color:orange}}]});
  }

  function render() {
    const {current:live} = data;
    renderCurrent(live);
    $('target').replaceChildren(...data.targets.map(t => { const option = node('option',t.label); option.value=t.id; return option; }));
    if (!data.targets.length) $('target').append(node('option','Nicio sursa conectata'));
    if (live) { $('target').value=live.target.id; selected.battery_id=live.target.id; }
    const localToday = isoDay(data.generated_at,data.timezone);
    $('end').value=data.end_date; $('end').max=localToday; $('day').value=data.day; $('day').max=data.end_date;
    $('day').min=isoDay(data.daily[0].start,data.timezone); $('days').value=String(data.days);
    text('day-title',data.day === localToday ? 'Astazi' : 'Ziua selectata');
    text('day-context',`${time(data.today.start,data.timezone)} · ${data.timezone} · ${context(data.today)}`);
    text('period-title',`${data.days} zile · energie`);
    text('period-context',`${time(data.period.start,data.timezone)} – ${time(data.end_date+'T12:00:00Z',data.timezone)} · ${data.period.complete_days} zile complete · ${context(data.period)}`);
    totals('day',data.today); totals('period',data.period);
    $('notices').replaceChildren(...data.notices.map(notice => { const el = node('div',undefined,'battery-notice'); el.append(node('strong',notice.title),node('p',notice.explanation)); return el; }));
    const temp = data.temperature_days.at(-1);
    text('temp-range',temp.temperature_min_c == null ? 'Fara date de temperatura' : `${format(temp.temperature_min_c)} – ${format(temp.temperature_max_c)} °C`);
    text('temp-context',`Ultimele 7 zile pana la ziua selectata · albastru: minim, portocaliu: maxim. Acoperire temperatura in ziua selectata: ${format(number(temp.coverage.temperature)*100)}% · ${qualities[temp.quality.temperature]}.`);
    $('extremes').replaceChildren(...data.extremes.map(extreme => { const el=node('div'); el.append(node('p',extreme.kind==='coldest'?'Cel mai rece':'Cel mai cald'),node('strong',`${format(extreme.value_c)} °C`,extreme.kind==='coldest'?'':'battery-out'),node('p',`${time(extreme.measured_at,data.timezone)} · ${qualities[extreme.quality]}`)); return el; }));
    text('soh',live?.soh.value == null ? 'Indisponibil' : `${format(live.soh.value)}%`);
    text('soh-meta',live?.soh.status === 'unavailable' || !live ? 'Sursa nu raporteaza SOH. Nivelul SOC nu indica sanatatea bateriei.' : `${live.soh.status==='estimated'?'Estimat de sursa':'Raportat de sursa'} · ${quality(live.soh)} · incredere: ${{low:'redusa',medium:'medie',high:'ridicata',unknown:'nespecificata'}[live.soh.confidence]} · ${live.soh.method || 'metoda nespecificata'}${live.soh.method_version ? ' / '+live.soh.method_version : ''} · ${time(live.soh.measured_at,data.timezone,{hour:'2-digit',minute:'2-digit'})}`);
    text('efc',format(data.period.efc,2));
    const reasons = {available:'Estimat din energia observata.',partial_history:'Istoric partial: doar ciclurile observate.',capacity_missing:'Capacitate nominala lipsa sau zero in istoric.',no_power_data:'Fara istoric de putere disponibil.'};
    text('efc-meta',`${reasons[data.period.efc_reason]} EFC ${qualities[data.period.efc_quality]} · acoperire ${format(number(data.period.coverage)*100)}%. ${data.period.flags.includes('simulated') ? 'Include date simulate.' : ''}`);
    $('facts').replaceChildren();
    if (live) {
      const facts = [['Tensiune',`${metricValue(live.voltage)} · ${quality(live.voltage)}`],['Curent DC',`${metricValue(live.current)} · ${quality(live.current)}`],
        ['Capacitate nominala',`${metricValue(live.nominal_capacity)} · ${quality(live.nominal_capacity)}`],['Capacitate utilizabila',`${metricValue(live.usable_capacity)} · ${quality(live.usable_capacity)}`],
        ['Cicluri totale raportate',live.reported_cycles.value == null ? 'Indisponibil' : `${format(live.reported_cycles.value,0)} · ${quality(live.reported_cycles)}`],
        ['Baza calculului EFC',data.period.capacity_basis.length ? data.period.capacity_basis.join('; ') : 'Capacitate necunoscuta']];
      facts.forEach(([label,value]) => { const el=node('div'); el.append(node('dt',label),node('dd',value)); $('facts').append(el); });
    }
    table('hourly-table',data.hourly,true); table('daily-table',data.daily); table('temp-table',data.temperature_days,false,true);
    $('content').hidden=false;
    charts();
  }

  async function load() {
    controller?.abort(); controller=new AbortController();
    const localController=controller;
    $('loading').hidden=false; $('error').hidden=true; root.setAttribute('aria-busy','true');
    try {
      data=await request(`${endpoint}?${new URLSearchParams(selected)}`,controller.signal);
      render();
    } catch (error) {
      if (error.name==='AbortError') return;
      $('error').hidden=false; $('content').hidden=true;
    } finally {
      if (controller===localController) { $('loading').hidden=true; root.setAttribute('aria-busy','false'); }
    }
  }
  function selectDay(day) { selected.day=day; selected.end=data.end_date; load(); }
  $('target').addEventListener('change',() => { selected.battery_id=$('target').value; load(); });
  $('days').addEventListener('change',() => { selected.days=$('days').value; delete selected.day; load(); });
  $('end').addEventListener('change',() => { if (!$('end').value || !$('end').checkValidity()) return; selected.end=$('end').value; delete selected.day; load(); });
  $('day').addEventListener('change',() => { if ($('day').value && $('day').checkValidity()) selectDay($('day').value); });
  $('retry').addEventListener('click',load);
  root.querySelector('form').addEventListener('submit',event => event.preventDefault());
  document.addEventListener('ems:theme-change',charts);
  setInterval(() => { if (!document.hidden) load(); },300000);
  load();
})();
