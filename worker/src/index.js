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
// POST /daily {day, rich, text, again, draft} — от сборки (daily.py): сводка за сутки в канал DAILY_CHAT_ID (@vestomat_daily; бот —
//   админ канала с правом публикации). rich — rich message (sendRichMessage), text — простой HTML на случай, если rich не
//   прошёл. Пока DAILY_CHAT_ID не задан — черновиком в чат жалоб. Одну сводку на день и чат отправляем один раз:
//   сборка может прислать её снова, если её кэш пропал.
// POST /live {id, text, theme, fact} — от сборки (live.py): срочное молнией в канал LIVE_CHAT_ID (@vestomat_live), а пока
//   он не задан — черновиком в чат жалоб. Под черновиком (или под копией поста в чате жалоб) кнопки «✓ срочно» / «✗ могло
//   подождать» (live:ok:id, live:no:id); ответ на пост или на «почему?» одной фразой — причина. POST /live {rejected, day} —
//   раз в день список тем, которые модель сочла несрочными, с кнопкой «надо было» (live:miss:id) у каждой.
//   GET /live — для сборки: отмеченные решения с причинами, прецеденты для модели. Всё — в одном ключе KV "live".
// POST /telegram — webhook бота: строки «Air Serbia = Air Serbia» или «удалить Air Serbia» из нашего чата
//   worker коммитит в glossary.json на GitHub (секрет GITHUB_TOKEN), а коммит запускает сборку.
//   Команды сюжетам (теневой режим, vestomat.rs/plot/) — «/move 1234 s471», «/split s191», «/unmerge s102», «/merge s1 s2» —
//   так же дописывает в stories-commands.json; сборка их применяет (stories_live.py).
// POST /stories {results: [{message, text, buttons}]} — от сборки: чем кончились команды сюжетам, ответом на сообщение
//   с командой; отчёт проверки сюжетов — с кнопками (buttons — ряды {text, data}). Нажатая кнопка «fix:день:находка»
//   становится командой {do: "fix"} в stories-commands.json, как набранная. Кнопки отчёта за день лежат в KV
//   ("fix:день"): по «убрать все уверенные» снимаем кнопки уверенных находок («fix:день:находка:s») со всех его сообщений.
//   Webhook worker подключает сам при первой сборке после выкатки (см. ensureWebhook).
// POST /texts {id, system, system_sr, items: {url: {ru, sr, text}}} — от сборки (essences.py): вопросы модели для «сути одной фразой» — текст
//   статьи с правилами и глоссарием, пачка одной сборки в ключе KV "tx:id" (одна запись на сборку: у KV на бесплатном
//   тарифе ~1000 записей в день). Тексты статей лежат только здесь и на сайт не попадают.
// POST /essence {url, lang} — от читателя (lang: ru — /ru/, sr — сербская страница), когда он раскрыл новость: суть одной фразой (Haiku). Новость берём из data.json
//   (у неё должен быть номер пачки "tx"), фразу кэшируем (ESSENCE_CACHE + адрес). Расходы — в ключе "essence-stats" по дням;
//   за дневным лимитом (ESSENCE_USD_PER_DAY) новые фразы не пишем, кэш отдаём.
// GET /costs — страница с этой статистикой, GET /stats — сами данные для неё (с ключом STATS_KEY).
//   Ключ страница берёт из адреса после # — эта часть не уходит ни на сервер, ни в Referer.
// Оригинал и перевод берём не из запроса, а из опубликованного data.json: так пожаловаться можно
// только на настоящую новость с сайта, а в Telegram не попадёт чужой текст под её видом.

import Anthropic from "@anthropic-ai/sdk";
import COSTS_PAGE from "./costs.html";

const ORIGINS = ["https://vestomat.rs", "http://localhost:8765"];
const DATA_URL = "https://vestomat.rs/data.json";
const PER_IP_PER_HOUR = 10;
const KEEP_SECONDS = 7 * 24 * 3600;
const REPORTS_OPEN = "reports-open";  // ключи r:… ещё не обработанных жалоб на перевод, см. openReports
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
// суть одной фразой: Haiku 5.5 — проба 25.11 на 30 статьях: медиана 157 знаков, ~$0.0006 и ~1,5 с на фразу
const ESSENCE_MODEL = "claude-haiku-5-5";
const REWRITE_MODEL = "claude-sonnet-5-5";  // переписывает описание, на которое пожаловались
const ESSENCE_PRICES = { [ESSENCE_MODEL]: [0.10, 0.50], [REWRITE_MODEL]: [2.0, 10.0] };  // $ за миллион токенов: вход, выход
const ESSENCE_USD_PER_DAY = 0.30;  // ~500 фраз; дальше до полуночи по Белграду — только из кэша
const ESSENCE_PER_IP_PER_HOUR = 60;
const ESSENCE_LIMIT = 200;  // знаков; длиннее — просим сократить, потом режем по «;»
const ESSENCE_KEEP_SECONDS = 7 * 24 * 3600;
const TEXTS_KEEP_SECONDS = 6 * 24 * 3600;  // сборка помнит тексты 5 дней (essences.KEEP_DAYS)
// напоминание после текста статьи: без него Haiku на длинной статье Mašina 4 раза из 6 отвечал обрывком
// «Высшая прокуратура» (25.11); с ним — 8 из 8 фраз и 30 из 30 на пробе
const ESSENCE_ASK = {
  ru: "\n\nНапиши суть этой статьи одной фразой: 12–25 слов, полное предложение — кто что сделал, заявил или что произошло.",
  sr: "\n\nNapiši suštinu ovog članka jednom rečenicom: 12–22 reči, cela rečenica — ko je šta uradio, izjavio ili šta se dogodilo.",
};
const ESSENCE_NO = { ru: "НЕТ", sr: "NE" };  // ответ модели «текст не про то»
const ESSENCE_MIN_WORDS = 8;  // короче — не фраза, а обрывок: спрашиваем ещё раз
const ESSENCE_CACHE = "e2:";  // меняется вместе с правилами — старые фразы пишутся заново
const ESSENCE_SCHEMA = { type: "object", additionalProperties: false, required: ["essence"], properties: { essence: { type: "string" } } };
const ESSENCE_FIX = {};
ESSENCE_FIX.ru = "Ты редактор. Перепиши фразу так, чтобы она стала одним предложением по-русски не длиннее 170 знаков " +
  "(это жёсткий предел: если фраза длиннее, обязательно выбрось одну из частей или подробностей), кириллицей (кроме " +
  "общепринятых латинских аббревиатур и брендов). Сохрани главный факт, выбрось второстепенное. Ничего не добавляй от себя.";
ESSENCE_FIX.sr = "Ti si urednik. Prepiši rečenicu tako da bude jedna rečenica na srpskom, latinicom, ne duža od 170 znakova " +
  "(to je čvrsta granica: ako je duža, obavezno izbaci jedan deo ili detalj). Zadrži glavnu činjenicu, izbaci sporedno. Ništa ne dodaj od sebe.";
const REPO = "brainydamage/serbia-news";
const GLOSSARY_PATH = "glossary.json";
const COMMANDS_PATH = "stories-commands.json";
const COMMANDS_KEEP = 200;  // последних команд в файле: сборка помнит, до какой дошла
// поменять, если изменятся настройки webhook, — worker подключит его заново
const WEBHOOK_VERSION = "2";  // 2 — и нажатия кнопок (callback_query)
const LIVE_KEPT = 120;  // записей в ключе "live": постов и несрочных тем, отмеченных и нет
const LIVE_VERDICTS = { ok: "✓ отмечено: срочно", no: "✗ отмечено: могло подождать", miss: "отмечено: надо было" };
const GLOSSARY_HELP = [
  "Чтобы добавить название в глоссарий, напиши строку «как в оригинале = как по-русски», можно несколько строк:",
  "Air Serbia = Air Serbia", "Republika Srpska = Республика Сербская", "",
  "Убрать: «удалить Air Serbia». Новости с этим названием переведутся заново в ближайшей сборке.",
  "", "Сюжеты (vestomat.rs/plot/), по команде на строку:",
  "/move 1234 s471 — новость №1234 в сюжет s471 (new вместо s471 — в новый сюжет)",
  "/move 1234 1240 1302 new — несколько новостей вместе в один новый сюжет (или в s471)",
  "/split s191 s298 — части s191 и s298 в отдельный сюжет",
  "/unmerge s102 — отменить склейку: s102 снова отдельный сюжет",
  "/merge s12970 s13840 — склеить: тема s12970 целиком в s13840 (отменить — /unmerge s12970)",
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
    if ((pathname === "/report" || pathname === "/essence") && request.method === "POST") {
      const response = await (pathname === "/report" ? report : essence)(request, env, ctx);
      for (const [k, v] of Object.entries(cors)) response.headers.set(k, v);
      return response;
    }
    if (pathname === "/telegram" && request.method === "POST") return telegramUpdate(request, env);
    if (pathname === "/costs" && request.method === "GET") {
      return new Response(COSTS_PAGE, { headers: { "Content-Type": "text/html; charset=utf-8", "X-Robots-Tag": "noindex" } });
    }
    if (pathname === "/stats" && request.method === "GET") {
      if (!authorized(request, env.STATS_KEY)) return json({ error: "unauthorized" }, 401);
      const [all, essences] = await Promise.all([env.REPORTS.get("stats", "json"), env.REPORTS.get("essence-stats", "json")]);
      const out = all || { days: {}, builds: [] };
      // фразы пишет не сборка, а worker — свой ключ, чтобы не перезаписывать "stats" наперегонки со сборкой
      for (const [day, spent] of Object.entries(essences?.days || {})) (out.days[day] ||= { builds: 0 }).essence = spent;
      return json(out);
    }
    if (["/reports", "/stats", "/untranslated", "/feeds", "/stories", "/texts", "/daily", "/live"].includes(pathname) && !authorized(request, env.REPORTS_TOKEN)) return json({ error: "unauthorized" }, 401);
    if (pathname === "/reports" && request.method === "GET") return list(env);
    if (pathname === "/reports" && request.method === "POST") return resolve(request, env);
    if (pathname === "/stats" && request.method === "POST") {
      // сборка приходит сюда каждые 15 минут — заодно проверяем, что webhook бота подключён
      ctx.waitUntil(ensureWebhook(env, new URL(request.url).origin));
      return stats(request, env);
    }
    if (pathname === "/untranslated" && request.method === "POST") return untranslated(request, env);
    if (pathname === "/feeds" && request.method === "POST") return watchFeeds(request, env);
    if (pathname === "/stories" && request.method === "POST") return storyResults(request, env);
    if (pathname === "/texts" && request.method === "POST") return saveTexts(request, env);
    if (pathname === "/daily" && request.method === "POST") return daily(request, env);
    if (pathname === "/live" && request.method === "GET") {
      const live = (await env.REPORTS.get("live", "json")) || { items: [] };
      return json({ feedback: live.items.filter(i => i.verdict) });
    }
    if (pathname === "/live" && request.method === "POST") return livePost(request, env);
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
  // жалоба на описание: ответ ждёт, пока Sonnet перепишет (несколько секунд), — читатель сразу видит новое
  if (body.kind === "essence") return json({ ok: true, text: await rewriteEssence(env, ctx, url, body.lang === "sr" ? "sr" : "ru", found) });
  ctx.waitUntil(save(env, url, comment, found));
  return json({ ok: true });
}

// «Плохое описание?»: описание Haiku переписывает Sonnet, с пометкой, какой вариант забраковали, и новое сразу видят все.
// Пожаловались и на переписанное — прячем (в кэше null: читатели видят подводку издания). За дневным лимитом расходов
// тоже прячем. В Telegram — что было и что стало: по жалобам видно, что не так с вопросом или правилами
async function rewriteEssence(env, ctx, url, lang, { item, src }) {
  const key = await essenceKey(url, lang);
  const cached = (await env.REPORTS.get(key, "json")) || {};
  const old = cached.text || null;
  const day = belgrade(Date.now()).slice(0, 10);
  const spent = (await env.REPORTS.get("essence-stats", "json"))?.days?.[day]?.usd || 0;
  let text = null, why = "текста статьи нет — прячем";
  if (cached.by === REWRITE_MODEL) why = "уже переписано Sonnet — прячем";
  else if (!old) why = "описания не было — нечего переписывать";
  else if (spent >= ESSENCE_USD_PER_DAY) why = "дневной лимит расходов — прячем";
  else if (item.tx) {
    const usage = { usd: 0 };
    const extra = lang === "sr"
      ? `\n\nČitalac je kao loš označio ovaj opis: „${old}“. Napiši novi, tačniji, isključivo prema tekstu članka.`
      : `\n\nЧитатель отметил как плохое это описание: «${old}». Напиши новое, точнее, строго по тексту статьи.`;
    try {
      text = (await writeEssence(env, url, lang, item, REWRITE_MODEL, extra, usage)) || null;
    } catch (error) {
      console.log("rewrite:", error?.status, error?.message);
    }
    ctx.waitUntil(addEssenceStats(env, day, usage));
    if (!text) why = "Sonnet не справился — прячем";
  }
  await env.REPORTS.put(key, JSON.stringify({ text, by: REWRITE_MODEL, reported: old || cached.reported || null }), { expirationTtl: ESSENCE_KEEP_SECONDS });
  const lines = [`⚑ Плохое описание от ИИ (${lang === "sr" ? "по-сербски" : "по-русски"}) — ${src.name}`, "",
    `Заголовок: ${lang === "sr" ? item.title : item.ru?.title || item.title}`, `Было: ${old || cached.reported || "(нет)"}`,
    text ? `Стало (Sonnet): ${text}` : `Итог: ${why}`];
  const lead = lang === "sr" ? item.summary : item.ru?.summary;
  if (lead) lines.push(`Подводка: ${lead}`);
  lines.push("", url);
  ctx.waitUntil(notify(env, lines.join("\n")));
  return text;
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
  const open = await openReports(env);
  if (!open.includes(key)) await env.REPORTS.put(REPORTS_OPEN, JSON.stringify([...open, key]));
}

async function saveTexts(request, env) {
  const { id, system, system_sr, items } = await request.json();
  if (!/^\d{12}$/.test(id || "") || typeof system !== "string" || !items || typeof items !== "object") return json({ error: "bad batch" }, 400);
  await env.REPORTS.put(`tx:${id}`, JSON.stringify({ system, system_sr, items }), { expirationTtl: TEXTS_KEEP_SECONDS });
  return json({ ok: true, items: Object.keys(items).length });
}

async function essence(request, env, ctx) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "bad json" }, 400);
  }
  const url = typeof body.url === "string" ? body.url.slice(0, 2000) : "";
  if (!url) return json({ error: "no url" }, 400);
  const lang = body.lang === "sr" ? "sr" : "ru";
  const key = await essenceKey(url, lang);
  const cached = await env.REPORTS.get(key, "json");
  if (cached) return json({ text: cached.text });

  // текст статьи сборка отдаёт вместе с data.json, где у новости появился "tx": закэшированный data.json мог быть старше
  const found = await findItem(url, env.DATA_URL || DATA_URL, i => i.tx);
  if (!found?.item.tx) return json({ error: "unknown item" }, 404);
  const day = belgrade(Date.now()).slice(0, 10);
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const limitKey = `eip:${ip}:${Math.floor(Date.now() / 3600e3)}`;
  const [count, spent] = await Promise.all([env.REPORTS.get(limitKey).then(Number), env.REPORTS.get("essence-stats", "json")]);
  if (count >= ESSENCE_PER_IP_PER_HOUR) return json({ error: "too many" }, 429);
  if ((spent?.days?.[day]?.usd || 0) >= ESSENCE_USD_PER_DAY) return json({ error: "daily limit" }, 429);
  const usage = { usd: 0 };
  let answer;
  try {
    answer = await writeEssence(env, url, lang, found.item, ESSENCE_MODEL, "", usage);
  } catch (error) {
    console.log("essence:", error?.status, error?.message);
    return json({ error: "model" }, 502);
  } finally {
    ctx.waitUntil(addEssenceStats(env, day, usage));
    ctx.waitUntil(env.REPORTS.put(limitKey, String(count + 1), { expirationTtl: 3600 }));
  }
  if (answer === undefined) return json({ error: "no text" }, 404);
  // «НЕТ»/«NE» (текст не про то) и негодное тоже кэшируем — чтобы не платить за тот же отказ снова
  ctx.waitUntil(env.REPORTS.put(key, JSON.stringify({ text: answer }), { expirationTtl: ESSENCE_KEEP_SECONDS }));
  return json({ text: answer });
}

const essenceKey = async (url, lang) => ESSENCE_CACHE + (lang === "sr" ? "sr:" : "") + (await sha1(url));

// описание по тексту статьи из пачки сборки: фраза, null (модель отказалась или вышло негодное) или undefined (текста нет).
// extra — дописка к вопросу (при переписывании: какой вариант забраковали); usage.usd копит цену всех вызовов
async function writeEssence(env, url, lang, item, model, extra, usage) {
  const batch = await env.REPORTS.get(`tx:${item.tx}`, { type: "json", cacheTtl: 3600 });
  // пачки до 25.11 — строка, только русский вопрос; дальше — шапки по языкам и один текст
  const entry = batch?.items?.[url];
  const question = typeof entry === "string" ? (lang === "ru" ? entry : null) : entry?.[lang] && entry[lang] + entry.text;
  const system = lang === "sr" ? batch?.system_sr : batch?.system;
  if (!question || !system) return undefined;
  const no = ESSENCE_NO[lang];
  const fixLetters = lang === "sr" ? toLatin : cyrFix;
  const price = ESSENCE_PRICES[model];

  const client = new Anthropic({ apiKey: env.ANTHROPIC_API_KEY, baseURL: env.ANTHROPIC_API || undefined, maxRetries: 1, timeout: 20000 });
  const ask = async (system, content) => {
    const response = await client.messages.create({
      model, max_tokens: 4000, system, messages: [{ role: "user", content }],
      output_config: { effort: "low", format: { type: "json_schema", schema: ESSENCE_SCHEMA } },
    });
    usage.usd += (response.usage.input_tokens * price[0] + response.usage.output_tokens * price[1]) / 1e6;
    usage.in = (usage.in || 0) + response.usage.input_tokens;
    usage.out = (usage.out || 0) + response.usage.output_tokens;
    const text = response.content.filter(b => b.type === "text").map(b => b.text).join("");
    try {
      return fixLetters(String(JSON.parse(text).essence || "").trim().replace(/\.+$/, ""));
    } catch {
      return "";
    }
  };
  let text = await ask(system, question + ESSENCE_ASK[lang] + extra);
  if (text && text !== no && words(text) < ESSENCE_MIN_WORDS) text = await ask(system, question + ESSENCE_ASK[lang] + extra);
  const problems = text && text !== no ? essenceProblems(text, question, lang) : [];
  if (problems.length) {
    const fixed = await ask(ESSENCE_FIX[lang], lang === "sr"
      ? `Naslov: ${item.title}\nPrimedbe: ${problems.join(", ")}\nSada ${text.length} znakova, treba najviše 170.\nRečenica: ${text}`
      : `Заголовок: ${item.ru.title}\nЗамечания: ${problems.join(", ")}\nСейчас ${text.length} знаков, нужно не больше 170.\nФраза: ${text}`);
    if (fixed && fixed !== no) text = fixed;
  }
  text = cutClauses(text);
  return text && text !== no && words(text) >= ESSENCE_MIN_WORDS && text.length <= ESSENCE_LIMIT + 40 && !essenceProblems(text, question, lang).some(p => !p.startsWith("длина")) ? text : null;
}

async function addEssenceStats(env, day, usage) {
  if (!usage.in) return;
  // читаем и перезаписываем один ключ: два клика в одну и ту же миллисекунду потеряют одну строку счёта — не страшно
  const all = (await env.REPORTS.get("essence-stats", "json")) || { days: {} };
  const row = (all.days[day] ||= { items: 0, in: 0, out: 0, usd: 0 });
  row.items++;
  row.in += usage.in;
  row.out += usage.out;
  row.usd = Math.round((row.usd + usage.usd) * 1e6) / 1e6;
  await env.REPORTS.put("essence-stats", JSON.stringify(all));
}

// латинские буквы внутри русских слов и сербские ђ/ћ/ј/љ/њ/џ: Haiku пишет «Зdравкович», «Марija», «Стаматовић» —
// правим по таблице, без модели (модель в правке сама вставляла «ј»)
const LATIN_TO_RU = { a: "а", b: "б", c: "ц", č: "ч", ć: "ч", d: "д", đ: "дж", e: "е", f: "ф", g: "г", h: "х", i: "и", j: "ј",
  k: "к", l: "л", m: "м", n: "н", o: "о", p: "п", r: "р", s: "с", š: "ш", t: "т", u: "у", v: "в", z: "з", ž: "ж", y: "ы",
  x: "кс", w: "в", q: "к" };
const SERBIAN_TO_RU = [["ија", "ия"], ["ију", "ию"], ["ије", "ии"], ["ја", "я"], ["ју", "ю"], ["је", "е"], ["ј", "й"],
  ["ња", "ня"], ["ње", "не"], ["њу", "ню"], ["ља", "ля"], ["ље", "ле"], ["љу", "лю"],
  ["ђ", "дж"], ["ћ", "ч"], ["љ", "ль"], ["њ", "нь"], ["џ", "дж"]];
const keepCase = (src, dst) => src[0] && src[0] !== src[0].toLowerCase() ? dst[0].toUpperCase() + dst.slice(1) : dst;

function cyrFix(text) {
  return text.replace(/[\p{L}\p{N}_]+/gu, word => {
    if (!/[а-яёђћјљњџ]/i.test(word)) return word;
    if (/[a-zčćžšđ]/i.test(word)) word = [...word].map(ch => LATIN_TO_RU[ch.toLowerCase()] ? keepCase(ch, LATIN_TO_RU[ch.toLowerCase()]) : ch).join("");
    if (/[ђћјљњџ]/i.test(word)) {
      let low = word.toLowerCase();
      for (const [a, b] of SERBIAN_TO_RU) low = low.split(a).join(b);
      word = keepCase(word, low);
    }
    return word;
  });
}

// сербская суть — латиницей (на странице её переводят в кириллицу, если выбрана кириллица); Haiku на статье
// в кириллице иногда отвечает кириллицей — переводим по таблице
const SR_LATIN = { а: "a", б: "b", в: "v", г: "g", д: "d", ђ: "đ", е: "e", ж: "ž", з: "z", и: "i", ј: "j", к: "k", л: "l", љ: "lj",
  м: "m", н: "n", њ: "nj", о: "o", п: "p", р: "r", с: "s", т: "t", ћ: "ć", у: "u", ф: "f", х: "h", ц: "c", ч: "č", џ: "dž", ш: "š" };
const toLatin = text => text.replace(/[а-шђјљњћџ]/gi, ch => {
  const lat = SR_LATIN[ch.toLowerCase()];
  return !lat ? ch : ch === ch.toLowerCase() ? lat : lat[0].toUpperCase() + lat.slice(1);
});

const words = text => (text.match(/[\p{L}\p{N}]+/gu) || []).length;

function essenceProblems(text, question, lang = "ru") {
  const problems = [];
  if (text.length > ESSENCE_LIMIT) problems.push(`длина ${text.length}`);
  // по-сербски буквы уже переведены в латиницу (toLatin); русские слова остаются узнаваемыми по ы, э, щ, ъ
  if (lang === "sr") {
    if (/[ыэщъё]/i.test(text)) problems.push("nije na srpskom");
    return problems;
  }
  // обычные слова латиницей, которых нет в оригинале: «following», «near» (названия пишутся с заглавной — не в счёт)
  const source = new Set(question.toLowerCase().match(/[a-zčćžšđ]+/g) || []);
  const latin = [...new Set(text.match(/(?<![\p{L}])[a-z]{2,}(?![\p{L}])/gu) || [])].filter(w => !source.has(w));
  if (latin.length) problems.push("латиница: " + latin.slice(0, 3).join(", "));
  if (/[čćžšđ]/i.test(text) && !/[а-яё]/i.test(text)) problems.push("не по-русски");
  return problems;
}

// всё ещё длинно — отрезаем последние части после «;»: Haiku пишет «главное; подробность»
function cutClauses(text) {
  if (!text || text.length <= ESSENCE_LIMIT || !text.includes("; ")) return text;
  const parts = text.split("; ");
  while (parts.length > 1 && parts.join("; ").length > ESSENCE_LIMIT) parts.pop();
  return parts.join("; ");
}

function authorized(request, secret) {
  return secret && request.headers.get("Authorization") === `Bearer ${secret}`;
}

// Жалобы, ещё не обработанные сборкой, — ключи r:… в одном ключе "reports-open". Раньше сборка каждые 15 минут
// перебирала ключи (list) — это операция из того же дневного лимита, что и запись (1000 на бесплатном тарифе, 25.11).
// Ключа ещё нет (первый запуск) — один раз собираем его перебором
async function openReports(env) {
  const open = await env.REPORTS.get(REPORTS_OPEN, "json");
  if (open) return open;
  const keys = [];
  let cursor;
  do {
    const page = await env.REPORTS.list({ prefix: "r:", cursor });
    keys.push(...page.keys.map(k => k.name));
    cursor = page.list_complete ? null : page.cursor;
  } while (cursor);
  await env.REPORTS.put(REPORTS_OPEN, JSON.stringify(keys));
  return keys;
}

async function list(env) {
  const open = await openReports(env);
  const reports = [], still = [];
  for (const name of open) {
    const value = await env.REPORTS.get(name, "json");
    if (value && !value.status) {
      reports.push(value);
      still.push(name);
    }
  }
  // обработанные и истёкшие (жалоба живёт неделю) — из списка вон
  if (still.length !== open.length) await env.REPORTS.put(REPORTS_OPEN, JSON.stringify(still));
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

async function daily(request, env) {
  const { day, text, rich, again, draft: wantDraft } = await request.json();
  if (!/^\d{4}-\d\d-\d\d$/.test(String(day)) || typeof text !== "string" || !text) return json({ error: "bad request" }, 400);
  // draft — ручной запуск сборки (daily_day) без отметки «в канал»: черновик в чат жалоб, посмотреть оформление
  const channel = wantDraft ? "" : env.DAILY_CHAT_ID || "";
  const to = channel || env.TELEGRAM_CHAT_ID;
  const where = channel || "draft";
  const key = `daily:${to}:${day}`;
  const posted = await env.REPORTS.get(key);
  // again — ручной запуск: черновик шлём заново, а в канале правим уже опубликованный пост, а не шлём второй
  if (posted && !again) return json({ ok: true, already: true, to: where });
  if (!env.TELEGRAM_TOKEN || !to) return json({ error: "no telegram" }, 503);
  const edit = Boolean(channel && posted && Number(posted) > 1);
  const api = `${env.TELEGRAM_API || "https://api.telegram.org"}/bot${env.TELEGRAM_TOKEN}`;
  const call = (method, payload) => fetch(`${api}/${method}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: to, ...(edit ? { message_id: Number(posted) } : {}), ...payload }),
  });
  const note = channel ? "" : "<p><i>Черновик Vestomat Daily</i></p><hr/>";
  // текст не обрезаем: лимит Telegram — на видимые знаки, а обрезка по разметке рвёт теги (26.11 так не ушла первая
  // сводка — ссылки длинные). Длину держит сборка (daily.LIMIT)
  // rich message (Bot API 10.1+: заголовки, таблицы, до 32768 знаков); не прошёл — простой HTML, чтобы сводка всё равно вышла
  let kind = "rich", richError = "", response = null;
  if (typeof rich === "string" && rich) {
    response = await call(edit ? "editMessageText" : "sendRichMessage", { rich_message: { html: note + rich } });
    if (!response.ok) richError = `${response.status}: ${(await response.text()).slice(0, 200)}`;
  }
  if (!response?.ok) {
    kind = "plain";
    const plainNote = channel ? "" : "Черновик Vestomat Daily\n\n";
    response = await call(edit ? "editMessageText" : "sendMessage",
      { text: plainNote + text, parse_mode: "HTML", link_preview_options: { is_disabled: true } });
    if (!response.ok) return json({ error: `telegram ${response.status}: ${(await response.text()).slice(0, 300)}`, rich_error: richError }, 502);
  }
  const id = (await response.json()).result?.message_id;
  if (!edit) await env.REPORTS.put(key, String(id || 1), { expirationTtl: KEEP_SECONDS });
  return json({ ok: true, to: where, kind, ...(edit ? { edited: true } : {}), ...(richError ? { rich_error: richError } : {}) });
}

// срочное от сборки (live.py) — или список несрочного за день
async function livePost(request, env) {
  const body = await request.json();
  const live = (await env.REPORTS.get("live", "json")) || { items: [] };
  const save = () => {
    live.items = live.items.slice(-LIVE_KEPT);
    return env.REPORTS.put("live", JSON.stringify(live));
  };
  if (Array.isArray(body.rejected)) {
    const rows = body.rejected.filter(r => r?.id && r?.name).slice(0, 15);
    if (!rows.length) return json({ ok: true, to: "draft" });
    const text = [`🗂 Live: несрочное с прошлого списка (${rows.length})`, "",
      ...rows.map((r, k) => `${k + 1}. ${r.time || ""} ${r.name}${r.reason ? ` — ${r.reason}` : ""}`.trim()), "",
      "Если что-то из этого надо было прислать сразу — нажми кнопку с его номером."].join("\n");
    const keyboard = rows.map((r, k) => [{ text: `Надо было: ${k + 1}. ${r.name}`.slice(0, 60), callback_data: `live:miss:${r.id}`.slice(0, 64) }]);
    const sent = await telegram(env, "sendMessage", { text: text.slice(0, 4000), reply_markup: { inline_keyboard: keyboard } });
    if (!sent) return json({ error: "telegram" }, 502);
    for (const r of rows) live.items.push({ id: r.id, kind: "rejected", msg: sent.message_id, text: r.name, why: r.reason || "", t: belgrade(Date.now()) });
    await save();
    return json({ ok: true, to: "draft" });
  }
  const { id, text, theme, fact } = body;
  if (!id || typeof text !== "string" || !text) return json({ error: "bad request" }, 400);
  if (live.items.some(i => i.id === id && i.kind === "post")) return json({ ok: true, already: true });
  const channel = env.LIVE_CHAT_ID || "";
  const buttons = { inline_keyboard: [[{ text: "✓ срочно", callback_data: `live:ok:${id}` }, { text: "✗ могло подождать", callback_data: `live:no:${id}` }]] };
  const base = { parse_mode: "HTML", link_preview_options: { is_disabled: true } };
  if (channel) {
    const posted = await telegram(env, "sendMessage", { chat_id: channel, text, ...base });
    if (!posted) return json({ error: "telegram channel" }, 502);
  }
  const head = channel ? `Отправлено в ${channel}` : "Черновик Vestomat Live";
  const copy = await telegram(env, "sendMessage", { text: `<i>${head}</i>\n\n${text}`, reply_markup: buttons, ...base });
  if (!copy && !channel) return json({ error: "telegram" }, 502);
  live.items.push({ id, kind: "post", msg: copy?.message_id, text: String(fact || ""), theme: String(theme || ""), t: belgrade(Date.now()) });
  await save();
  return json({ ok: true, to: channel || "draft" });
}

// кнопка под постом Live или под списком несрочного: отметка — прецедент для модели. Нажатую кнопку меняем на «отмечено»
// (под списком — убираем), и спрашиваем почему: ответ на этот вопрос — причина
async function liveButton(env, query) {
  const answer = text => telegram(env, "answerCallbackQuery", { callback_query_id: query.id, text });
  const message = query.message;
  if (!message || String(message.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return answer("Эта кнопка не отсюда");
  const m = String(query.data).match(/^live:(ok|no|miss):(.+)$/);
  if (!m) return answer("Уже отмечено");
  const live = (await env.REPORTS.get("live", "json")) || { items: [] };
  const item = live.items.findLast(i => i.id === m[2] && i.kind === (m[1] === "miss" ? "rejected" : "post"));
  if (!item) return answer("Не нашла этот пост — он старше недели?");
  item.verdict = m[1];
  const keyboard = message.reply_markup?.inline_keyboard || [];
  const rows = m[1] === "miss" ? keyboard.filter(row => !row.some(b => b.callback_data === query.data))
    : [[{ text: LIVE_VERDICTS[m[1]], callback_data: "live:done" }]];
  await telegram(env, "editMessageReplyMarkup", { message_id: message.message_id, reply_markup: { inline_keyboard: rows } });
  const ask = await telegram(env, "sendMessage", {
    text: `Записала${m[1] === "miss" ? `: «${item.text}» — надо было` : ""}. Почему? Ответь на это сообщение одной фразой — модель учтёт. Можно не отвечать.`,
    reply_parameters: { message_id: message.message_id },
  });
  if (ask) item.prompt = ask.message_id;
  await env.REPORTS.put("live", JSON.stringify(live));
  await answer("Записала");
}

// ответ на пост Live или на вопрос «почему?» — причина отметки. Не про Live — false: дальше глоссарий и команды
async function liveReason(env, message) {
  const to = message.reply_to_message.message_id;
  const live = (await env.REPORTS.get("live", "json")) || { items: [] };
  const item = live.items.findLast(i => i.prompt === to || (i.kind === "post" && i.msg === to));
  if (!item) return false;
  item.reason = message.text.trim().slice(0, 300);
  await env.REPORTS.put("live", JSON.stringify(live));
  await react(env, message.message_id, "👌");
  if (!item.verdict) {
    await telegram(env, "sendMessage", { text: "Причину записала. Нажми ещё кнопку под постом — срочно или могло подождать, без неё модель отметку не увидит.", reply_parameters: { message_id: message.message_id } });
  }
  return true;
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
  const before = JSON.stringify(known);
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
  // пишем, только если что-то изменилось: на бесплатном тарифе KV 1000 записей в день, а сборка приходит 96 раз (25.11)
  if (JSON.stringify(known) !== before) await env.REPORTS.put("feeds", JSON.stringify(known));
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
    url: `${origin}/telegram`, secret_token: await webhookSecret(env), allowed_updates: ["message", "callback_query"],
  });
  if (done) await env.REPORTS.put("webhook", WEBHOOK_VERSION);
}

// ответ Telegram всегда 200: на ошибку он стал бы присылать то же сообщение снова и снова
async function telegramUpdate(request, env) {
  const secret = await webhookSecret(env);
  if (!secret || request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== secret) return json({ error: "unauthorized" }, 401);
  const update = await request.json();
  if (update.callback_query) {
    if (String(update.callback_query.data || "").startsWith("live:")) await liveButton(env, update.callback_query);
    else await storyButton(env, update.callback_query);
    return json({ ok: true });
  }
  const message = update.message;
  if (!message?.text || String(message.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return json({ ok: true });
  const reply = text => telegram(env, "sendMessage", { text, reply_parameters: { message_id: message.message_id } });
  if (message.reply_to_message && await liveReason(env, message)) return json({ ok: true });

  const lines = message.text.split("\n").map(l => l.trim()).filter(Boolean);
  const commands = lines.map(storyCommand);
  if (commands.some(Boolean)) {
    if (!commands.every(Boolean)) {
      await reply(GLOSSARY_HELP);
      return json({ ok: true });
    }
    try {
      await reply(await addStoryCommands(env, commands, message.message_id));
    } catch (error) {
      await reply(`Не получилось: ${error.message}`.slice(0, 1000));
    }
    return json({ ok: true });
  }

  const changes = [];
  for (const line of lines) {
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

// «/move 1234 s471», «/move №1234 new», «/move 1234 1240 new» (вместе), «/split s191 s298», «/unmerge s102», «/merge s1 s2»; в группе Telegram дописывает к команде
// @имя_бота — его отбрасываем. Не команда сюжетам — null
function storyCommand(line) {
  const [head, ...args] = line.replace(/^(\/\w+)@\w+/, "$1").split(/[\s,]+/);
  const story = /^s\d+$/;
  const to = args.at(-1), numbers = args.slice(0, -1);
  if (head === "/move" && numbers.length && numbers.every(a => /^[№#]?\d+$/.test(a)) && (story.test(to) || to === "new")) {
    const news = numbers.map(a => Number(a.replace(/^[№#]/, "")));
    return { do: "move", news: news.length === 1 ? news[0] : news, to };
  }
  if (head === "/split" && args.length && args.every(a => story.test(a))) return { do: "split", parts: args };
  if (head === "/unmerge" && args.length === 1 && story.test(args[0])) return { do: "unmerge", story: args[0] };
  if (head === "/merge" && args.length === 2 && args.every(a => story.test(a))) return { do: "merge", story: args[0], to: args[1] };
  return head.startsWith("/") && ["/move", "/split", "/unmerge", "/merge"].includes(head) ? { do: "bad" } : null;
}

// кнопка под отчётом проверки сюжетов: «fix:2026-11-14:7» — убрать находку 7 («:s» в конце — она уверенная),
// «fix:2026-11-14:sure» — все уверенные. Нажатую кнопку убираем с сообщения, чтобы не нажать дважды, а после «все
// уверенные» — и кнопки уверенных находок со всех сообщений отчёта; ответ — сообщением на отчёт, как на набранную команду
async function storyButton(env, query) {
  const answer = text => telegram(env, "answerCallbackQuery", { callback_query_id: query.id, text });
  const message = query.message;
  if (!message || String(message.chat?.id) !== String(env.TELEGRAM_CHAT_ID)) return answer("Эта кнопка не отсюда");
  const fix = String(query.data || "").match(/^fix:(\d{4}-\d\d-\d\d):(sure|\d+)(:s)?$/);
  if (!fix) return answer("Не знаю такой кнопки");
  try {
    const text = await addStoryCommands(env, [{ do: "fix", day: fix[1], which: fix[2] === "sure" ? "sure" : Number(fix[2]) }],
      message.message_id);
    await answer("Записала: применится в ближайшей сборке");
    const keyboards = (await env.REPORTS.get(`fix:${fix[1]}`, "json")) || {};
    keyboards[message.message_id] = message.reply_markup?.inline_keyboard || [];
    const gone = fix[2] === "sure" ? b => b.callback_data === query.data || b.callback_data.endsWith(":s")
      : b => b.callback_data === query.data;
    for (const [id, keyboard] of Object.entries(keyboards)) {
      if (fix[2] !== "sure" && Number(id) !== message.message_id) continue;
      const rows = keyboard.map(row => row.filter(b => !gone(b))).filter(row => row.length);
      if (rows.flat().length === keyboard.flat().length) continue;
      keyboards[id] = rows;
      await telegram(env, "editMessageReplyMarkup", { message_id: Number(id), reply_markup: { inline_keyboard: rows } });
    }
    await env.REPORTS.put(`fix:${fix[1]}`, JSON.stringify(keyboards), { expirationTtl: KEEP_SECONDS });
    await telegram(env, "sendMessage", { text, reply_parameters: { message_id: message.message_id } });
  } catch (error) {
    await answer(`Не получилось: ${error.message}`.slice(0, 200));
  }
}

// stories-commands.json — по команде на строку; id растёт, сборка применяет те, что новее последней применённой
async function addStoryCommands(env, commands, messageId) {
  const bad = commands.filter(c => c.do === "bad");
  if (bad.length) return GLOSSARY_HELP;
  if (!env.GITHUB_TOKEN) throw new Error("у worker нет секрета GITHUB_TOKEN");
  const api = `${env.GITHUB_API || "https://api.github.com"}/repos/${REPO}/contents/${COMMANDS_PATH}`;
  const headers = githubHeaders(env);
  for (let attempt = 0; attempt < 2; attempt++) {
    const file = await fetch(`${api}?ref=main`, { headers });
    if (!file.ok && file.status !== 404) throw new Error(`GitHub не отдал ${COMMANDS_PATH}: ${file.status}`);
    const { content, sha } = file.ok ? await file.json() : {};
    const list = content ? JSON.parse(fromBase64(content)).commands : [];
    let id = Math.max(0, ...list.map(c => c.id));
    const t = belgrade(Date.now());
    for (const c of commands) list.push({ id: ++id, t, message: messageId, ...c });
    const kept = list.slice(-COMMANDS_KEEP);
    const text = `{\n  "commands": [\n${kept.map(c => `    ${JSON.stringify(c)}`).join(",\n")}\n  ]\n}\n`;
    const label = commands.map(c => c.do === "move" ? `move ${[c.news].flat().join(" ")} → ${c.to}` : c.do === "split" ? `split ${c.parts.join(" ")}`
      : c.do === "fix" ? `fix ${c.day} ${c.which === "sure" ? "все уверенные" : "находка " + c.which}`
      : c.do === "merge" ? `merge ${c.story} → ${c.to}` : `unmerge ${c.story}`);
    const saved = await fetch(api, {
      method: "PUT", headers,
      body: JSON.stringify({ message: `Stories from Telegram: ${label.join(", ")}`.slice(0, 200), content: toBase64(text), sha, branch: "main" }),
    });
    if (saved.status === 409) continue;
    if (!saved.ok) throw new Error(`GitHub не сохранил ${COMMANDS_PATH}: ${saved.status}`);
    return [...label, "", "Применится в сборке минуты через 3 — отвечу сюда, что вышло."].join("\n");
  }
  throw new Error(`${COMMANDS_PATH} всё время меняется, попробуй ещё раз`);
}

async function storyResults(request, env) {
  const { results } = await request.json();
  const reports = {};  // день → {id сообщения: кнопки} — кнопки отчёта, отправленные сейчас
  for (const r of (Array.isArray(results) ? results : []).slice(0, 20)) {
    const text = String(r?.text || "").slice(0, 1000);
    if (!text) continue;
    const payload = { text };
    const rows = (Array.isArray(r.buttons) ? r.buttons : []).filter(Array.isArray)
      .map(row => row.filter(b => b?.text && b?.data).map(b => ({ text: String(b.text).slice(0, 64), callback_data: String(b.data).slice(0, 64) })))
      .filter(row => row.length);
    if (rows.length) payload.reply_markup = { inline_keyboard: rows };
    if (Number.isInteger(r.message)) payload.reply_parameters = { message_id: r.message, allow_sending_without_reply: true };
    const sent = await telegram(env, "sendMessage", payload);
    const day = rows.flat().map(b => b.callback_data.match(/^fix:(\d{4}-\d\d-\d\d):/)?.[1]).find(Boolean);
    if (sent && day) (reports[day] ||= {})[sent.message_id] = rows;
  }
  for (const [day, sent] of Object.entries(reports)) {
    const keyboards = (await env.REPORTS.get(`fix:${day}`, "json")) || {};
    await env.REPORTS.put(`fix:${day}`, JSON.stringify({ ...keyboards, ...sent }), { expirationTtl: KEEP_SECONDS });
  }
  return json({ ok: true });
}

function githubHeaders(env) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`, Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vestomat-bot", "Content-Type": "application/json",
  };
}

// glossary.json — по записи на строку, как его пишем руками; дописываем в конец, существующую — правим на месте
async function editGlossary(env, changes) {
  if (!env.GITHUB_TOKEN) throw new Error("у worker нет секрета GITHUB_TOKEN");
  const api = `${env.GITHUB_API || "https://api.github.com"}/repos/${REPO}/contents/${GLOSSARY_PATH}`;
  const headers = githubHeaders(env);
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

async function findItem(url, dataUrl, ready = () => true) {
  // data.json обновляется раз в 15 минут — кэшируем на 5, чтобы не тянуть его на каждую жалобу;
  // если новости в нём нет (или она ещё не та — ready, например без "tx"), она может быть свежее кэша —
  // тогда перечитываем мимо кэша
  for (const fresh of [false, true]) {
    const response = await fetch(fresh ? `${dataUrl}?t=${Date.now()}` : dataUrl,
      fresh ? {} : { cf: { cacheTtl: 300, cacheEverything: true } });
    if (!response.ok) continue;
    const data = await response.json();
    for (const src of data.sources) {
      const item = src.items.find(i => i.url === url && i.ru && (fresh || ready(i)));
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
