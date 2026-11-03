/* Only the generic terminal screen is cached. Login and API responses are never cached. */
const CACHE = "{{ shell_cache|escapejs }}";
const APP = "{{ app_url|escapejs }}";
const OFFLINE = "{{ offline_url|escapejs }}";
const CLIENT = "{{ client_url|escapejs }}";
self.addEventListener("install", event => {
    event.waitUntil((async () => {
        const cache = await caches.open(CACHE);
        for (const path of [OFFLINE, CLIENT]) {
            const response = await fetch(path, {cache: "no-store"});
            if (!response.ok || response.redirected) throw new Error("Offline screen could not be prepared");
            await cache.put(path, response);
        }
    })());
});
self.addEventListener("activate", event => {
    event.waitUntil((async () => {
        for (const name of await caches.keys()) {
            if (name.startsWith("lupine-terminal-shell-") && name !== CACHE) await caches.delete(name);
        }
        await self.clients.claim();
    })());
});
self.addEventListener("fetch", event => {
    const request = event.request;
    const parsed = new URL(request.url);
    if (request.method !== "GET" || parsed.origin !== self.location.origin) return;
    if (parsed.pathname === CLIENT) {
        event.respondWith((async () => (await caches.open(CACHE)).match(CLIENT).then(cached => cached || fetch(request)))());
    } else if (request.mode === "navigate" && [APP, OFFLINE].includes(parsed.pathname)) {
        event.respondWith((async () => {
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), 4000);
            try {
                const response = await fetch(request, {signal: controller.signal});
                if (response.ok && !response.redirected) return response;
                // An expired login must not prevent access to saved offline scans.
            } catch (_) {} finally { clearTimeout(timeout); }
            const cached = await (await caches.open(CACHE)).match(OFFLINE);
            return cached || new Response("Uruchom terminal z internetem, aby przygotować tryb offline.", {
                status: 503, headers: {"Content-Type": "text/plain; charset=utf-8"}
            });
        })());
    }
});
