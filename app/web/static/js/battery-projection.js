(() => {
  'use strict';
  const root = document.querySelector('[data-charge-projection]');
  if (!root) return;
  const find = name => root.querySelector(`[data-charge-${name}]`);
  const control = document.getElementById('battery-target');
  const format = value => emsFormatNumber(value, 1);
  const node = (tag, text) => { const el = document.createElement(tag); el.textContent = text; return el; };
  const reasons = {
    no_battery:'Conecteaza o sursa de date pentru baterie.',
    soc_unavailable:'Avem nevoie de un SOC actual, fara date simulate.',
    power_unavailable:'Puterea de incarcare nu este disponibila sau actualizata.',
    capacity_unavailable:'Completeaza o capacitate valida a bateriei pentru estimare.',
    battery_fault:'Sursa raporteaza o eroare a bateriei.',
    invalid_limits:'Verifica limitele SOC, puterile si eficientele configurate.',
    limits_unavailable:'Completeaza puterile maxime si eficientele bateriei in configurare.',
    ambiguous_battery_allocation:'Prognoza statiei nu poate fi repartizata sigur intre aceste bancuri sau pachete.',
    solar_forecast_missing:'Lipseste o parte din prognoza solara.',
    weather_forecast_missing:'Lipseste prognoza meteo care sustine estimarea solara.',
    load_forecast_missing:'Lipseste prognoza de consum sau o citire curenta utilizabila.',
    synthetic_forecast:'Prognoza include date simulate; nu calculam o ora de incarcare.',
    stale_forecast:'Prognoza este mai veche de 6 ore.',
    untrusted_forecast:'Prognoza foloseste un istoric cu date nesigure.',
    invalid_forecast:'Prognoza contine valori sau intervale neutilizabile.',
    not_charging:'Bateria nu se incarca acum.',
    over_48_hours:'Ar dura peste 48 de ore la puterea actuala.',
  };
  const flags = {
    constant_current_load:'Consumul curent este presupus constant; nu exista inca un profil de consum.',
    nominal_capacity_fallback:'Folosim capacitatea nominala; capacitatea utilizabila nu este raportata.',
    cold_start_load:'Istoricul de consum este inca limitat.',
    low_confidence_forecast:'Prognoza are incredere redusa.',
  };
  let data, controller, chart, requestedTarget = '';
  const time = value => new Date(value).toLocaleString('ro-RO', {timeZone:data.timezone, day:'numeric', month:'short', hour:'2-digit', minute:'2-digit'});
  function estimate(value, prefix) {
    const labels = {already_at_target:'Tinta atinsa', not_charging:'Nu se incarca acum', not_reached:'Nu atinge tinta azi', partial:'Prognoza partiala', unavailable:'Estimare indisponibila', beyond_horizon:'Peste 48 ore'};
    find(prefix).textContent = value.status === 'estimated' ? `≈ ${time(value.reaches_target_at)}` : labels[value.status];
    find(`${prefix}-note`).textContent = value.reason ? reasons[value.reason] || 'Date insuficiente pentru estimare.' :
      value.minutes_to_target > 0 ? `Aproximativ ${Math.ceil(Number(value.minutes_to_target))} minute pana la ${format(data.target_soc_percent)}%.` :
      value.end_soc_percent !== null ? `${format(value.end_soc_percent)}% estimat la sfarsitul zilei · varf ${format(value.peak_soc_percent)}%.` : `Tinta ${format(data.target_soc_percent)}%.`;
    if (value.status === 'estimated' && value.end_soc_percent !== null) find(`${prefix}-note`).textContent += ` ${format(value.end_soc_percent)}% la sfarsitul zilei.`;
  }
  function draw() {
    if (!data || !find('details').open || find('content').hidden) return;
    chart ||= emsCreateChart(find('chart'));
    const css = getComputedStyle(root), color = css.getPropertyValue('--bat-in').trim();
    chart.setOption({animation:false, legend:{show:false}, tooltip:{trigger:'axis', confine:true, renderMode:'richText'},
      grid:{left:42,right:18,top:40,bottom:35},
      xAxis:{type:'time',splitNumber:4,min:Date.parse(data.generated_at),max:Date.parse(data.horizon_end),axisLabel:{hideOverlap:true,formatter:value => new Date(value).toLocaleTimeString('ro-RO',{timeZone:data.timezone,hour:'2-digit',minute:'2-digit'})}},
      yAxis:{type:'value',name:'SOC %',min:0,max:100},
      series:[{name:'SOC estimat (%)',type:'line',smooth:false,connectNulls:false,symbolSize:4,
        lineStyle:{color,width:3,type:'dashed'}, itemStyle:{color},areaStyle:{color,opacity:.08},
        data:data.points.map(p => [p.at,p.soc_percent === null ? null : Number(p.soc_percent)]),
        markLine:{silent:true,symbol:'none',data:[{yAxis:Number(data.target_soc_percent),label:{formatter:`Tinta ${format(data.target_soc_percent)}%`,position:'insideEndTop',color:css.getPropertyValue('--bat-muted').trim(),textBorderWidth:0,fontSize:10}}]}}],
    },true);
    emsApplyChartAppearance(chart); chart.resize();
  }
  function render() {
    find('context').textContent = `${data.target?.label || 'Baterie'} · ${data.timezone} · actualizat ${time(data.generated_at)}${data.confidence === 'low' ? ' · incredere redusa' : ''}`;
    estimate(data.solar,'solar'); estimate(data.current_rate,'rate');
    find('energy').textContent = data.energy_to_target_kwh === null ? '' : `${format(data.energy_to_target_kwh)} kWh pana la tinta de ${format(data.target_soc_percent)}% · putere acum ${format(data.power.value)} kW`;
    find('forecast-meta').textContent = `Prognoza solara: ${data.forecast_issued_at ? time(data.forecast_issued_at) : 'indisponibila'} · SOC observat: ${data.soc.measured_at ? time(data.soc.measured_at) : 'indisponibil'}. Tinta configurata este un reper pentru estimare.`;
    find('notes').textContent = data.flags.filter(flag => flags[flag]).map(flag => flags[flag]).join(' ');
    const body = find('rows'); body.replaceChildren();
    data.points.forEach(p => { const row = node('tr',''); [time(p.at),`${format(p.soc_percent)}%${p.kind === 'observed' ? ' · observat' : ''}`,`${format(p.pv_kw)} kW`,`${format(p.load_kw)} kW`,`${format(p.battery_power_kw)} kW`].forEach(value => row.append(node('td',value))); body.append(row); });
    find('content').hidden = false; draw();
  }
  async function load() {
    controller?.abort(); controller = new AbortController(); const signal = controller.signal;
    requestedTarget = control?.value || '';
    const params = new URLSearchParams(); if (requestedTarget) params.set('battery_id',requestedTarget);
    find('error').hidden = true;
    try {
      const response = await fetch(`/api/v1/stations/${root.dataset.station}/battery-health/projection?${params}`, {credentials:'same-origin',cache:'no-store',signal});
      if (!response.ok) throw new Error('projection_unavailable');
      const result = await response.json(); if (signal.aborted) return;
      data = result; render();
    } catch (error) { if(error.name !== 'AbortError') { find('error').hidden=false; find('content').hidden=true; find('context').textContent=''; } }
  }
  control?.addEventListener('change', () => { find('content').hidden=true; load(); });
  // Battery detail populates its selector asynchronously, including its initial target.
  if (control) new MutationObserver(() => { if (control.value !== requestedTarget) { find('content').hidden=true; load(); } }).observe(control,{childList:true});
  find('retry').addEventListener('click',load);
  find('details').addEventListener('toggle',draw);
  document.addEventListener('ems:theme-change',draw);
  document.addEventListener('visibilitychange',() => { if (!document.hidden) load(); });
  setInterval(() => { if (!document.hidden) load(); },60000);
  load();
})();
