const CACHE_NAME = "putwatch-shell-__PUTWATCH_SW_VERSION__";
const NETWORK_TIMEOUT_MS = 2500;

const PRECACHE_URLS = [
  "/",
  "/static/app.js",
  "/static/favicon.svg",
  "/static/apple-touch-icon.png",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) =>
        Promise.all(
          PRECACHE_URLS.map((url) =>
            cache.add(new Request(url, { cache: "reload" })).catch(() => null)
          )
        )
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith("putwatch-shell-") && key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

async function fetchWithTimeout(request, timeoutMs = NETWORK_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(request, { signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  // Never intercept API calls or the service worker script itself
  if (url.pathname.startsWith("/api/") || url.pathname === "/sw.js") {
    return;
  }

  // Navigation / index.html: network-first with fast timeout, falling back to cached shell
  if (event.request.mode === "navigate" || url.pathname === "/") {
    event.respondWith(
      (async () => {
        try {
          const networkResp = await fetchWithTimeout(event.request, NETWORK_TIMEOUT_MS);
          if (networkResp && networkResp.ok) {
            const cache = await caches.open(CACHE_NAME);
            cache.put("/", networkResp.clone()).catch(() => {});
            return networkResp;
          }
        } catch (_err) {
          // Network unreachable or timed out -> serve cached app shell
        }
        const cached = await caches.match("/");
        if (cached) return cached;
        return new Response("PutWatch is offline and not yet cached on this device.", {
          status: 503,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
      })()
    );
    return;
  }

  // Versioned static assets (e.g. /static/app.js?v=12345): cache-first for exact version,
  // network fallback + path-normalized cache fallback if offline.
  if (url.searchParams.has("v")) {
    event.respondWith(
      (async () => {
        const exactCached = await caches.match(event.request);
        if (exactCached) return exactCached;

        try {
          const networkResp = await fetchWithTimeout(event.request, NETWORK_TIMEOUT_MS);
          if (networkResp && networkResp.ok) {
            const cache = await caches.open(CACHE_NAME);
            cache.put(event.request, networkResp.clone()).catch(() => {});
            cache.put(url.pathname, networkResp.clone()).catch(() => {});
            return networkResp;
          }
        } catch (_err) {
          // Fall back to latest cached copy of this pathname
        }

        const pathCached =
          (await caches.match(url.pathname)) ||
          (await caches.match(event.request, { ignoreSearch: true }));
        if (pathCached) return pathCached;
        return Response.error();
      })()
    );
    return;
  }

  // Other local shell assets (icons, manifest, favicon): stale-while-revalidate
  event.respondWith(
    (async () => {
      const cached = await caches.match(event.request);
      const networkPromise = fetchWithTimeout(event.request, NETWORK_TIMEOUT_MS)
        .then(async (networkResp) => {
          if (networkResp && networkResp.ok) {
            const cache = await caches.open(CACHE_NAME);
            cache.put(event.request, networkResp.clone()).catch(() => {});
          }
          return networkResp;
        })
        .catch(() => null);

      if (cached) {
        event.waitUntil(networkPromise);
        return cached;
      }
      const networkResp = await networkPromise;
      return networkResp || Response.error();
    })()
  );
});
