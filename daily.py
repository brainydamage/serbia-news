"""Vestomat Daily — сводка за сутки в Telegram-канал @vestomat_daily (https://t.me/vestomat_daily).

Раз в день, первой сборкой после AT часов по Белграду (и не позже UNTIL), stories_live.run зовёт post(): главные темы
за сутки (с AT вчера) — по числу изданий, которые о них писали. Под каждой темой — что произошло за день и одна строка о
том, как её подали стороны (критичные к SNS, крупные порталы, государственные, близкие к SNS); это пишет Opus одним
запросом. Ссылки — на сами новости: по одной от каждой писавшей стороны, самая типичная для темы.

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
from collections import Counter
from datetime import datetime, timedelta, timezone

import numpy as np

import stories
import translate

AT, UNTIL = 20, 23  # часы по Белграду: с AT:00 до UNTIL:59; сборки позже нет — день без сводки
TOP = 8  # тем в сводке, не больше
MIN_OUTLETS = 3  # тема, о которой за сутки писали меньше изданий, — не в сводку
SHOWN = 6  # сколько новостей каждой стороны видит модель: самые типичные для темы, по одной на издание
KEEP_DAYS = 7
LIMIT = 3900  # Telegram берёт до 4096 знаков текста (без разметки); лишние темы с конца — прочь
SIDES = ["independent", "mainstream", "state", "progov"]  # порядок как на страницах тем (stories_live.SR_SPECTRUM)
MARKS = {"independent": "🔵", "mainstream": "⚪", "state": "🟣", "progov": "🔴"}  # цвета сторон на сайте
SIDE_NAMES = {"independent": "критичные к SNS", "mainstream": "крупные порталы", "state": "государственные",
              "progov": "близкие к SNS"}
SITE = "https://vestomat.rs/ru/#teme"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
          "декабря"]
DAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

PROMPT = """Ты пишешь вечернюю сводку новостей Сербии за день для Telegram-канала на русском языке. Читатели — русскоязычные люди, живущие в Сербии или следящие за ней.
Вход — JSON: themes — главные темы дня, id вида T…: name — название темы; sides — заголовки и пересказы новостей темы за сутки по сторонам: independent — издания, критичные к SNS (правящей партии), mainstream — крупные порталы, state — государственные СМИ, progov — издания, близкие к SNS. Стороны, которой нет, о теме за день не писали.
Для каждой темы верни:
- what — что произошло за день, одно-два коротких предложения, до 220 знаков. Факты, которые подтверждают разные стороны, без оценок. Если тема длится не первый день — что нового сегодня. Имена — как принято по-русски («Вучич», «Скупщина»), сербские названия изданий и партий — латиницей.
- framing — одна строка до 200 знаков: чем различается подача сторон — на чём делают акцент, кого цитируют, как называют событие. Называй стороны так: «критичные к SNS», «крупные порталы», «государственные», «близкие к SNS». Пиши только то, что видно по заголовкам. Если все подают одинаково — так и скажи коротко («подают одинаково»); если одна из основных сторон молчит, а другие пишут много — это тоже подача, скажи об этом.
Без кавычек вокруг ответа, без хэштегов и эмодзи."""
SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "what": {"type": "string"}, "framing": {"type": "string"}},
                "required": ["id", "what", "framing"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["themes"],
    "additionalProperties": False,
}
# пишет Opus: сводка — лицо канала, и она одна в день (вход ~15 тыс. токенов, ≈10 центов). Цена — как в stories_gold.py
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
    """Темы сводки: (сюжет, его новости за сутки) — о Сербии (не foreign), не регулярные сводки, не меньше
    MIN_OUTLETS изданий за сутки; по числу изданий, потом новостей. Не больше TOP."""
    out = []
    for sid, story in state["stories"].items():
        if state.get("serbia", {}).get(sid) == "foreign" or state.get("routine", {}).get(sid):
            continue
        urls = [u for u in story["urls"] if since <= state["items"][u]["seen"] <= now]
        outlets = {state["items"][u]["source"] for u in urls}
        if len(outlets) >= MIN_OUTLETS and any(state["items"][u]["section"] in SIDES for u in urls):
            out.append((len(outlets), len(urls), sid, urls))
    out.sort(key=lambda x: x[:2], reverse=True)
    return [(sid, urls) for _, _, sid, urls in out[:TOP]]


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
        {"id": f"T{k}", "name": name_of(state, sid, urls),
         "sides": {side: [line(u) for u in news[:SHOWN]] for side, news in by_side(state, vectors, urls).items()}}
        for k, (sid, urls) in enumerate(themes)]}, ensure_ascii=False)


def compose(state: dict, vectors: dict, themes: list[tuple[str, list[str]]], answer: dict, day: str) -> str:
    """Текст поста (HTML Telegram). Тема без ответа модели — только название и ссылки."""
    e = html.escape
    said = {t["id"]: t for t in answer.get("themes", [])}
    d = datetime.fromisoformat(day)
    head = f"<b>Сербия за день · {DAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}</b>"
    foot = f'Все темы дня — <a href="{SITE}">vestomat.rs</a>'
    blocks = []
    for k, (sid, urls) in enumerate(themes):
        t = said.get(f"T{k}", {})
        sides = by_side(state, vectors, urls)
        links = " · ".join(f'{MARKS[s]} <a href="{e(sides[s][0], quote=True)}">{e(state["items"][sides[s][0]]["source"])}</a>'
                           for s in SIDES if s in sides)
        outlets = len({state["items"][u]["source"] for u in urls})
        lines = [f"<b>{k + 1}. {e(name_of(state, sid, urls))}</b>"]
        if what := t.get("what", "").strip():
            lines.append(e(what))
        if framing := t.get("framing", "").strip():
            lines.append(f"<i>Как подали: {e(framing[0].lower() + framing[1:])}</i>")
        lines.append(f"{links} · {outlets} изд.")
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
        today["text"] = compose(state, vectors, themes, answer, day)
        today["themes"] = [sid for sid, _ in themes]
        print(f"Сводка дня: {len(themes)} тем, ≈ ${usage['usd']:.3f}", file=sys.stderr)
    reply = translate.reports_api({"day": day, "text": today["text"]}, "daily")
    if reply and reply.get("ok"):
        today["sent"] = now
        today["to"] = reply.get("to")
        print(f"Сводка дня: {'уже была' if reply.get('already') else 'отправлена'} — {reply.get('to')}", file=sys.stderr)
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
    fake = {"themes": [{"id": f"T{k}", "what": "(что произошло — напишет модель)", "framing": "(как подали — напишет модель)"}
                       for k in range(len(themes))]}
    text = compose(state, vectors, themes, fake, local.date().isoformat())
    print(f"\n{text}\n\n{visible_len(text)} знаков", file=sys.stderr)
