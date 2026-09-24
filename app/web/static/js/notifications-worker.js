'use strict';
self.addEventListener('push', (event) => {
  const payload = event.data?.json() || {};
  event.waitUntil(self.registration.showNotification('Notificari EMS', {body: 'Ai notificari noi in EMS.', tag: payload.tag || 'ems-notifications', data: {url: '/notifications'}}));
});
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil(clients.openWindow('/notifications'));
});
