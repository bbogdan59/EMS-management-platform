'use strict';
const assistantForm = document.getElementById('energy-assistant-form');
assistantForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const target = document.getElementById('energy-assistant-answer');
  const button = assistantForm.querySelector('button');
  target.textContent = 'Se verifica datele...';
  button.disabled = true;
  try {
    const data = new FormData(assistantForm);
    if (!data.get('day')) data.delete('day');
    const response = await fetch(assistantForm.action, {method: 'POST', body: data, headers: {'Accept': 'application/json'}, signal: AbortSignal.timeout(10000)});
    const answer = await response.json();
    target.textContent = answer.answer || answer.detail || 'Raspuns indisponibil.';
    if (answer.evidence?.length) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = 'Dovezi, formule si acoperire';
      const pre = document.createElement('pre');
      pre.className = 'overflow-x-auto text-xs';
      pre.textContent = JSON.stringify(answer.evidence, null, 2);
      details.append(summary, pre);
      target.append(details);
    }
  } catch (_) { target.textContent = 'Datele nu pot fi consultate acum. Reincearca mai tarziu.'; }
  finally { button.disabled = false; }
});
