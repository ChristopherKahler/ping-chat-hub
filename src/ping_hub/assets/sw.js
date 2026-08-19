// Minimal service worker: makes the hub installable. Everything passes
// straight to the network — this app is useless offline by nature, so there is
// nothing worth caching and a cache here is purely a way to serve stale code.
//
// The deletion below is not tidiness. An installed PWA kept an old shell alive
// across reloads on 2026-08-19 and a shipped, correct fix read as a regression
// because the client never loaded it. Any cache this origin has ever had is
// removed on activate, every time.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil((async () => {
  for (const k of await caches.keys()) await caches.delete(k);
  await clients.claim();
})()));
self.addEventListener("fetch", () => {});
