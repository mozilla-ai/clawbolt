/// <reference lib="webworker" />

import { precacheAndRoute, cleanupOutdatedCaches } from 'workbox-precaching'

declare const self: ServiceWorkerGlobalScope

// Workbox precaching: serves cached assets for offline support.
// No NavigationRoute is registered, so /api/* requests pass through untouched.
cleanupOutdatedCaches()
precacheAndRoute(self.__WB_MANIFEST)

// Activate new SW immediately. Combined with autoUpdate registration mode,
// this triggers a page reload via the controllerchange event so users always
// get the latest deploy without manual intervention.
self.skipWaiting()
