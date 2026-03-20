/// <reference lib="webworker" />

import { precacheAndRoute, cleanupOutdatedCaches } from 'workbox-precaching'

declare const self: ServiceWorkerGlobalScope

// Workbox precaching: serves cached assets for offline support.
// No NavigationRoute is registered, so /api/* requests pass through untouched.
cleanupOutdatedCaches()
precacheAndRoute(self.__WB_MANIFEST)

// Activate new SW immediately and take control of all open tabs.
// skipWaiting() bypasses the waiting phase; clients.claim() ensures the
// new SW controls existing tabs without requiring a second navigation.
// Combined with autoUpdate registration mode, this triggers a page reload
// via the controllerchange event so users always get the latest deploy.
self.skipWaiting()
self.addEventListener('activate', () => self.clients.claim())
