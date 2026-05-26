/// <reference lib="webworker" />

import { precacheAndRoute, cleanupOutdatedCaches } from 'workbox-precaching'

declare const self: ServiceWorkerGlobalScope

// Workbox precaching: serves cached assets for offline support.
// No NavigationRoute is registered, so /api/* requests pass through untouched.
cleanupOutdatedCaches()
precacheAndRoute(self.__WB_MANIFEST)

// The new SW stays in the waiting state until the page sends SKIP_WAITING,
// which the in-app "Update" banner triggers via vite-plugin-pwa's
// updateServiceWorker(). clients.claim() on activate lets the new SW take
// over open tabs immediately so the post-update reload renders the new build.
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting()
  }
})
self.addEventListener('activate', () => self.clients.claim())
