// Приёмник жалоб «плохой перевод» для vestomat.rs.
// POST /report {url, comment} — от читателя: присылает жалобу в Telegram и хранит неделю.
// GET /reports — для сборки (с токеном): какие новости перевести заново моделью посильнее.
// POST /reports {results: [{url, status}]} — от сборки: чем кончилось, отмечаем реакцией на сообщение в Telegram.
// POST /stats {kinds: {new|retry|review: {items, in, out, usd}}} — от сборки: сколько она потратила на перевод.
//   Копим в ключе KV "stats": итоги по дням навсегда и каждую сборку за последние две недели.
// POST /untranslated {items: [{url, source, title, summary, reason, attempt}]} — от сборки: новости, которые модель
//   не перевела и с подсказкой (обычно мешает название — его стоит добавить в glossary.json). Присылаем в Telegram
//   одним сообщением, каждую новость один раз: сборка шлёт их снова, пока они в ленте.
// GET /costs — страница с этой статистикой, GET /stats — сами данные для неё (с ключом STATS_KEY).
//   Ключ страница берёт из адреса после # — эта часть не уходит ни на сервер, ни в Referer.
// Оригинал и перевод берём не из запроса, а из опубликованного data.json: так пожаловаться можно
// только на настоящую новость с сайта, а в Telegram не попадёт чужой текст под её видом.

import COSTS_PAGE from "./costs.html";

const ORIGINS = ["https://vestomat.rs", "http://localhost:8765"];
const DATA_URL = "https://vestomat.rs/data.json";
const PER_IP_PER_HOUR = 10;
const KEEP_SECONDS = 7 * 24 * 3600;
const COMMENT_MAX = 500;
// fixed — перевели заново, same — перевод сильной модели вышел (или уже был) таким же, gone — новость ушла из ленты,
// error — не вышло: жалоба остаётся в списке, и следующая сборка попробует ещё раз
const REACTIONS = { fixed: "👍", same: "👎", gone: "🤷", error: "🤯" };
// new — первый перевод новых новостей, retry — повтор непереведённого, review — перевод заново по жалобам
const KINDS = ["new", "retry", "review"];
const BUILDS_DAYS = 14;
const UNTRANSLATED_MAX = 10;  // за одну сборку — чтобы сообщение влезло в лимит Telegram
const UNTRANSLATED_SECONDS = 4 * 24 * 3600;  // дольше, чем новость держится в ленте

export default {
  async fetch(request, env, ctx) {
    const { pathname } = new URL(request.url);
    const origin = request.headers.get("Origin");
    const cors = ORIGINS.includes(origin) ? { "Access-Control-Allow-Origin": origin, Vary: "Origin" } : {};

    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: { ...cors, "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "86400" },
      });
    }
    if (pathname === "/report" && request.method === "POST") {
      const response = await report(request, env, ctx);
      for (const [k, v] of Object.entries(cors)) response.headers.set(k, v);
      return response;
    }
    if (pathname === "/costs" && request.method === "GET") {
      return new Response(COSTS_PAGE, { headers: { "Content-Type": "text/html; charset=utf-8", "X-Robots-Tag": "noindex" } });
    }
    if (pathname === "/stats" && request.method === "GET") {
      if (!authorized(request, env.STATS_KEY)) return json({ error: "unauthorized" }, 401);
      return json((await env.REPORTS.get("stats", "json")) || { days: {}, builds: [] });
    }
    if (["/reports", "/stats", "/untranslated"].includes(pathname) && !authorized(request, env.REPORTS_TOKEN)) return json({ error: "unauthorized" }, 401);
    if (pathname === "/reports" && request.method === "GET") return list(env);
    if (pathname === "/reports" && request.method === "POST") return resolve(request, env);
    if (pathname === "/stats" && request.method === "POST") return stats(request, env);
    if (pathname === "/untranslated" && request.method === "POST") return untranslated(request, env);
    return json({ error: "not found" }, 404);
  },
};

async function report(request, env, ctx) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad json" }, 400);
  }
  // поле-ловушка: человек его не видит, а бот заполняет — делаем вид, что всё принято
  if (body.website) return json({ ok: true });
  const url = typeof body.url === "string" ? body.url.slice(0, 2000) : "";
  const comment = typeof body.comment === "string" ? body.comment.trim().slice(0, COMMENT_MAX) : "";
  if (!url) return json({ error: "no url" }, 400);

  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const limitKey = `ip:${ip}:${Math.floor(Date.now() / 3600e3)}`;
  const count = Number(await env.REPORTS.get(limitKey)) || 0;
  if (count >= PER_IP_PER_HOUR) return json({ error: "too many" }, 429);
  const [found] = await Promise.all([
    findItem(url, env.DATA_URL || DATA_URL),
    env.REPORTS.put(limitKey, String(count + 1), { expirationTtl: 3600 }),
  ]);
  if (!found) return json({ error: "unknown item" }, 404);
  // Telegram и запись жалобы — уже после ответа читателю, чтобы окно не ждало их
  ctx.waitUntil(save(env, url, comment, found));
  return json({ ok: true });
}

async function save(env, url, comment, { item, src }) {
  const key = "r:" + (await sha1(url));
  const old = await env.REPORTS.get(key, "json");
  const lines = [`⚑ Плохой перевод${old ? ` (жалоба №${(old.count || 0) + 1})` : ""} — ${src.name}`, "",
    `Оригинал: ${item.title}`, `Перевод: ${item.ru.title}`];
  if (item.summary) lines.push("", `Подводка: ${item.summary}`, `Перевод: ${item.ru.summary}`);
  if (comment) lines.push("", `Комментарий: ${comment}`);
  lines.push("", url);
  const message = await notify(env, lines.join("\n"));

  // новая жалоба снова ждёт итога, даже если на эту новость уже жаловались и она обработана;
  // reacted — сколько первых сообщений уже отмечены реакцией
  await env.REPORTS.put(key, JSON.stringify({
    url, source: src.name, first: old?.first || new Date().toISOString(), count: (old?.count || 0) + 1,
    comments: [...(old?.comments || []), ...(comment ? [comment] : [])].slice(-10),
    messages: [...(old?.messages || []), ...(message ? [message] : [])], reacted: old?.reacted || 0,
  }), { expirationTtl: KEEP_SECONDS });
}

function authorized(request, secret) {
  return secret && request.headers.get("Authorization") === `Bearer ${secret}`;
}

async function list(env) {
  const reports = [];
  let cursor;
  do {
    const page = await env.REPORTS.list({ prefix: "r:", cursor });
    for (const { name } of page.keys) {
      const value = await env.REPORTS.get(name, "json");
      if (value && !value.status) reports.push(value);
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  return json({ reports });
}

async function resolve(request, env) {
  const { results } = await request.json();
  for (const { url, status } of results || []) {
    if (!(status in REACTIONS)) continue;
    const key = "r:" + (await sha1(url));
    const record = await env.REPORTS.get(key, "json");
    if (!record) continue;
    const pending = (record.messages || []).slice(record.reacted || 0);
    await Promise.all(pending.map(id => react(env, id, REACTIONS[status])));
    if (status === "error") continue;
    // put без срока сделал бы запись вечной — оставляем ей остаток недели
    const left = KEEP_SECONDS - (Date.now() - Date.parse(record.first)) / 1000;
    await env.REPORTS.put(key, JSON.stringify({ ...record, status, reacted: (record.messages || []).length }),
      { expirationTtl: Math.max(60, Math.round(left)) });
  }
  return json({ ok: true });
}

async function stats(request, env) {
  const { kinds } = await request.json();
  const build = { at: belgrade(Date.now()) };
  for (const kind of KINDS) {
    const spent = kinds?.[kind];
    if (!spent) continue;
    build[kind] = {};
    for (const field of ["items", "in", "out", "usd"]) build[kind][field] = Number(spent[field]) || 0;
  }
  if (!KINDS.some(k => build[k])) return json({ error: "nothing spent" }, 400);
  // сборки идут по очереди, так что читать и перезаписывать один ключ безопасно
  const all = (await env.REPORTS.get("stats", "json")) || { days: {}, builds: [] };
  const day = (all.days[build.at.slice(0, 10)] ||= { builds: 0 });
  day.builds++;
  for (const kind of KINDS.filter(k => build[k])) {
    const sum = (day[kind] ||= {});
    for (const [field, value] of Object.entries(build[kind])) sum[field] = Math.round(((sum[field] || 0) + value) * 1e6) / 1e6;
  }
  const since = belgrade(Date.now() - BUILDS_DAYS * 86400e3);
  all.builds = [...all.builds.filter(b => b.at >= since), build];
  await env.REPORTS.put("stats", JSON.stringify(all));
  return json({ ok: true });
}

async function untranslated(request, env) {
  const { items } = await request.json();
  const fresh = [];
  for (const item of (items || []).slice(0, UNTRANSLATED_MAX)) {
    if (typeof item?.url !== "string") continue;
    const key = "u:" + (await sha1(item.url));
    if (!(await env.REPORTS.get(key))) fresh.push({ key, item });
  }
  if (!fresh.length) return json({ ok: true, new: 0 });
  const cut = value => String(value || "").slice(0, 300);
  const blocks = fresh.map(({ item }) => {
    const lines = [`${cut(item.source)}: ${cut(item.title)}`];
    if (item.summary) lines.push(`Подводка: ${cut(item.summary)}`);
    lines.push(`Модель: ${cut(item.attempt)}`, `Причина: ${cut(item.reason)}`, cut(item.url));
    return lines.join("\n");
  });
  const message = await notify(env, [`⚠️ Не перевелось: ${fresh.length}`, ...blocks].join("\n\n"));
  // не дошло до Telegram — не запоминаем: следующая сборка пришлёт эти новости ещё раз
  if (message || !env.TELEGRAM_TOKEN) {
    await Promise.all(fresh.map(({ key }) => env.REPORTS.put(key, "1", { expirationTtl: UNTRANSLATED_SECONDS })));
  }
  return json({ ok: true, new: fresh.length, sent: Boolean(message) });
}

// время по Белграду строкой «2026-11-03 20:21:06» — такие строки сравниваются как даты
function belgrade(ms) {
  return new Date(ms).toLocaleString("sv-SE", { timeZone: "Europe/Belgrade" });
}

async function findItem(url, dataUrl) {
  // data.json обновляется раз в 15 минут — кэшируем на 5, чтобы не тянуть его на каждую жалобу;
  // если новости в нём нет, она может быть свежее кэша — тогда перечитываем мимо кэша
  for (const fresh of [false, true]) {
    const response = await fetch(fresh ? `${dataUrl}?t=${Date.now()}` : dataUrl,
      fresh ? {} : { cf: { cacheTtl: 300, cacheEverything: true } });
    if (!response.ok) continue;
    const data = await response.json();
    for (const src of data.sources) {
      const item = src.items.find(i => i.url === url && i.ru);
      if (item) return { item, src };
    }
  }
  return null;
}

async function telegram(env, method, payload) {
  if (!env.TELEGRAM_TOKEN || !env.TELEGRAM_CHAT_ID) return null;
  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, ...payload }),
  });
  return response.ok ? (await response.json()).result : null;
}

async function notify(env, text) {
  const message = await telegram(env, "sendMessage", { text: text.slice(0, 4000), link_preview_options: { is_disabled: true } });
  return message?.message_id;
}

function react(env, messageId, emoji) {
  return telegram(env, "setMessageReaction", { message_id: messageId, reaction: [{ type: "emoji", emoji }] });
}

async function sha1(text) {
  const digest = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
