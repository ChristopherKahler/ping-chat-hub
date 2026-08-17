// minimal service worker: makes the hub installable; everything passes
// straight to the network (this app is useless offline by nature)
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(clients.claim()));
self.addEventListener("fetch", () => {});
