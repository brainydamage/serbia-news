// Приёмник жалоб «плохой перевод» для vestomat.rs.
// POST /report {url, comment} — от читателя: присылает жалобу в Telegram и хранит неделю.
// GET /reports — для сборки (с токеном): какие новости перевести заново моделью посильнее.
// POST /reports {results: [{url, status}]} — от сборки: чем кончилось, отмечаем реакцией на сообщение в Telegram.
// POST /stats {kinds: {new|retry|review: {items, in, out, usd}}} — от сборки: сколько она потратила на перевод.
//   Копим в ключе KV "stats": итоги по дням навсегда и каждую сборку за последние две недели.
// POST /untranslated {items: [{url, source, title, summary, reason, attempt, attempt_summary}]} — от сборки: новости, которые модель
//   не перевела и с подсказкой (обычно мешает название — его стоит добавить в glossary.json). Присылаем в Telegram
//   одним сообщением, каждую новость один раз: сборка шлёт их снова, пока они в ленте.
// POST /feeds {feeds: [feed], problems: [{name, site, feed, kind: broken|silent, since, detail, gap_hours}],
//   resting: [feed], newest: {feed: дата}} — от сборки, каждую сборку: сторож лент. О ленте, которая не открывается
//   или молчит дольше суток, пишем в Telegram один раз, а когда тревога снята — отвечаем на то сообщение: «снова пишет»,
//   если вышла новость, или «отбой», если у издания выходной (resting). Кого уже прислали, лежит в ключе KV "feeds".
// POST /telegram — webhook бота: строки «Air Serbia = Air Serbia» или «удалить Air Serbia» из нашего чата
//   worker коммитит в glossary.json на GitHub (секрет GITHUB_TOKEN), а коммит запускает сборку.
//   Webhook worker подключает сам при первой сборке после выкатки (см. ensureWebhook).
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
const FEEDS_MAX = 15;  // новых тревог в одном сообщении
// сторож лент заметно отличается от «Не перевелось»: свой заголовок и значки
const FEEDS_TITLE = "📡 СТОРОЖ ЛЕНТ";
const REPO = "brainydamage/serbia-news";
const GLOSSARY_PATH = "glossary.json";
// поменять, если изменятся настройки webhook, — worker подключит его заново
const WEBHOOK_VERSION = "1";
const GLOSSARY_HELP = [
  "Чтобы добавить название в глоссарий, напиши строку «как в оригинале = как по-русски», можно несколько строк:",
  "Air Serbia = Air Serbia", "Republika Srpska = Республика Сербская", "",
  "Убрать: «удалить Air Serbia». Новости с этим названием переведутся заново в ближайшей сборке.",
].join("\n");

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
    if (pathname === "/telegram" && request.method === "POST") return telegramUpdate(request, env);
    if (pathname === "/costs" && request.method === "GET") {
      return new Response(COSTS_PAGE, { headers: { "Content-Type": "text/html; charset=utf-8", "X-Robots-Tag": "noindex" } });
    }
    if (pathname === "/stats" && request.method === "GET") {
      if (!authorized(request, env.STATS_KEY)) return json({ error: "unauthorized" }, 401);
      return json((await env.REPORTS.get("stats", "json")) || { days: {}, builds: [] });
    }
    if (["/reports", "/stats", "/untranslated", "/feeds"].includes(pathname) && !authorized(request, env.REPORTS_TOKEN)) return json({ error: "unauthorized" }, 401);
    if (pathname === "/reports" && request.method === "GET") return list(env);
    if (pathname === "/reports" && request.method === "POST") return resolve(request, env);
    if (pathname === "/stats" && request.method === "POST") {
      // сборка приходит сюда каждые 15 минут — заодно проверяем, что webhook бота подключён
      ctx.waitUntil(ensureWebhook(env, new URL(request.url).origin));
      return stats(request, env);
    }
    if (pathname === "/untranslated" && request.method === "POST") return untranslated(request, env);
    if (pathname === "/feeds" && request.method === "POST") return watchFeeds(request, env);
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
    lines.push(`Модель: ${cut(item.attempt)}`);
    if (item.attempt_summary) lines.push(`Модель, подводка: ${cut(item.attempt_summary)}`);
    lines.push(`Причина: ${cut(item.reason)}`, cut(item.url));
    return lines.join("\n");
  });
  const message = await notify(env, [`⚠️ Не перевелось: ${fresh.length}`, ...blocks].join("\n\n"));
  // не дошло до Telegram — не запоминаем: следующая сборка пришлёт эти новости ещё раз
  if (message || !env.TELEGRAM_TOKEN) {
    await Promise.all(fresh.map(({ key }) => env.REPORTS.put(key, "1", { expirationTtl: UNTRANSLATED_SECONDS })));
  }
  return json({ ok: true, new: fresh.length, sent: Boolean(message) });
}

async function watchFeeds(request, env) {
  const { feeds, problems, resting, newest } = await request.json();
  const restingSet = new Set(Array.isArray(resting) ? resting : []);
  const listed = new Set(Array.isArray(feeds) ? feeds : []);
  const now = (Array.isArray(problems) ? problems : []).filter(p => typeof p?.feed === "string");
  const known = (await env.REPORTS.get("feeds", "json")) || {};
  const cut = value => String(value || "").slice(0, 300);

  const fresh = now.filter(p => !(p.feed in known)).slice(0, FEEDS_MAX);
  if (fresh.length) {
    const blocks = fresh.map(p => (p.kind === "broken"
      ? [`🔴 ${cut(p.name)} не открывается ${since(p.since)}`, `Ошибка: ${cut(p.detail)}`, cut(p.feed)]
      : [`🔇 ${cut(p.name)} молчит ${since(p.since)}`, `Последняя новость: ${cut(p.detail)}`,
        `Обычно пишет раз в ${hours(p.gap_hours)}`, cut(p.site)]).join("\n"));
    const message = await notify(env, [FEEDS_TITLE, ...blocks].join("\n\n"));
    // не дошло до Telegram — не запоминаем: следующая сборка пришлёт эти ленты ещё раз
    if (message || !env.TELEGRAM_TOKEN) {
      for (const p of fresh) known[p.feed] = { name: cut(p.name), kind: p.kind, since: cut(p.since), message: message || null };
    }
  }

  // ожила — лента больше не в списке проблем. Если её убрали из sources.json, просто забываем
  const down = new Set(now.map(p => p.feed));
  for (const [feed, entry] of Object.entries(known)) {
    if (down.has(feed)) continue;
    if (listed.has(feed)) {
      const wrote = entry.since && newest?.[feed] && Date.parse(newest[feed]) > Date.parse(entry.since);
      const text = entry.kind === "broken" ? `✅ Сторож лент: ${entry.name} снова открывается`
        : wrote ? `✅ Сторож лент: ${entry.name} снова пишет`
        : restingSet.has(feed) ? `💤 Сторож лент: ${entry.name} — отбой: по выходным и праздникам не пишет, ждём рабочего дня`
        : `💤 Сторож лент: ${entry.name} — отбой`;
      const sent = await telegram(env, "sendMessage", {
        text, ...(entry.message ? { reply_parameters: { message_id: entry.message, allow_sending_without_reply: true } } : {}),
      });
      if (!sent && env.TELEGRAM_TOKEN) continue;
    }
    delete known[feed];
  }
  await env.REPORTS.put("feeds", JSON.stringify(known));
  return json({ ok: true, new: fresh.length, watching: Object.keys(known).length });
}

// «с 5 ноября, 16:45 (30 ч)» — время по Белграду
function since(iso) {
  const ms = Date.parse(iso);
  if (!ms) return "";
  const date = new Date(ms).toLocaleString("ru-RU", { timeZone: "Europe/Belgrade", day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
  return `с ${date} (${hours((Date.now() - ms) / 3600e3)})`;
}

// 30 → «30 ч», 75 → «3 дня»
function hours(value) {
  const h = Number(value) || 0;
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} мин`;
  if (h < 48) return `${Math.round(h)} ч`;
  const d = Math.round(h / 24);
  const last = d % 10, tens = d % 100;
  return `${d} ${last === 1 && tens !== 11 ? "день" : last >= 2 && last <= 4 && (tens < 12 || tens > 14) ? "дня" : "дней"}`;
}

// секретная метка webhook: Telegram ставит её в заголовок каждого запроса, так чужой не притворится Telegram.
// Выводим её из токена бота, чтобы не заводить ещё один секрет
async function webhookSecret(env) {
  return env.TELEGRAM_TOKEN ? sha1("webhook:" + env.TELEGRAM_TOKEN) : null;
}

async function ensureWebhook(env, origin) {
  if (!env.TELEGRAM_TOKEN || (await env.REPORTS.get("webhook")) === WEBHOOK_VERSION) return;
  const done = await telegram(env, "setWebhook", {
    url: `${origin}/telegram`, secret_token: await webhookSecret(env), allowed_updates: ["message"],
  });
  if (done) await env.REPORTS.put("webhook", WEBHOOK_VERSION);
}

// ответ Telegram всегда 200: на ошибку он стал бы присылать то же сообщение снова и снова
async function telegramUpdate(request, env) {
  const secret = await webhookSecret(env);
  if (!secret || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== secret) return json({ error: "unauthorized" }, 401);
  const message = (await request.json()).message;
  if (!message?.text || String(message.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return json({ ok: true });
  const reply = text => telegram(env, "sendMessage", { text, reply_parameters: { message_id: message.message_id } });

  const changes = [];
  for (const line of message.text.split("\n").map(l => l.trim()).filter(Boolean)) {
    const remove = line.match(/^удалить\s+(.+)$/i);
    const pair = line.match(/^(.+?)\s*(?:=|→)\s*(.+)$/);
    if (remove) changes.push({ sr: remove[1].trim() });
    else if (pair && pair[1].length <= 100 && pair[2].length <= 100) changes.push({ sr: pair[1].trim(), ru: pair[2].trim() });
    else {
      await reply(GLOSSARY_HELP);
      return json({ ok: true });
    }
  }
  try {
    await reply(await editGlossary(env, changes));
  } catch (error) {
    await reply(`Не получилось: ${error.message}`.slice(0, 1000));
  }
  return json({ ok: true });
}

// glossary.json — по записи на строку, как его пишем руками; дописываем в конец, существующую — правим на месте
async function editGlossary(env, changes) {
  if (!env.GITHUB_TOKEN) throw new Error("у worker нет секрета GITHUB_TOKEN");
  const api = `${env.GITHUB_API || "https://api.github.com"}/repos/${REPO}/contents/${GLOSSARY_PATH}`;
  const headers = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vestomat-bot", "Content-Type": "application/json",
  };
  // если файл успели поменять между чтением и записью, GitHub ответит 409 — читаем заново
  for (let attempt = 0; attempt < 2; attempt++) {
    const file = await fetch(`${api}?ref=main`, { headers });
    if (!file.ok) throw new Error(`GitHub не отдал ${GLOSSARY_PATH}: ${file.status}`);
    const { content, sha } = await file.json();
    const names = JSON.parse(fromBase64(content)).names;
    const done = [];
    for (const change of changes) {
      const at = names.findIndex(e => e.sr.toLowerCase() === change.sr.toLowerCase());
      if (!change.ru) {
        if (at < 0) done.push(`«${change.sr}» в глоссарии нет`);
        else done.push(`− ${names.splice(at, 1)[0].sr}`);
      } else if (at < 0) {
        names.push({ sr: change.sr, ru: change.ru });
        done.push(`+ ${change.sr} → ${change.ru}`);
      } else if (names[at].ru !== change.ru) {
        done.push(`✎ ${names[at].sr} → ${change.ru} (было: ${names[at].ru})`);
        names[at] = { ...names[at], ru: change.ru };
      } else done.push(`= ${change.sr} → ${change.ru} уже есть`);
    }
    const changed = done.filter(d => /^[+−✎]/.test(d));
    if (!changed.length) return done.join("\n");
    // все поля записи, а не только sr и ru: иначе пропадёт пояснение note
    const entry = e => `{${Object.entries(e).map(([k, v]) => `${JSON.stringify(k)}: ${JSON.stringify(v)}`).join(", ")}}`;
    const text = `{\n  "names": [\n${names.map(e => `    ${entry(e)}`).join(",\n")}\n  ]\n}\n`;
    const saved = await fetch(api, {
      method: "PUT", headers,
      body: JSON.stringify({ message: `Glossary from Telegram: ${changed.join(", ")}`.slice(0, 200), content: toBase64(text), sha, branch: "main" }),
    });
    if (saved.status === 409) continue;
    if (!saved.ok) throw new Error(`GitHub не сохранил ${GLOSSARY_PATH}: ${saved.status}`);
    return [...done, "", "Новости с этими названиями переведутся заново в ближайшей сборке, минуты через 3."].join("\n");
  }
  throw new Error("glossary.json всё время меняется, попробуй ещё раз");
}

function fromBase64(text) {
  return new TextDecoder().decode(Uint8Array.from(atob(text.replace(/\s/g, "")), c => c.charCodeAt(0)));
}

function toBase64(text) {
  let binary = "";
  for (const byte of new TextEncoder().encode(text)) binary += String.fromCharCode(byte);
  return btoa(binary);
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
  const response = await fetch(`${env.TELEGRAM_API || "https://api.telegram.org"}/bot${env.TELEGRAM_TOKEN}/${method}`, {
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
