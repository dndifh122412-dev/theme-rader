/* Theme Surge Radar - service worker
   앱 셸은 캐시(오프라인 실행), 데이터(trends.json)는 네트워크 우선, 웹폰트는 런타임 캐시. */
const CACHE = 'surge-radar-v2';
const SHELL = [
  './index.html',
  './manifest.webmanifest',
  './icon-192.png',
  './icon-512.png'
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // 데이터: network-first (실패 시 캐시 fallback)
  if (url.pathname.endsWith('trends.json')) {
    e.respondWith(
      fetch(e.request).then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // 웹폰트(Google Fonts): cache-first 런타임 캐시 → 오프라인에서도 폰트 유지
  if (url.host.includes('fonts.googleapis.com') || url.host.includes('fonts.gstatic.com')) {
    e.respondWith(
      caches.match(e.request).then(cached => cached ||
        fetch(e.request).then(res => {
          const copy = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
          return res;
        }).catch(() => cached)
      )
    );
    return;
  }

  // 앱 셸: cache-first
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
