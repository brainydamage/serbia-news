// Приёмник жалоб «плохой перевод» для vestomat.rs.
// POST /report {url, comment} — от читателя: присылает жалобу в Telegram и хранит неделю.
// GET /reports — для сборки (с токеном): какие новости перевести заново моделью посильнее.
// Оригинал и перевод берём не из запроса, а из опубликованного data.json: так пожаловаться можно
// только на настоящую новость с сайта, а в Telegram не попадёт чужой текст под её видом.

const ORIGINS = ["https://vestomat.rs", "http://localhost:8765"];
const DATA_URL = "https://vestomat.rs/data.json";
const PER_IP_PER_HOUR = 10;
const KEEP_SECONDS = 7 * 24 * 3600;
const COMMENT_MAX = 500;

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
    if (pathname === "/reports" && request.method === "GET") return list(request, env);
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
  await env.REPORTS.put(limitKey, String(count + 1), { expirationTtl: 3600 });

  const found = await findItem(url, env.DATA_URL || DATA_URL);
  if (!found) return json({ error: "unknown item" }, 404);
  const { item, src } = found;

  const key = "r:" + (await sha1(url));
  const old = await env.REPORTS.get(key, "json");
  const comments = [...(old?.comments || []), ...(comment ? [comment] : [])].slice(-10);
  await env.REPORTS.put(key, JSON.stringify({ url, source: src.name, comments, first: old?.first || new Date().toISOString(), count: (old?.count || 0) + 1 }),
    { expirationTtl: KEEP_SECONDS });

  const lines = [`⚑ Плохой перевод${old ? ` (жалоба №${(old.count || 0) + 1})` : ""} — ${src.name}`, "",
    `Оригинал: ${item.title}`, `Перевод: ${item.ru.title}`];
  if (item.summary) lines.push("", `Подводка: ${item.summary}`, `Перевод: ${item.ru.summary}`);
  if (comment) lines.push("", `Комментарий: ${comment}`);
  lines.push("", url);
  ctx.waitUntil(notify(env, lines.join("\n")));
  return json({ ok: true });
}

async function list(request, env) {
  if (!env.REPORTS_TOKEN || request.headers.get("Authorization") !== `Bearer ${env.REPORTS_TOKEN}`) {
    return json({ error: "unauthorized" }, 401);
  }
  const reports = [];
  let cursor;
  do {
    const page = await env.REPORTS.list({ prefix: "r:", cursor });
    for (const { name } of page.keys) {
      const value = await env.REPORTS.get(name, "json");
      if (value) reports.push(value);
    }
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  return json({ reports });
}

async function findItem(url, dataUrl) {
  // data.json обновляется раз в 15 минут — кэшируем на 5, чтобы не тянуть его на каждую жалобу
  const response = await fetch(dataUrl, { cf: { cacheTtl: 300, cacheEverything: true } });
  if (!response.ok) return null;
  const data = await response.json();
  for (const src of data.sources) {
    const item = src.items.find(i => i.url === url && i.ru);
    if (item) return { item, src };
  }
  return null;
}

async function notify(env, text) {
  if (!env.TELEGRAM_TOKEN || !env.TELEGRAM_CHAT_ID) return;
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text: text.slice(0, 4000), link_preview_options: { is_disabled: true } }),
  });
}

async function sha1(text) {
  const digest = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
