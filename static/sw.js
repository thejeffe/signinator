// Signinator: an installable app that says so when the server is down.
// Pages always come from the server (never an old copy of the app); only the offline screen and its GIF are kept here,
// and shown when the server can't be reached or answers 502-504 (the web proxy is up but Signinator isn't).
const CACHE = 'signinator-offline-v7', OFFLINE = '/static/offline.html', KEEP = [OFFLINE, '/static/offline.gif', '/static/icon-192.png'];
self.addEventListener('install', (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(KEEP)).then(() => self.skipWaiting())); });
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const r = e.request, p = new URL(r.url).pathname;
  if (r.mode === 'navigate') {
    e.respondWith(fetch(r).then((res) => (res.status >= 502 && res.status <= 504 ? caches.match(OFFLINE).then((m) => m || res) : res))
      .catch(() => caches.match(OFFLINE).then((m) => m || new Response('Signinator is offline.', { status: 503, headers: { 'Content-Type': 'text/plain' } }))));
  } else if (KEEP.includes(p)) {
    e.respondWith(caches.match(r).then((m) => m || fetch(r)));
  }
});
