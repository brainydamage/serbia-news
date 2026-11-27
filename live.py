"""Vestomat Live — срочные новости молнией в Telegram (@vestomat_live, https://t.me/vestomat_live).

Каждая сборка (раз в 15 минут) stories_live.run зовёт post(). Два шага:

1. Сигнал — код, бесплатно. Тема резко пошла: за последний WINDOW о ней написали не меньше MIN_OUTLETS изданий и не
   меньше MIN_SIDES основных сторон. Новая тема или новый всплеск в старой (25.11 главная новость дня — задержание
   главреда Informer — случилась внутри давно идущей темы). Одну тему спрашиваем не чаще раза в AGAIN.
   Сама по себе вспышка — не важность: сербские издания перепечатывают агентства, и «10 изданий за час» случается
   8–20 раз в день, в том числе на ДТП и находку тела (замер 22–25.11).
2. Решение — Opus по правилам (PROMPT): меняет ли это повседневную жизнь человека, живущего в Сербии, и надо ли ему
   узнать об этом сейчас, а не вечером из Daily. Канал на русском — всё про русских и россиян в Сербии важнее. Мировая
   политика — нет. Ночью (NIGHT) — только исключительное. Не больше PER_DAY постов за день по Белграду; дошли до
   предела — до полуночи не спрашиваем. Ничего не случилось — молчим: нормы нет.

Пост — молния: факт одной-двумя фразами и ссылки на 2–3 издания разных сторон.

Отправляет worker/ (POST /live): в канал LIVE_CHAT_ID, а пока он не задан — черновиком в чат жалоб. Под каждым
черновиком (а когда пишем в канал — под его копией в чате жалоб) кнопки «✓ срочно» / «✗ могло подождать»; ответ на
пост одной фразой — почему. Раз в день, первой сборкой после REJECTED_AT, — список тем, которые модель сочла
несрочными, с кнопкой «надо было» у каждой: так видны пропуски. Отметки копит worker (GET /live), и каждый запрос к
модели получает последние FEEDBACK из них как прецеденты редактора.

    .venv/bin/python live.py [stories.json]   — локально: прогон по сохранённому состоянию, сборка за сборкой каждые
                                                 15 минут: какие темы дали бы сигнал, без запроса к модели и отправки
"""
import html
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone

import numpy as np

import stories
import translate

WINDOW = timedelta(minutes=60)
MIN_OUTLETS = 8  # за WINDOW — столько изданий
MIN_SIDES = 3  # и столько основных сторон: тема одной стороны — её повестка, а не срочное
AGAIN = timedelta(hours=3)  # одну тему спрашиваем не чаще: вспышка длится часа два-три
FRESH = timedelta(hours=3)  # новость, опубликованная раньше, в вспышку не идёт: кэш пропал — сборка видит всё разом
PER_DAY = 3
NIGHT = (23, 7)  # с 23:00 до 7:00 по Белграду — только исключительное
REJECTED_AT = 20  # список несрочного за день — первой сборкой после 20:00 по Белграду, вместе с Daily
REJECTED_MAX = 12
STALE = timedelta(minutes=45)  # пост, который не дошёл до Telegram за столько, не шлём: молния опоздала
KEEP = timedelta(days=7)
SHOWN = 5  # сколько новостей каждой стороны за WINDOW видит модель: самые типичные, по одной на издание
BEFORE = 6  # и сколько новостей темы до вспышки — что было известно раньше
FEEDBACK = 30
SIDES = ["independent", "mainstream", "state", "progov"]
MODEL = "claude-opus-5-5"
PRICE = (4.0, 20.0)  # вход / выход за 1M токенов, $ — как в daily.py
EFFORT = "medium"

PROMPT = """Ты дежурный редактор Telegram-канала срочных новостей Сербии на русском языке. Читатели — русскоязычные люди, живущие в Сербии: россияне, белорусы, украинцы и другие. Вечером у них есть отдельная сводка дня, поэтому сюда — только то, что нужно узнать сейчас, а не вечером. Лишний пост раздражает, пропуск обесценивает канал. Если сомневаешься — не срочно.

Вход — JSON: candidates — темы, о которых за последний час резко начали писать сербские издания, id вида C…: name — название темы; outlets_hour — сколько изданий написали за час; age_hours — сколько часов тема идёт; hour — новости за последний час по сторонам (independent — критичные к правящей партии SNS, mainstream — крупные порталы, state — государственные СМИ, progov — близкие к SNS), в квадратных скобках номер; before — что писали в этой теме раньше; posted — что канал уже отправлял об этой теме. Ещё: night — сейчас ночь; posted_today — сколько срочных постов уже было сегодня (предел 3); editor — поправки редактора канала к прошлым решениям: учитывай их как прецеденты, они важнее общих правил.

Срочно (urgent) — новое событие, которое меняет повседневную жизнь человека в Сербии или касается его безопасности:
- решения власти, которые касаются жизни людей: цены, тарифы, налоги, зарплаты, пенсии, законы и правила, которые вступают в силу, отставка правительства или премьера, назначение выборов;
- ключевые события выборов: кто идёт кандидатом в президенты или во главе списка от власти, оппозиции или студентов — когда его официально назвали (это событие, а не заявление), снятие кандидата, бойкот, перенос или отмена выборов, их итоги. Анонс («в 12 назовут кандидата») — ещё не срочно, срочно — когда назвали;
- ЧП с жертвами или угрозой многим людям: стрельба, взрывы, крупные пожары, обрушения, массовые отравления; отключения воды, света, газа, связи; опасная погода, наводнения, землетрясения;
- задержания и обвинения против людей наверху: министры, мэры, руководители госкомпаний, крупный бизнес, главы СМИ;
- массовые протесты, блокады дорог, мостов, границ, забастовки транспорта — то, что меняет, как людям передвигаться и жить;
- резкие экономические события: банки, курс динара, бензин, NIS, крупные закрытия и увольнения;
- всё, что касается русских, россиян, белорусов, украинцев в Сербии: визы, ВНЖ, высылки, задержания, банки и карты, правила для иностранцев.
Исключительное (exceptional) — то, ради чего стоит разбудить человека: массовые жертвы, теракт, угроза жизни многим прямо сейчас, падение правительства, событие на всю страну.
Не срочно (no):
- мировая политика, в том числе решения ЕС, России, США, Косово — если они не меняют жизнь в Сербии прямо сейчас;
- бытовая криминальная хроника: ДТП, находка тела, бытовые убийства, драки — если нет угрозы многим;
- заявления, обвинения, перепалки и интервью политиков без нового события; встречи с послами, визиты, рутинные заседания, анонсы;
- развитие темы, о которой канал уже писал, если нет нового события (было задержание — теперь подробности допроса: не срочно; назначили преемника — срочно).
Чем больше постов уже было сегодня, тем выше планка. Ночью — только исключительное.

Для каждого кандидата верни: id; level — no, urgent или exceptional; reason — почему, одной короткой фразой (её прочитает редактор). Для urgent и exceptional ещё:
- text — молния: что произошло, одна-две фразы до 260 знаков, факты, которые подтверждают разные стороны, без оценок и без «срочно»/«внимание». Имена — как принято по-русски («Вучич», «Скупщина»), сербские названия изданий и партий — латиницей;
- picks — 2–3 новости из hour для ссылок: {side, pick}, по возможности разных сторон, самые информативные; не видео и не «онлайн»-ленты.
Если два кандидата — про одно событие, срочным отметь только один."""
SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "level": {"type": "string", "enum": ["no", "urgent", "exceptional"]},
                    "reason": {"type": "string"},
                    "text": {"type": "string"},
                    "picks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"side": {"type": "string", "enum": SIDES}, "pick": {"type": "integer"}},
                            "required": ["side", "pick"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "level", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


def when(it: dict) -> str:
    """Когда новость вышла: время публикации, но не позже, чем сборка её увидела."""
    return min(it.get("published") or it["seen"], it["seen"])


def local_day(iso: str, tz) -> str:
    return datetime.fromisoformat(iso).astimezone(tz).date().isoformat()


def is_night(now: str, tz) -> bool:
    hour = datetime.fromisoformat(now).astimezone(tz).hour
    return hour >= NIGHT[0] or hour < NIGHT[1]


def signals(state: dict, now: str, asked: dict) -> list[tuple[str, list[str]]]:
    """Темы со вспышкой: (сюжет, его новости за WINDOW) — о Сербии, не регулярные сводки, не меньше MIN_OUTLETS изданий
    и MIN_SIDES сторон за WINDOW. Тему, спрошенную за AGAIN, — только если новая вспышка набралась из новостей после
    вопроса: анонс «в 12 студенты назовут кандидата» не должен закрыть на три часа саму новость. Сначала — где больше
    изданий."""
    t = datetime.fromisoformat(now)
    since, fresh, again = (t - WINDOW).isoformat(), (t - FRESH).isoformat(), (t - AGAIN).isoformat()
    out = []
    for sid, story in state["stories"].items():
        if state.get("serbia", {}).get(sid) == "foreign" or state.get("routine", {}).get(sid):
            continue
        last = asked.get(sid, "")
        after = last if last > again else ""  # спрошена недавно — в счёт только то, чего модель ещё не видела
        urls = [u for u in story["urls"] if after < state["items"][u]["seen"] <= now and when(state["items"][u]) >= since
                and (state["items"][u].get("published") or now) >= fresh]
        outlets = {state["items"][u]["source"] for u in urls}
        sides = {state["items"][u]["section"] for u in urls} & set(SIDES)
        if len(outlets) >= MIN_OUTLETS and len(sides) >= MIN_SIDES:
            out.append((len(outlets), sid, urls))
    out.sort(reverse=True)
    return [(sid, urls) for _, sid, urls in out]


def typical(vectors: dict, urls: list[str]) -> list[str]:
    have = [u for u in urls if u in vectors]
    if not have:
        return list(urls)
    v = np.stack([vectors[u] for u in have]).astype(np.float32)
    return [have[i] for i in np.argsort(-(v @ v.mean(axis=0)))] + [u for u in urls if u not in vectors]


def by_side(state: dict, vectors: dict, urls: list[str]) -> dict[str, list[str]]:
    """Сторона → её новости, самые типичные первыми, по одной на издание, не больше SHOWN."""
    sides = {}
    for u in typical(vectors, urls):
        it = state["items"][u]
        mine = sides.setdefault(it["section"], []) if it["section"] in SIDES else None
        if mine is not None and len(mine) < SHOWN and it["source"] not in {state["items"][x]["source"] for x in mine}:
            mine.append(u)
    return {s: v for s, v in sides.items() if v}


def name_of(state: dict, sid: str, urls: list[str]) -> str:
    return state.get("names", {}).get(sid) or state["items"][urls[0]].get("what") or state["items"][urls[0]]["title"]


def line(state: dict, u: str) -> str:
    it = state["items"][u]
    return f'{it["source"]}: {it["title"]}' + (f' — {it["what"]}' if it.get("what") else "")


def request_of(state: dict, vectors: dict, found: list[tuple[str, list[str]]], live: dict, now: str, tz,
               feedback: list[dict]) -> str:
    t = datetime.fromisoformat(now)
    day = local_day(now, tz)
    out = []
    for k, (sid, urls) in enumerate(found):
        story = state["stories"][sid]
        earlier = [u for u in story["urls"] if u not in set(urls)]
        out.append({
            "id": f"C{k}", "name": name_of(state, sid, urls),
            "outlets_hour": len({state["items"][u]["source"] for u in urls}),
            "age_hours": round((t - datetime.fromisoformat(story["created"])).total_seconds() / 3600, 1),
            "hour": {s: [f"[{i}] {line(state, u)}" for i, u in enumerate(news)]
                     for s, news in by_side(state, vectors, urls).items()},
            "before": [line(state, u) for u in typical(vectors, earlier[-60:])[:BEFORE]],
            "posted": [p["text"] for p in live["posts"] if p["sid"] == sid and p.get("sent")],
        })
    editor = []
    for f in feedback[-FEEDBACK:]:
        verdict = {"ok": "верно, срочно", "no": "отправил зря — могло подождать до вечера",
                   "miss": "пропустил — это было срочно"}.get(f.get("verdict"))
        if verdict and f.get("text"):
            mine = f' (ты решил: {f["why"]})' if f.get("why") else ""  # пропуск — и почему модель сочла его несрочным
            editor.append(f'{verdict}: «{f["text"]}»{mine}' + (f' — редактор: {f["reason"]}' if f.get("reason") else ""))
    return json.dumps({"night": is_night(now, tz),
                       "posted_today": sum(1 for p in live["posts"] if p.get("sent") and p["day"] == day),
                       "editor": editor, "candidates": out}, ensure_ascii=False)


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def links(state: dict, vectors: dict, urls: list[str], picks: list[dict]) -> list[str]:
    """2–3 новости для ссылок: что выбрала модель (по одной на издание), не хватает — самые типичные других сторон."""
    sides = by_side(state, vectors, urls)
    out = []
    for p in picks:
        news = sides.get(p.get("side"), [])
        if isinstance(p.get("pick"), int) and 0 <= p["pick"] < len(news):
            out.append(news[p["pick"]])
    for s in SIDES:
        if len(out) >= 2:
            break
        if s in sides and not any(state["items"][u]["section"] == s for u in out):
            out.append(sides[s][0])
    seen, kept = set(), []
    for u in out:
        if state["items"][u]["source"] not in seen:
            seen.add(state["items"][u]["source"])
            kept.append(u)
    return kept[:3]


def compose(state: dict, text: str, urls: list[str]) -> str:
    """Молния (HTML sendMessage): ⚡ факт жирным, ниже — ссылки, названия изданий через запятую."""
    refs = ", ".join(f'<a href="{esc(u)}">{esc(state["items"][u]["source"])}</a>' for u in urls)
    return f"⚡ <b>{esc(text.strip())}</b>" + (f"\n\n{refs}" if refs else "")


def post(state: dict, vectors: dict, now: str, tz, spend) -> None:
    """Срочное, если что-то случилось. Ошибка — в лог: сюжеты и сайт важнее."""
    try:
        _post(state, vectors, now, tz, spend)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print(f"Live: {type(exc).__name__}: {exc}"[:300], file=sys.stderr)


def send(post_: dict) -> bool:
    reply = translate.reports_api({"id": post_["id"], "text": post_["html"], "theme": post_["name"],
                                   "fact": post_["text"]}, "live")
    if reply and reply.get("ok"):
        post_["sent"] = reply.get("to", "?")
        print(f"Live: отправлено — {post_['sent']}: {post_['text'][:120]}", file=sys.stderr)
        return True
    print(f"Live: worker не принял — {reply}"[:300], file=sys.stderr)
    return False


def _post(state: dict, vectors: dict, now: str, tz, spend) -> None:
    live = state.setdefault("live", {"posts": [], "asked": {}, "rejected": [], "rejected_sent": ""})
    t = datetime.fromisoformat(now)
    old = (t - KEEP).isoformat()
    live["posts"] = [p for p in live["posts"] if p["t"] > old]
    live["rejected"] = [r for r in live["rejected"] if r["t"] > old]
    live["asked"] = {sid: a for sid, a in live["asked"].items() if a > old and sid in state["stories"]}
    # не дошло до Telegram — ещё раз, пока молния не опоздала
    for p in live["posts"]:
        if not p.get("sent") and p["t"] > (t - STALE).isoformat():
            send(p)
    rejected_list(state, live, now, tz)
    day = local_day(now, tz)
    if sum(1 for p in live["posts"] if p["day"] == day) >= PER_DAY:
        return
    found = signals(state, now, live["asked"])
    if not found:
        return
    import anthropic

    feedback = (translate.reports_api(None, "live") or {}).get("feedback", [])
    client = anthropic.Anthropic(max_retries=translate.API_RETRIES)
    answer, usage = stories.ask_model(client, PROMPT, request_of(state, vectors, found, live, now, tz, feedback),
                                      SCHEMA, EFFORT, MODEL, PRICE)
    spend("срочное", usage)
    night = is_night(now, tz)
    verdicts = []
    for a in answer.get("candidates", []):
        n = a.get("id", "")[1:]
        if not (n.isdigit() and int(n) < len(found)):
            continue
        sid, urls = found[int(n)]
        live["asked"][sid] = now
        level = a.get("level", "no")
        name = name_of(state, sid, urls)
        verdicts.append(f"{name[:50]} — {level}")
        go = level == "exceptional" or (level == "urgent" and not night)
        today = sum(1 for p in live["posts"] if p["day"] == day)
        if go and a.get("text", "").strip() and today < PER_DAY:
            chosen = links(state, vectors, urls, a.get("picks", []))
            p = {"id": f"{sid}-{t.astimezone(timezone.utc):%m%d%H%M}", "t": now, "day": day, "sid": sid, "name": name,
                 "level": level, "text": a["text"].strip(), "reason": a.get("reason", ""), "urls": chosen}
            p["html"] = compose(state, p["text"], chosen)
            live["posts"].append(p)
            send(p)
        else:
            why = a.get("reason", "") if not go else "ночь" if level == "urgent" else f"предел {PER_DAY} в день"
            live["rejected"].append({"t": now, "sid": sid, "name": name, "level": level, "reason": why})
    print(f"Live: {len(found)} со вспышкой ({'; '.join(verdicts)}), ≈ ${usage['usd']:.3f}"[:500], file=sys.stderr)


def rejected_list(state: dict, live: dict, now: str, tz) -> None:
    """Раз в день, первой сборкой после REJECTED_AT по Белграду, — что модель сочла несрочным с прошлого списка, с
    кнопкой «надо было» у каждой темы. Одна тема — одна строка, последняя причина."""
    local = datetime.fromisoformat(now).astimezone(tz)
    day = local.date().isoformat()
    if local.hour < REJECTED_AT or live.get("rejected_sent", "") >= day:
        return
    since = live.get("rejected_at", "")
    last = {}
    for r in live["rejected"]:
        if r["t"] > since:
            last[r["sid"]] = r
    items = sorted(last.values(), key=lambda r: r["t"])[-REJECTED_MAX:]
    rows = [{"id": f'{r["sid"]}-{datetime.fromisoformat(r["t"]).astimezone(timezone.utc):%m%d%H%M}', "name": r["name"],
             "reason": r["reason"], "time": datetime.fromisoformat(r["t"]).astimezone(tz).strftime("%H:%M")}
            for r in items]
    reply = translate.reports_api({"rejected": rows, "day": day}, "live")
    if reply and reply.get("ok"):
        live["rejected_sent"], live["rejected_at"] = day, now
        print(f"Live: список несрочного за день — {len(rows)} тем", file=sys.stderr)


if __name__ == "__main__":
    from pathlib import Path
    from zoneinfo import ZoneInfo

    root = Path(__file__).parent
    state = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else root / "cache" / "stories.json").read_text("utf-8"))
    tz = ZoneInfo("Europe/Belgrade")
    end = datetime.fromisoformat(state["built"])
    t = end - timedelta(days=4)
    asked, per_day = {}, {}
    while t <= end:
        now = t.isoformat()
        for sid, urls in signals(state, now, asked):
            asked[sid] = now
            day = local_day(now, tz)
            per_day.setdefault(day, []).append(
                f'{datetime.fromisoformat(now).astimezone(tz):%H:%M} {len({state["items"][u]["source"] for u in urls}):>2} '
                f'{"🌙" if is_night(now, tz) else "  "} {name_of(state, sid, urls)[:90]}')
        t += timedelta(minutes=15)
    for day, rows in per_day.items():
        print(f"\n{day}: {len(rows)} сигналов")
        print("\n".join(rows))
    found = signals(state, (end - timedelta(hours=10)).isoformat(), {})
    if found:
        print("\nвход модели (пример):\n" + request_of(state, {}, found[:2], {"posts": []}, (end - timedelta(hours=10)).isoformat(), tz,
                                                   [{"verdict": "no", "text": "пример", "reason": "просто заявление"}])[:3000])
