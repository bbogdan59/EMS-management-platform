'use strict';
for (const button of document.querySelectorAll('[data-push-org]')) {
  button.addEventListener('click', async () => {
    const status = document.getElementById('push-status');
    try {
      if (!('serviceWorker' in navigator) || !('PushManager' in window)) throw new Error();
      const registration = await navigator.serviceWorker.register('/notifications-worker.js');
      await navigator.serviceWorker.ready;
      const encoded = button.dataset.vapidKey.replace(/-/g, '+').replace(/_/g, '/');
      const key = Uint8Array.from(atob(encoded + '='.repeat((4 - encoded.length % 4) % 4)), (c) => c.charCodeAt(0));
      const subscription = await registration.pushManager.getSubscription() || await registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
      const response = await fetch(`/organizations/${button.dataset.pushOrg}/notifications/push`, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('[name=csrf_token]').value}, body: JSON.stringify(subscription)});
      if (!response.ok) throw new Error();
      status.textContent = 'Browser conectat. Alege categoriile push dorite si salveaza preferintele.';
    } catch (_) { status.textContent = 'Push nu a putut fi activat. Verifica permisiunea browserului.'; }
  });
}
