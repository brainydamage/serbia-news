"""Vestomat Daily — сводка за сутки в Telegram-канал @vestomat_daily (https://t.me/vestomat_daily).

Раз в день, первой сборкой после AT часов по Белграду (и не позже UNTIL), stories_live.run зовёт post(): главные темы
за сутки (с AT вчера). Кандидаты — CANDIDATES тем, о которых писали больше всего изданий (не меньше MIN_OUTLETS и не
меньше MIN_SIDES основных сторон из четырёх); из них Opus одним запросом выбирает до TOP самых важных и ставит по
важности, а не по шуму. Он же пишет «Коротко» — день одной фразой, под каждой темой — что произошло за день, и для
каждой стороны (критичные к SNS, крупные порталы, государственные, близкие к SNS) — на чём её подача. Подача — таблицей:
сторона и издание со ссылкой на его статью (самую типичную для темы) — и в чём акцент; молчавшая сторона — «не писали».
Внизу — «Ещё сегодня» (заметные темы, не вошедшие в топ) и легенда цветов.

Пост — rich message Telegram (Bot API 10.1+, sendRichMessage: заголовки, таблицы, цитаты, до 32768 знаков). Вместе с
ним сборка отдаёт простой вариант (HTML sendMessage, до 4096 знаков): его worker шлёт, если rich не прошёл.

Отправляет worker/ (POST /daily, тот же бот, что шлёт жалобы): в канал DAILY_CHAT_ID, а пока он не задан — черновиком
в чат жалоб. Одну сводку за день worker отправляет один раз, даже если сборка пришлёт её снова (кэш сюжетов пропал).
Готовый текст лежит в состоянии сюжетов (state["daily"]): не дошёл до Telegram — следующая сборка шлёт его же, не
спрашивая модель заново.

    .venv/bin/python daily.py [stories.json] [archive.json]   — локально: какие темы попали бы в сводку сейчас и что
                                                                 увидела бы модель, без запроса к ней и без отправки
"""
import html
import json
import sys
import traceback
from datetime import datetime, timedelta, timezone

import numpy as np

import stories
import translate

AT, UNTIL = 20, 23  # часы по Белграду: с AT:00 до UNTIL:59; сборки позже нет — день без сводки
TOP = 5  # тем в сводке, не больше; в тихий день кандидатов может быть меньше
CANDIDATES = 12  # из стольких самых обсуждаемых тем модель выбирает TOP: с запасом для важных, но тихих тем
MIN_OUTLETS = 10  # тема, о которой за сутки писали меньше изданий, — не в сводку
MIN_SIDES = 3  # и о которой писали меньше основных сторон (SIDES): тема одной стороны — её повестка, а не новость дня
SHOWN = 6  # сколько новостей каждой стороны видит модель: самые типичные для темы, по одной на издание
KEEP_DAYS = 7
LIMIT = 3900  # Telegram берёт до 4096 знаков текста (без разметки); лишние темы с конца — прочь
SIDES = ["independent", "mainstream", "state", "progov"]  # порядок как на страницах тем (stories_live.SR_SPECTRUM)
MARKS = {"independent": "🔵", "mainstream": "⚪", "state": "🟣", "progov": "🔴"}  # цвета сторон на сайте
SIDE_NAMES = {"independent": "критичные к SNS", "mainstream": "крупные порталы", "state": "государственные",
              "progov": "близкие к SNS"}
SITE = "https://vestomat.rs/ru/#teme"
MORE = 3  # «Ещё сегодня»: столько самых обсуждаемых кандидатов, не вошедших в топ
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
          "декабря"]
DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

PROMPT = """Ты пишешь вечернюю сводку новостей Сербии за день для Telegram-канала на русском языке. Читатели — русскоязычные люди, живущие в Сербии или следящие за ней.
Вход — JSON: themes — самые обсуждаемые темы дня, id вида T…: name — название темы; outlets — сколько изданий писали о ней за сутки; sides — заголовки и пересказы новостей темы за сутки по сторонам: independent — издания, критичные к SNS (правящей партии), mainstream — крупные порталы, state — государственные СМИ, progov — издания, близкие к SNS. Стороны, которой нет, о теме за день не писали.
Выбери до 5 самых важных тем и верни их в порядке важности, самую важную первой. Важность — не число публикаций, а значение для страны и для жизни людей в Сербии: власть и политика, суды и громкие дела, законы и решения, экономика и цены, безопасность, протесты, крупные происшествия и их последствия. Число изданий — подсказка, а не правило. Криминальная хроника, частная трагедия или скандал без общественного значения — только если важнее за день ничего нет. Если две темы — про одну и ту же ситуацию, бери одну.
Для каждой выбранной темы верни:
- what — что произошло за день, одно-два коротких предложения, до 220 знаков. Факты, которые подтверждают разные стороны, без оценок. Если тема длится не первый день — что нового сегодня. Имена — как принято по-русски («Вучич», «Скупщина»), сербские названия изданий и партий — латиницей.
- sides — подача каждой стороны, которая писала о теме: side — сторона (independent, mainstream, state, progov), angle — на чём её акцент, по её заголовкам: что выносит вперёд, кого цитирует, как называет событие. До 60 знаков, без подлежащего-стороны, с маленькой буквы, как пометка в таблице («депутаты большинства аплодировали Вучичу», «через слова Вучича о «врачарском клане»»). Если сторона подаёт событие без своего акцента — «нейтрально, фактом». Пиши только то, что видно по заголовкам, не домысливай мотивы.
Ещё верни summary — день одной фразой до 160 знаков: две-три главные темы через запятую, без вводных слов («Задержан главред Informer, у Скупщины появились комитеты, новые аресты по делу «Алта банки»»).
Без кавычек вокруг ответа, без хэштегов и эмодзи."""
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "what": {"type": "string"},
                    "sides": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"side": {"type": "string", "enum": SIDES}, "angle": {"type": "string"}},
                            "required": ["side", "angle"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "what", "sides"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "themes"],
    "additionalProperties": False,
}
# пишет Opus: сводка — лицо канала, и она одна в день (вход ~20 тыс. токенов, ≈13 центов). Цена — как в stories_gold.py
MODEL = "claude-opus-5-5"
PRICE = (4.0, 20.0)  # вход / выход за 1M токенов, $
EFFORT = "medium"


def _when(it: dict) -> str:
    return it.get("published", it["seen"])


def window(now: str, tz) -> tuple[str, str] | None:
    """(день сводки, начало суток в UTC), если сейчас время сводки (AT–UNTIL по Белграду); иначе None."""
    local = datetime.fromisoformat(now).astimezone(tz)
    if not AT <= local.hour <= UNTIL:
        return None
    start = local.replace(hour=AT, minute=0, second=0, microsecond=0) - timedelta(days=1)
    return local.date().isoformat(), start.astimezone(timezone.utc).isoformat()


def pick(state: dict, since: str, now: str) -> list[tuple[str, list[str]]]:
    """Кандидаты в сводку: (сюжет, его новости за сутки) — о Сербии (не foreign), не регулярные сводки, не меньше
    MIN_OUTLETS изданий и MIN_SIDES основных сторон за сутки; по числу изданий, потом новостей. Не больше CANDIDATES."""
    out = []
    for sid, story in state["stories"].items():
        if state.get("serbia", {}).get(sid) == "foreign" or state.get("routine", {}).get(sid):
            continue
        urls = [u for u in story["urls"] if since <= state["items"][u]["seen"] <= now]
        outlets = {state["items"][u]["source"] for u in urls}
        sides = {state["items"][u]["section"] for u in urls} & set(SIDES)
        if len(outlets) >= MIN_OUTLETS and len(sides) >= MIN_SIDES:
            out.append((len(outlets), len(urls), sid, urls))
    out.sort(key=lambda x: x[:2], reverse=True)
    return [(sid, urls) for _, _, sid, urls in out[:CANDIDATES]]


def central(state: dict, vectors: dict, urls: list[str]) -> list[str]:
    """Новости по убыванию типичности для темы за сутки (ближе всех к среднему вектору); без вектора — в конце."""
    have = [u for u in urls if u in vectors]
    if not have:
        return list(urls)
    v = np.stack([vectors[u] for u in have]).astype(np.float32)
    order = [have[i] for i in np.argsort(-(v @ v.mean(axis=0)))]
    return order + [u for u in urls if u not in vectors]


def by_side(state: dict, vectors: dict, urls: list[str]) -> dict[str, list[str]]:
    """Сторона → её новости за сутки, самые типичные первыми, по одной на издание."""
    sides = {}
    for u in central(state, vectors, urls):
        it = state["items"][u]
        if it["section"] in SIDES and it["source"] not in {state["items"][x]["source"] for x in sides.get(it["section"], [])}:
            sides.setdefault(it["section"], []).append(u)
    return sides


def name_of(state: dict, sid: str, urls: list[str]) -> str:
    if name := state.get("names", {}).get(sid):
        return name
    it = state["items"][urls[0]]
    return it.get("what") or it["title"]


def request_of(state: dict, vectors: dict, themes: list[tuple[str, list[str]]]) -> str:
    """Вход модели: темы с новостями по сторонам (заголовок перевода и «что произошло»)."""
    def line(u: str) -> str:
        it = state["items"][u]
        return f'{it["source"]}: {it["title"]}' + (f' — {it["what"]}' if it.get("what") else "")

    return json.dumps({"themes": [
        {"id": f"T{k}", "name": name_of(state, sid, urls), "outlets": len({state["items"][u]["source"] for u in urls}),
         "sides": {side: [line(u) for u in news[:SHOWN]] for side, news in by_side(state, vectors, urls).items()}}
        for k, (sid, urls) in enumerate(themes)]}, ensure_ascii=False)


def chosen(themes: list[tuple[str, list[str]]], answer: dict) -> list[tuple[tuple[str, list[str]], dict]]:
    """Темы, которые выбрала модель, в её порядке (не больше TOP, без повторов и чужих id), с её текстами. Модель не
    выбрала ни одной — первые TOP кандидатов, без текстов."""
    out, seen = [], set()
    for t in answer.get("themes", []):
        n = t.get("id", "")[1:]
        if n.isdigit() and int(n) < len(themes) and n not in seen:
            seen.add(n)
            out.append((themes[int(n)], t))
    return out[:TOP] or [(theme, {}) for theme in themes[:TOP]]


def esc(text: str) -> str:
    """Текст для HTML Telegram: только &amp; &lt; &gt; — rich HTML знает лишь немногие именованные сущности."""
    return html.escape(text, quote=False)


def href(url: str) -> str:
    return esc(url).replace('"', "&quot;")


def plural(n: int, one: str, few: str, many: str) -> str:
    k = n % 100
    return one if k % 10 == 1 and k != 11 else few if 2 <= k % 10 <= 4 and not 12 <= k <= 14 else many


def date_of(day: str) -> str:
    d = datetime.fromisoformat(day)
    return f"{DAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def angles(t: dict) -> dict[str, str]:
    return {a["side"]: a["angle"].strip() for a in t.get("sides", []) if a.get("side") in SIDES and a.get("angle", "").strip()}


def compose_rich(state: dict, vectors: dict, picked: list, more: list[tuple[str, list[str]]], summary: str,
                 day: str) -> str:
    """Rich HTML поста (sendRichMessage): заголовок, «Коротко», темы с таблицей подачи, «Ещё сегодня», легенда."""
    out = [f"<h1>Сербия за день</h1>",
           f"<p><i>{date_of(day).capitalize()} · {len(picked)} {plural(len(picked), 'главная тема', 'главные темы', 'главных тем')}"
           f" · как их подали разные стороны</i></p>"]
    if summary.strip():
        out.append(f"<blockquote><b>Коротко:</b> {esc(summary.strip())}</blockquote>")
    for k, ((sid, urls), t) in enumerate(picked):
        sides, said = by_side(state, vectors, urls), angles(t)
        out.append(f"<h3>{k + 1}. {esc(name_of(state, sid, urls))}</h3>")
        if what := t.get("what", "").strip():
            out.append(f"<p>{esc(what)}</p>")
        rows = []
        for s in SIDES:
            if s in sides:
                u = sides[s][0]
                cell = f'{MARKS[s]} <a href="{href(u)}">{esc(state["items"][u]["source"])}</a>'
                rows.append(f"<tr><td>{cell}</td><td>{esc(said.get(s, '—'))}</td></tr>")
            else:
                rows.append(f"<tr><td>{MARKS[s]} {SIDE_NAMES[s]}</td><td><i>не писали</i></td></tr>")
        out.append(f"<table bordered striped compact>{''.join(rows)}</table>")
        n = len({state["items"][u]["source"] for u in urls})
        out.append(f"<p><i>{n} {plural(n, 'издание', 'издания', 'изданий')}</i></p>")
    if more:
        out.append("<hr/><h4>Ещё сегодня</h4><ul>"
                   + "".join(f"<li>{esc(name_of(state, sid, urls))}</li>" for sid, urls in more) + "</ul>")
    out.append("<hr/><p>" + " · ".join(f"{MARKS[s]} {SIDE_NAMES[s]}" for s in SIDES) + "</p>")
    out.append(f'<p>Все темы дня — <a href="{SITE}">vestomat.rs</a></p>')
    return "".join(out)


def compose(state: dict, vectors: dict, picked: list, day: str) -> str:
    """Простой вариант (HTML sendMessage, до LIMIT знаков) — если rich не прошёл: темы, что произошло, подача строкой
    по сторонам, ссылки. Тема без текста модели — только название и ссылки."""
    head = f"<b>Сербия за день · {date_of(day)}</b>"
    foot = f'Все темы дня — <a href="{SITE}">vestomat.rs</a>'
    blocks = []
    for k, ((sid, urls), t) in enumerate(picked):
        sides, said = by_side(state, vectors, urls), angles(t)
        lines = [f"<b>{k + 1}. {esc(name_of(state, sid, urls))}</b>"]
        if what := t.get("what", "").strip():
            lines.append(esc(what))
        for s in SIDES:
            if s in sides:
                u = sides[s][0]
                lines.append(f'{MARKS[s]} <a href="{href(u)}">{esc(state["items"][u]["source"])}</a>'
                             + (f": <i>{esc(said[s])}</i>" if s in said else ""))
        blocks.append("\n".join(lines))
    while len(blocks) > 1 and visible_len("\n\n".join([head, *blocks, foot])) > LIMIT:
        blocks.pop()
    return "\n\n".join([head, *blocks, foot])


def visible_len(text: str) -> int:
    """Длина текста, как её считает Telegram: без тегов, сущности — одним знаком."""
    import re

    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def post(state: dict, vectors: dict, now: str, tz, spend) -> None:
    """Сводка, если пора и за сегодня её ещё не отправили. Ошибка — в лог: сюжеты и сайт важнее."""
    try:
        _post(state, vectors, now, tz, spend)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print(f"Сводка дня: не отправлена — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)


def _post(state: dict, vectors: dict, now: str, tz, spend) -> None:
    if not (when := window(now, tz)):
        return
    day, since = when
    done = state.setdefault("daily", {})
    for old in [d for d in done if d < (datetime.fromisoformat(day) - timedelta(days=KEEP_DAYS)).date().isoformat()]:
        del done[old]
    today = done.setdefault(day, {})
    if today.get("sent"):
        return
    if "text" not in today:
        themes = pick(state, since, now)
        if not themes:
            print("Сводка дня: тем за сутки нет", file=sys.stderr)
            return
        import anthropic

        client = anthropic.Anthropic(max_retries=translate.API_RETRIES)
        answer, usage = stories.ask_model(client, PROMPT, request_of(state, vectors, themes), SCHEMA, EFFORT,
                                          MODEL, PRICE)
        spend("сводка", usage)
        picked = chosen(themes, answer)
        more = [th for th in themes if th[0] not in {sid for (sid, _), _ in picked}][:MORE]
        today["text"] = compose(state, vectors, picked, day)
        today["rich"] = compose_rich(state, vectors, picked, more, answer.get("summary", ""), day)
        today["themes"] = [sid for (sid, _), _ in picked]
        print(f"Сводка дня: {len(picked)} тем из {len(themes)} кандидатов, ≈ ${usage['usd']:.3f}", file=sys.stderr)
    reply = translate.reports_api({"day": day, "text": today["text"], "rich": today.get("rich")}, "daily")
    if reply and reply.get("ok"):
        today["sent"] = now
        today["to"] = reply.get("to")
        print(f"Сводка дня: {'уже была' if reply.get('already') else 'отправлена'} — {reply.get('to')}"
              f"{', ' + reply['kind'] if reply.get('kind') else ''}"
              f"{' (rich не прошёл: ' + reply['rich_error'] + ')' if reply.get('rich_error') else ''}"[:400], file=sys.stderr)
    else:
        print(f"Сводка дня: worker не принял — {reply}"[:300] + ", пробуем следующей сборкой", file=sys.stderr)


if __name__ == "__main__":
    from pathlib import Path
    from zoneinfo import ZoneInfo

    root = Path(__file__).parent
    state = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else root / "cache" / "stories.json").read_text("utf-8"))
    now = state.get("built") or datetime.now().astimezone().isoformat(timespec="seconds")
    tz = ZoneInfo("Europe/Belgrade")
    local = datetime.fromisoformat(now).astimezone(tz)
    start = local.replace(hour=AT, minute=0, second=0, microsecond=0)
    start -= timedelta(days=1 if local.hour >= AT else 2)
    themes = pick(state, start.astimezone(timezone.utc).isoformat(), now)
    vectors = {}  # без векторов: «типичные» — в порядке темы
    print(request_of(state, vectors, themes))
    fake = {"summary": "(день одной фразой — напишет модель)",
            "themes": [{"id": f"T{k}", "what": "(что произошло — напишет модель)",
                        "sides": [{"side": s, "angle": "(акцент стороны)"} for s in SIDES[:3]]} for k in range(len(themes))]}
    picked = chosen(themes, fake)
    more = [th for th in themes if th[0] not in {sid for (sid, _), _ in picked}][:MORE]
    text = compose(state, vectors, picked, local.date().isoformat())
    rich = compose_rich(state, vectors, picked, more, fake["summary"], local.date().isoformat())
    print(f"\n{text}\n\n{visible_len(text)} знаков\n\n{rich}\n\n{visible_len(rich)} знаков rich", file=sys.stderr)
