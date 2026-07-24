'use strict';
/* ANUSA Scanner service worker.

   Two caches:
   - SHELL  (versioned): the app's own small files. Bumped every deploy; served
     network-first so a fresh deploy lands immediately, cache is the offline fallback.
   - ASSETS (stable):    the heavy immutable stuff — the ~10MB OCR models and the pinned
     CDN runtime (ONNX Runtime WASM, mqtt, jsQR). Served cache-first and NOT wiped on a
     deploy, so a version bump never re-downloads the ~20MB. Bump ASSETS_VER only when the
     models or a pinned library version actually change. */

const SHELL  = 'anusa-shell-v14-90';   // ← bump every deploy (small app files)
const ASSETS = 'anusa-assets-v1';      // ← STABLE; bump ONLY when models/CDN libs change
const KEEP = [SHELL, ASSETS];

const CORE = [
  './',
  './index.html',
  './app.js',
  './paddleocr.js',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/apple-touch-icon.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(SHELL).then((c) => c.addAll(CORE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !KEEP.includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// The app's own shell (index.html, app.js, paddleocr.js, sw.js, manifest, the root).
function isAppShell(url) {
  return url.origin === self.location.origin &&
    (/\/(index\.html|app\.js|paddleocr\.js|sw\.js|manifest\.webmanifest)$/.test(url.pathname) ||
     url.pathname.endsWith('/'));
}

// Heavy immutable assets that must survive deploys: the OCR models (same-origin /models/)
// and the pinned CDN libraries (ONNX Runtime WASM, mqtt, jsQR — all version-locked URLs).
function isStableAsset(url) {
  return url.pathname.includes('/models/') || url.origin === 'https://cdn.jsdelivr.net';
}

// Cache-first from a named cache: serve the cached copy if present, else fetch + store it.
function cacheFirst(req, cacheName) {
  return caches.match(req).then((hit) => {
    if (hit) return hit;
    return fetch(req).then((res) => {
      if (res && (res.ok || res.type === 'opaque')) {
        const copy = res.clone();
        caches.open(cacheName).then((c) => c.put(req, copy)).catch(() => {});
      }
      return res;
    }).catch(() => hit);
  });
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET' || !req.url.startsWith('http')) return;
  const url = new URL(req.url);

  if (isAppShell(url)) {
    // Network-first: latest deploy when online, cached shell when offline.
    e.respondWith(
      fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(SHELL).then((c) => c.put(req, copy)).catch(() => {});
        }
        return res;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // Big immutable assets → stable cache (survives deploys). Everything else → shell cache.
  e.respondWith(cacheFirst(req, isStableAsset(url) ? ASSETS : SHELL));
});
