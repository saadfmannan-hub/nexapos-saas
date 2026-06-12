/* Minimal, safety-first service worker.
 * Caches ONLY static assets and the offline fallback page.
 * Financial pages, customer data, reports, API and media are NEVER cached.
 *
 * Static assets use stale-while-revalidate: served from cache for speed,
 * refreshed from the network in the background, so CSS/JS updates roll
 * out automatically (no permanently stale styling after a deploy).
 */
const CACHE = "nexapos-static-v2";
const STATIC_ASSETS = [
  "/offline/",
  "/static/css/bootstrap.min.css",
  "/static/css/bootstrap-icons.min.css",
  "/static/css/app.css",
  "/static/js/bootstrap.bundle.min.js",
  "/static/js/alpine.min.js",
  "/static/js/chart.umd.min.js",
  "/static/icons/favicon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;

  // Static assets: stale-while-revalidate
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const cached = await cache.match(event.request);
        const refresh = fetch(event.request)
          .then((response) => {
            if (response && response.ok) cache.put(event.request, response.clone());
            return response;
          })
          .catch(() => cached);
        return cached || refresh;
      })
    );
    return;
  }
  // Never cache app/API/media pages; network with offline fallback for
  // top-level navigations only.
  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(() => caches.match("/offline/"))
    );
  }
});
