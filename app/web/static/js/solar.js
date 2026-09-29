/* Read-only diagnostics; tracker configuration describes topology, never hardware commands. */
(() => {
  'use strict';
  const root = document.querySelector('[data-solar]');
  if (!root) return;
  const endpoint = `/api/v1/stations/${root.dataset.station}/solar`;
  const list = root.querySelector('[data-solar-inverters]'), status = root.querySelector('[data-solar-status]'), retry = root.querySelector('[data-solar-retry]');
  const cards = new Map(), groups = new Map(), charts = new Set(), redraws = new Set();
  let timezone, requestId = 0;
  const node = (tag, text, className) => { const el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (className) el.className = className; return el; };
  const format = (value, digits = 1) => value == null ? '—' : new Intl.NumberFormat('ro-RO', {maximumFractionDigits:digits}).format(Number(value));
  const time = value => value ? new Date(value).toLocaleString('ro-RO', {timeZone:timezone, day:'numeric', month:'short', hour:'2-digit', minute:'2-digit'}) : 'Fara masuratori';
  const qualities = {measured:'masurat', simulated:'simulat', stale:'neactualizat', derived:'derivat', missing:'fara date'};
  async function get(url, options = {}) {
    const response = await fetch(url, {credentials:'same-origin', cache:'no-store', ...options});
    if (!response.ok) throw new Error(response.status === 409 ? 'Configuratia s-a schimbat. Reincarca pagina.' : 'Cererea nu a putut fi finalizata.');
    return response.json();
  }
  function history(entry) {
    const details = node('details'), summary = node('summary', 'Istoric pe intrare'); details.append(summary);
    const controls = node('div', undefined, 'solar-chart-controls');
    const range = node('select'), metric = node('select');
    range.setAttribute('aria-label', `Interval ${entry.label}`); metric.setAttribute('aria-label', `Metrica ${entry.label}`);
    for (const [key,label] of [['24h','24 ore'],['7d','7 zile'],['30d','30 zile']]) { const option = node('option',label); option.value=key; range.append(option); }
    for (const [key,label] of [['power_w','Putere · W'],['voltage_v','Tensiune · V'],['current_a','Curent · A']]) { const option=node('option',label); option.value=key; metric.append(option); }
    controls.append(range,metric);
    const feedback = node('p', '', 'solar-meta'), canvas = node('div', undefined, 'solar-chart'), button = node('button','Reincearca istoricul','btn-secondary'); button.type='button'; button.hidden=true;
    canvas.setAttribute('role','img'); canvas.setAttribute('aria-label',`Istoric ${entry.label}`);
    const tableBox = node('details'), tableSummary = node('summary','Vezi valorile in tabel'), table = node('div'); table.style.overflowX='auto'; tableBox.append(tableSummary,table);
    details.append(controls,feedback,button,canvas,tableBox);
    let chart, data, controller;
    function draw() {
      if (!data || !details.open) return;
      const field=metric.value, color=getComputedStyle(root).getPropertyValue('--solar-accent').trim(), textColor=getComputedStyle(root).color;
      chart ||= echarts.init(canvas); charts.add(chart); redraws.add(draw);
      chart.setOption({animation:false, textStyle:{color:textColor}, grid:{left:48,right:15,top:20,bottom:36}, tooltip:{trigger:'axis'},
        xAxis:{type:'time',axisLabel:{color:textColor,formatter:value=>new Date(value).toLocaleString('ro-RO',{timeZone:timezone,hour:'2-digit',minute:'2-digit',...(range.value==='24h'?{}:{day:'numeric',month:'short'})})}},
        yAxis:{type:'value',name:data.units[field],axisLabel:{color:textColor},splitLine:{lineStyle:{color:textColor,opacity:0.12}}}, series:[{name:entry.label,type:'line',showSymbol:false,symbol:'none',connectNulls:false,
          lineStyle:{width:2,color},itemStyle:{color},data:data.points.map(p=>[p.start,p[field]===null?null:Number(p[field])])}]},true);
      chart.resize(); canvas.dataset.chartReady='true';
      const flags = new Set(data.points.flatMap(p=>p.flags));
      feedback.textContent = !data.points.some(p=>p[field]!==null) ? 'Fara istoric agregat in acest interval.' : `Medii ponderate in timp · ${data.resolution}. ${flags.size ? [...flags].map(f=>qualities[f]||f).join(', ')+'. ' : ''}Acoperirea incompleta ramane vizibila in tabel.`;
      table.replaceChildren();
      const grid=node('table'); grid.className='w-full text-sm'; const head=node('tr'); ['Moment',data.units[field],'Acoperire'].forEach(label=>head.append(node('th',label)));grid.append(head);
      data.points.forEach(p=>{const row=node('tr'); row.append(node('td',time(p.start)),node('td',format(p[field])),node('td',`${format(Number(p.coverage[field])*100)}%`));grid.append(row);});table.append(grid);
    }
    async function load() {
      controller?.abort(); controller=new AbortController(); const signal=controller.signal;
      feedback.textContent='Se incarca istoricul...'; button.hidden=true;
      try { data=await get(`${endpoint}/trackers/${entry.id}/history?range=${range.value}`,{signal}); if(!signal.aborted)draw(); }
      catch(error) { if(error.name!=='AbortError'){feedback.textContent='Istoricul acestei intrari nu a putut fi incarcat.';button.hidden=false;} }
    }
    details.addEventListener('toggle',()=>{if(details.open){if(data)draw();else load();}});
    range.addEventListener('change',load);metric.addEventListener('change',draw);button.addEventListener('click',load);
    return details;
  }
  function configuration(entry) {
    const details=node('details'); details.append(node('summary','Configureaza siruri si comparabilitate'));
    const form=node('form',undefined,'solar-config'), config=entry.configuration;
    const fields={};
    for(const [key,label,value] of [['label','Eticheta',entry.label],['comparison_group','Grup de comparatie',config.comparison_group],['installed_kw','Putere instalata · kWp',config.installed_kw],['azimuth_deg','Azimut · °',config.azimuth_deg],['tilt_deg','Inclinare · °',config.tilt_deg],['warning_threshold_percent','Prag diferenta · %',config.warning_threshold_percent ?? 30]]) {
      const wrap=node('label',label), input=node('input'); input.name=key;input.value=value??'';
      if(!['label','comparison_group'].includes(key)){input.type='number';input.step='any';input.min=key==='installed_kw'?'0.001':'0';input.max=key==='azimuth_deg'?'359.999':key==='installed_kw'?'100000':'90';}
      if(key==='label')input.required=true; wrap.append(input);form.append(wrap);fields[key]=input;
    }
    const label=node('label','Siruri fizice · o eticheta pe linie','wide'), strings=node('textarea');strings.rows=2;strings.value=(config.strings||[]).map(s=>s.label).join('\n');label.append(strings);form.append(label);
    form.append(node('p','Comparam doar intrari din acelasi grup, cu aceeasi orientare si inclinare. Puterea este normalizata la kWp. Lasa grupul gol pentru a opri avertizarile.','wide solar-note'));
    const submit=node('button','Salveaza','btn-primary'), feedback=node('p','','wide solar-meta');submit.type='submit';form.append(submit,feedback);
    form.addEventListener('submit',async event=>{
      event.preventDefault();submit.disabled=true;feedback.textContent='';
      const body={revision:entry.revision};Object.entries(fields).forEach(([key,input])=>body[key]=input.value.trim()||null);
      body.strings=strings.value.split('\n').map(s=>s.trim()).filter(Boolean).map((label,index)=>({identifier:config.strings?.[index]?.identifier||`string-${index+1}`,label,modules:config.strings?.[index]?.modules??null}));
      try {const saved=await get(`${endpoint}/trackers/${entry.id}`,{method:'PUT',headers:{'Content-Type':'application/json','X-CSRF-Token':root.dataset.csrf},body:JSON.stringify(body)});entry.revision=saved.revision;feedback.textContent='Configuratie salvata.';load(true);}
      catch(error){feedback.textContent=error.message;}finally{submit.disabled=false;}
    });details.append(form);return details;
  }
  function card(entry) {
    const box=node('article',undefined,'solar-input'), title=node('h4'), power=node('p','','solar-power'), electrical=node('div',undefined,'solar-electrical'), meta=node('p','','solar-meta'), provenance=node('p','','solar-meta');
    const voltage=node('span'),current=node('span'),strings=node('p','','solar-meta');electrical.append(voltage,current);box.append(title,power,electrical,meta,provenance,strings,history(entry));
    if(root.dataset.edit==='true')box.append(configuration(entry));
    box.update=e=>{title.textContent=e.label; power.textContent=`${format(e.metrics.power_w.value)} W`;voltage.textContent=`${format(e.metrics.voltage_v.value)} V`;current.textContent=`${format(e.metrics.current_a.value)} A`;
      meta.textContent=`${e.kind==='mppt'?'Tracker MPPT':'Intrare PV'} · ${e.freshness==='fresh'?'Actualizat':e.freshness==='stale'?'Date neactualizate':'Fara date'} · ${time(e.measured_at)}`;
      provenance.textContent=`${e.source==='deye_cloud'?'Deye Cloud':'Dispozitiv EMS'} · ${qualities[e.metrics.power_w.quality]||e.metrics.power_w.quality}${Object.values(e.metrics).some(m=>!m.supported)?' · Unele valori nu sunt raportate de sursa':''}`;
      strings.textContent=(e.configuration.strings||[]).length?`Siruri configurate: ${e.configuration.strings.map(s=>s.label+(s.modules?` · ${s.modules} module`:'')).join(', ')}`:'';
      box.classList.toggle('is-stale',e.freshness!=='fresh');};box.update(entry);cards.set(entry.id,box);return box;
  }
  function updateGroup(group, inverter) {
    group.totals.replaceChildren(node('span',`DC · intrari raportate: ${format(inverter.dc_total_w)} W · ${qualities[inverter.dc_quality]}`),node('span',`Iesire invertor AC: ${format(inverter.ac_output.value)} W · ${qualities[inverter.ac_output.quality]}`));
    group.warnings.replaceChildren(...inverter.warnings.map(w=>node('p',`${w.message} Diferenta ${w.difference_percent}%.`,'solar-warning')));
  }
  async function load(force=false) {
    const currentRequest=++requestId;retry.hidden=true;
    try {
      const data=await get(endpoint);if(currentRequest!==requestId)return;timezone=data.timezone;
      const entries=data.inverters.flatMap(i=>i.inputs), same=!force&&entries.length===cards.size&&entries.every(e=>cards.has(e.id));
      if(same&&entries.length){status.textContent='';entries.forEach(e=>cards.get(e.id).update(e));data.inverters.forEach(i=>updateGroup(groups.get(i.id),i));return;}
      charts.forEach(chart=>chart.dispose());charts.clear();redraws.clear();cards.clear();groups.clear();list.replaceChildren();
      status.textContent=entries.length?'':'Sursa conectata nu raporteaza inca detalii MPPT/PV. Intrarile nesuportate nu sunt afisate ca 0 W.';
      data.inverters.forEach(inverter=>{
        const group=node('div',undefined,'solar-inverter');group.append(node('h3',inverter.label));
        const totals=node('div',undefined,'solar-totals'), warnings=node('div');group.append(totals,warnings);groups.set(inverter.id,{totals,warnings});updateGroup(groups.get(inverter.id),inverter);
        const grid=node('div',undefined,'solar-input-grid');inverter.inputs.forEach(e=>grid.append(card(e)));group.append(grid);list.append(group);
      });
    } catch(_) {status.textContent='Detaliile PV nu au putut fi incarcate.';retry.hidden=false;}
  }
  retry.addEventListener('click',()=>load(true));
  document.addEventListener('ems:theme-change',()=>redraws.forEach(draw=>draw()));
  new ResizeObserver(()=>charts.forEach(chart=>chart.resize())).observe(root);
  const observer=new IntersectionObserver(entries=>{if(entries.some(e=>e.isIntersecting)){load();observer.disconnect();}});observer.observe(root);
  setInterval(()=>{if(!document.hidden&&root.getBoundingClientRect().bottom>0)load();},60000);
})();
