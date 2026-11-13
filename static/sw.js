// Vestomat как приложение («На экран Домой»): страница / и /ru/ лежит в кеше, поэтому открывается и без сети.
// Новости встроены в саму страницу, так что кешируем её целиком, а не «оболочку + данные»: оболочка — 3% веса.
//
// Сборка выходит раз в 15 минут. Страница в кеше моложе этого — отдаём сразу, не спрашивая сеть.
// Старше — идём в сеть, но ждём не дольше TIMEOUT: на плохой сети показываем кеш, а свежую версию докачиваем
// в фоне — страница сама проверит version.json и покажет кнопку «Свежие новости».
//
// Аварийный выключатель: если service worker сломал сайт, замените этот файл на
//   self.addEventListener("install", () => self.skipWaiting());
//   self.addEventListener("activate", e => e.waitUntil(caches.keys()
//     .then(keys => Promise.all(keys.map(k => caches.delete(k)))).then(() => self.registration.unregister())));
// Просто удалить файл нельзя: на 404 браузер оставляет старый service worker работать.

const VERSION = 1;  // поднять, если поменялось устройство кеша: старые кеши удалятся при активации
const PAGES = `pages-v${VERSION}`;
const ASSETS = `assets-v${VERSION}`;
const PAGE_PATHS = ["/", "/ru/"];
const FRESH = 15 * 60e3;
const TIMEOUT = 3000;
const CACHED_AT = "x-vestomat-cached-at";

// иконки изданий, шрифт и favicon'ы от Google: меняются редко — берём из кеша и тихо обновляем
const isAsset = url => url.origin === location.origin
  ? /^\/(icons\/|icon[\w-]*\.(svg|png)$|favicon\.ico$|apple-touch-icon\.png$)/.test(url.pathname)
  : ["fonts.googleapis.com", "fonts.gstatic.com"].includes(url.hostname)
    || (url.hostname === "www.google.com" && url.pathname === "/s2/favicons");

self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", event => event.waitUntil((async () => {
  for (const key of await caches.keys()) if (key !== PAGES && key !== ASSETS) await caches.delete(key);
  await self.clients.claim();
  // первую открытую страницу service worker не видел — кладём её в кеш сразу (обычно из HTTP-кеша браузера)
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const paths = new Set(windows.map(w => new URL(w.url).pathname).filter(p => PAGE_PATHS.includes(p)));
  await Promise.all([...paths].map(p => fetch(p).then(r => store(p, r)).catch(() => {})));
})()));

self.addEventListener("fetch", event => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (request.mode === "navigate" && url.origin === location.origin && PAGE_PATHS.includes(url.pathname)) {
    event.respondWith(page(url.pathname, event));
  } else if (isAsset(url)) {
    event.respondWith(asset(request, event));
  }
  // остальное (version.json, /plot/, worker, статистика) — мимо, как без service worker
});

async function store(path, response) {
  if (!response.ok) return;
  const headers = new Headers(response.headers);
  headers.set(CACHED_AT, String(Date.now()));
  const body = await response.blob();
  await (await caches.open(PAGES)).put(path, new Response(body, { status: response.status, statusText: response.statusText, headers }));
}

async function page(path, event) {
  const cached = await (await caches.open(PAGES)).match(path);
  if (cached && Date.now() - Number(cached.headers.get(CACHED_AT)) < FRESH) return cached;
  // no-cache: GitHub Pages разрешает браузеру держать страницу 10 минут, а нам нужна последняя сборка
  const network = fetch(path, { cache: "no-cache" }).then(async r => { await store(path, r.clone()); return r; });
  event.waitUntil(network.catch(() => {}));
  if (!cached) return network;
  const timeout = new Promise(resolve => setTimeout(resolve, TIMEOUT));
  return Promise.race([network, timeout]).then(r => (r?.ok ? r : cached), () => cached);
}

async function asset(request, event) {
  const cache = await caches.open(ASSETS);
  const cached = await cache.match(request);
  const network = fetch(request).then(async r => {
    // ответы с чужих сайтов без CORS непрозрачные (status 0), но картинку из них браузер покажет
    if (r.ok || r.type === "opaque") await cache.put(request, r.clone());
    return r;
  });
  event.waitUntil(network.catch(() => {}));
  return cached || network;
}
