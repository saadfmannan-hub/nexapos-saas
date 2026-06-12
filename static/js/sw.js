/* Minimal, safety-first service worker.
 * Caches ONLY versioned static assets and the offline fallback page.
 * Financial pages, customer data, reports, API and media are NEVER cached.
 */
const CACHE = "nexapos-static-v1";
const STATIC_ASSETS = [
  "/offline/",
  "/static/css/bootstrap.min.css",
  "/static/css/bootstrap-icons.min.css",
  "/static/css/app.css",
  "/static/js/bootstrap.bundle.min.js",
  "/static/js/alpine.min.js",
  "/static/icons/favicon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;

  // Static assets: cache-first
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(event.request).then((hit) => hit || fetch(event.request))
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
