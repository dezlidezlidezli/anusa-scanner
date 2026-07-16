'use strict';
/* ANUSA Scanner service worker — cache-first app shell plus runtime caching of the
   CDN-hosted OCR/relay libraries (tesseract core + traineddata are several MB;
   after the first online load the app starts instantly). */

const VERSION = 'anusa-scanner-v14-46';
const CORE = [
  './',
  './index.html',
  './app.js',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(VERSION).then((c) => c.addAll(CORE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Our own app shell (index.html, app.js, sw.js, manifest, the root) is served
// NETWORK-FIRST so a fresh deploy lands immediately whenever the phone is online —
// cache is only the offline fallback. The big CDN libraries (tesseract core +
// traineddata, mqtt) stay cache-first for instant, offline-capable startup.
function isAppShell(url) {
  return url.origin === self.location.origin &&
    (/\/(index\.html|app\.js|sw\.js|manifest\.webmanifest)$/.test(url.pathname) ||
     url.pathname.endsWith('/'));
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET' || !req.url.startsWith('http')) return;
  const url = new URL(req.url);

  if (isAppShell(url)) {
    e.respondWith(
      fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      }).catch(() => caches.match(req))   // offline → last good copy
    );
    return;
  }

  e.respondWith(
    caches.match(req).then((hit) => {
      if (hit) return hit;
      return fetch(req).then((res) => {
        if (res && (res.ok || res.type === 'opaque')) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      }).catch(() => hit);
    })
  );
});
