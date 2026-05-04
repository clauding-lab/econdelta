// EconDelta PWA service worker
// Three caching tiers:
//   1. vendor/* (React, Babel)         — cache-first, never revalidate
//   2. app code (jsx/js/css/icons)     — stale-while-revalidate
//   3. RPC get_latest_dashboard()       — network-first, 5s timeout, cache fallback
//
// Cache version in CACHE_NAME — bump this string to force eviction on deploy.

const CACHE_NAME = 'econdelta-v1-2026-05-04';
const VENDOR_CACHE = 'econdelta-vendor-v1';
const RPC_CACHE = 'econdelta-rpc-v1';

const APP_SHELL = [
  './',
  './index.html',
  './styles.css',
  './manifest.webmanifest',
  './config.js',
  './lib/supabase-client.js',
  './components.jsx',
  './pages/latest.jsx',
  './pages/archive.jsx',
  './pages/runs.jsx',
  './pages/sources-about.jsx',
  './icons/icon-192.png',
  './icons/icon-512.png',
];

const VENDOR_ASSETS = [
  './vendor/react.production.min.js',
  './vendor/react-dom.production.min.js',
  './vendor/babel.min.js',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    Promise.all([
      caches.open(VENDOR_CACHE).then(c => c.addAll(VENDOR_ASSETS)),
      caches.open(CACHE_NAME).then(c => c.addAll(APP_SHELL)),
    ]).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => ![CACHE_NAME, VENDOR_CACHE, RPC_CACHE].includes(k))
          .map(k => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Tier 3: RPC call to get_latest_dashboard — network-first, 5s timeout
  if (url.pathname.endsWith('/rest/v1/rpc/get_latest_dashboard')) {
    event.respondWith(rpcStrategy(event.request));
    return;
  }

  // Tier 1: vendor — cache-first
  if (url.pathname.includes('/vendor/')) {
    event.respondWith(
      caches.match(event.request).then(r => r || fetch(event.request))
    );
    return;
  }

  // Tier 2: app shell — stale-while-revalidate
  if (event.request.method === 'GET') {
    event.respondWith(
      caches.match(event.request).then(cached => {
        const fetchPromise = fetch(event.request).then(networkRes => {
          if (networkRes.ok) {
            const clone = networkRes.clone();
            caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
          }
          return networkRes;
        }).catch(() => cached);
        return cached || fetchPromise;
      })
    );
  }
});

async function rpcStrategy(request) {
  try {
    const networkPromise = fetch(request.clone());
    const timeoutPromise = new Promise((_, reject) =>
      setTimeout(() => reject(new Error('rpc timeout')), 5000)
    );
    const networkRes = await Promise.race([networkPromise, timeoutPromise]);
    if (networkRes.ok) {
      const clone = networkRes.clone();
      caches.open(RPC_CACHE).then(c => c.put(request, clone));
      return networkRes;
    }
    throw new Error('rpc non-ok');
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({error: 'offline-no-cache'}),
      {status: 503, headers: {'Content-Type': 'application/json'}});
  }
}
