"""Проверка тем из топа — три раза в день (AUDIT_HOURS по Белграду), в первой сборке после каждого часа.

Топ — stories.SHOW_TOP тем с самой высокой за день горячестью среди тех, что хоть раз были на странице
(stories.hot); их горячесть отмечает каждая сборка (note_top). Модель (AUDIT_MODEL) читает новости, пришедшие в тему
с прошлой проверки (state["audit_seen"] — когда тему проверили), и отмечает те, что совсем не про её ситуацию.
Большие темы (PARTS_MIN_NEWS) она ещё и разбирает по составу: какие влитые в них части про другую ситуацию.
С 22.11 проверка сама исправляет: каждую находку Haiku сначала относит к подходящей теме (place) или в свою, и уверенные,
с которыми Haiku согласен, применяются сразу (_fix); кнопка «Вернуть» в отчёте отменяет исправление (_undo).
Отчёт — в Telegram через приёмник жалоб (path="stories", как итоги команд): доля ошибок, откуда они пришли
(раскладка, склейка, перепроверка…), что исправлено, кнопки для остального; раз в неделю — предложения по правилам
по ошибкам недели (state["audit_errors"]). Ничего не нашлось — отчёта нет. Итоги проверки — в
state["audits"][день T час]; находки — в state["audit_found"][день], с номерами подряд за день: кнопка отчёта
(«fix:день:находка», worker) знает только день. Уже отмеченные новости — в state["audited_news"].
До 18.11 проверка была раз в сутки и читала каждый изменившийся сюжет целиком (Opus, $0.3–0.65 за вечер).
"""
import json
from collections import Counter
from datetime import datetime, timedelta

import stories

AUDIT_HOURS = (11, 17, 23)  # по Белграду
AUDIT_MODEL, AUDIT_EFFORT = stories.SONNET, "medium"
AUDIT_PRICE = stories.PRICES[stories.SONNET]  # вход / выход за 1M токенов, $
AUDIT_KNOWN = 5  # сколько уже проверенных новостей темы модель видит для понимания, о чём она
AUDIT_BATCH = 200  # новостей в одном запросе: большой сюжет — один, мелкие — пачкой
AUDIT_DAYS = 14  # сколько дней итогов хранить — для сравнения с прошлой проверкой
MESSAGE = 1000  # приёмник жалоб режет сообщение до 1000 знаков
MESSAGES = 20  # и берёт не больше 20 за раз: отчёт длиннее уходит несколькими порциями
REPORT_MAX = 40  # больше сообщений за одну проверку не шлём
PLOT_URL = "https://vestomat.rs/plot/"  # страница темы (stories_live), #n<номер> — новость на ней
ADVICE_AT = (6, "23")  # советы по правилам — раз в неделю: воскресенье (weekday), проверка в 23 — по ошибкам за 7 дней
ADVICE_PER_FINDING = 5  # сколько новостей находки идёт в советы

AUDIT_PROMPT = f"""Ты проверяешь темы агрегатора новостей о Сербии перед показом читателям. {stories.RULES}

Вход — JSON: stories — темы, id вида S…: name — название темы, event — её ситуация (если определена), known — несколько её новостей, проверенных раньше (чтобы понять, о чём тема); news — новые новости темы, их и проверяешь: n — номер, t — время UTC, source — издание, what — что произошло.
Для каждой темы верни findings — новые новости, которым в ней не место: они про другую ситуацию, а общие с темой только участник, ведомство, город или область. Эпизоды, развитие, последствия, реакции и споры той же ситуации — на месте, даже если это отдельное событие.
У находки: kind — foreign; news — номера n (новости про одну и ту же чужую ситуацию — в одной находке); why — одной фразой, почему им здесь не место; sure — true, если повод новости явно другой (другое дело, событие или процесс) и с темой её связывают только участник, место или фон; false — если она может оказаться эпизодом, развитием или реакцией на ситуацию темы. Уверенные находки исправляются сами, без редактора. Всё на месте — пустой findings."""

# Разбор состава больших тем: тема собирается из частей (сюжетов, влитых склейкой), и проверка новых новостей не видит
# того, что влилось раньше. 22.11 в s9278 (заседание Скупщины, 1999 новостей, 73 части) лежали переговоры о составе
# правительства (s11927, s9723, s10816…) — при отдельной теме s10967 «Формирование правительства» —, годовой доклад
# Еврокомиссии и уборка граффити Чупичем. Модель видит каждую часть строкой: сколько новостей, когда, первая и последняя
PARTS_MIN_NEWS = 60  # тема с таким числом новостей — большая
PARTS_MIN = 3  # и хотя бы с таким числом частей
PARTS_SHOWN = 150  # частей в запросе — самые большие
AUTO_SHARE = 0.4  # сама выносит не больше этой доли новостей темы: больше — пусть посмотрит редактор

PARTS_PROMPT = f"""Ты проверяешь состав больших тем агрегатора новостей о Сербии. {stories.RULES}

Тема собрана из частей: часть — сюжет, который раскладка завела отдельно, а потом склейка или раскладка влила в тему. Разрастаясь, тема могла вобрать части про другую ситуацию — с теми же участниками, местом или фоном.
Вход — JSON: name — название темы, event — её ситуация, parts — части, id вида P…: news — сколько в ней новостей, from и to — даты первой и последней новости, what — что произошло в первой и в последней новости части; у ядра темы core — true, его не трогаешь.
Верни findings — части про другую ситуацию: другое дело, событие или процесс. Например, переговоры о составе правительства — другой процесс, чем учредительное заседание парламента; а выступление на том же заседании о другом — эпизод заседания, на месте. Части про одну и ту же чужую ситуацию — в одной находке. Эпизоды, развитие, последствия, реакции и споры ситуации темы — на месте.
У находки: parts — id частей; why — одной фразой, почему им здесь не место; sure — true, если ситуация явно другая и с темой её связывают только участники, место или фон; false — если это может быть эпизодом или развитием ситуации темы. Уверенные находки исправляются сами. Всё на месте — пустой findings."""

PARTS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"parts": {"type": "array", "items": {"type": "string"}},
                               "why": {"type": "string"}, "sure": {"type": "boolean"}},
                "required": ["parts", "why", "sure"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

# Куда переносить чужое: в тему, к ситуации которой оно относится, а не всегда в новую — 22.11 выплаты вкладчикам
# «Алта банки» лежали в теме «Поштанской штедионицы», а переговоры о правительстве — в теме заседания Скупщины, хотя
# у обеих ситуаций были свои темы. Решает Haiku по темам, похожим по словам; тема, где новости лежат сейчас, — среди
# кандидатов: выберет её — находку сама не исправляем (две модели разошлись)
PLACE_PROMPT = f"""Ты переносишь новости между темами агрегатора новостей о Сербии. {stories.RULES}

Проверка сочла группы новостей чужими в их теме. Вход — JSON: groups — группы, id вида G…: from — тема, где они сейчас, news — что произошло (несколько новостей группы), candidates — id тем, похожих по словам (from среди них); stories — эти темы, id вида S…: name — название, event — ситуация, news — несколько её новостей.
Для каждой группы: event — повод её новостей одной фразой; target — тема из её candidates, к ситуации которой они относятся (та же ситуация, её эпизод, развитие, последствие или реакция именно на неё), или пустая строка, если ни к одной — тогда у них будет своя тема. Если группа всё-таки про ситуацию темы from — target равен from. Общий участник, ведомство, место или область — ещё не повод выбрать тему."""

PLACE_SCHEMA = {
    "type": "object",
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "event": {"type": "string"}, "target": {"type": "string"}},
                "required": ["id", "event", "target"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["groups"],
    "additionalProperties": False,
}
PLACE_CANDIDATES = 5  # тем-кандидатов на группу, кроме своей
PLACE_WINDOW_H = 72  # кандидаты — темы, обновлявшиеся за столько часов
PLACE_SHOWN = 5  # сколько новостей группы видит модель
PLACE_BATCH = 15  # групп в одном запросе

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "stories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"kind": {"type": "string", "enum": ["foreign"]},
                                           "news": {"type": "array", "items": {"type": "integer"}},
                                           "why": {"type": "string"}, "sure": {"type": "boolean"}},
                            "required": ["kind", "news", "why", "sure"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "findings"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["stories"],
    "additionalProperties": False,
}

ADVICE_PROMPT = f"""Ты настраиваешь раскладку новостей по темам в агрегаторе новостей о Сербии. Правила темы сейчас: {stories.RULES}
Как новость попадает в тему: у видимой темы (от двух изданий) — карточка, ситуация одной фразой, её пишет Haiku и обновляет вместе с названием темы; раскладка (Haiku решает по новости и похожим темам с их карточками: join — в тему, new — новая тема; новость, почти дословно повторяющая уже разложенную, идёт за ней без модели; join с низким сходством или в тему страницы (30 самых горячих) перепроверяет Haiku: называет повод новости и выбирает, как он связан с ситуацией темы — та же ситуация, развитие или эпизод, последствие, реакция (остаётся) или новый случай того же рода, общий участник, другое (уходит в свою тему)), склейка дублей раз в два часа — только пары, где хоть одна тема на странице (Haiku сравнивает ситуации пары и выбирает тот же тип связи), ручные команды.
Вход — JSON: errors — ошибки, найденные проверкой: what — новость, story — тема, в которую она попала, why — почему ей там не место, path — как она туда попала.
Ошибки — за неделю; многие уже исправлены, но правило, которое их пропустило, осталось.
Верни advice — до трёх предложений: pattern — что повторяется в ошибках (не меньше трёх случаев в разных темах), change — что поменять и на каком шаге (правило, промпт раскладки или склейки, порог, перепроверка). Единичную ошибку без закономерности не обобщай. Закономерностей нет — пустой advice."""

ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "advice": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "change": {"type": "string"}},
                "required": ["pattern", "change"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["advice"],
    "additionalProperties": False,
}

PATHS = {"join": "раскладка", "same": "раскладка", "new": "раскладка", "alone": "раскладка", "unclear": "раскладка",
         "recheck": "перепроверка", "merge": "склейка", "line": "выделение", "move": "команда", "split": "команда",
         "unmerge": "команда", "copy": "копия", "audit": "проверка", "undo": "команда"}


def note_top(state: dict, day: str, heats: dict[str, float]) -> None:
    """Каждая сборка: сюжеты страницы (SHOW_TOP самых горячих) — в state["top"][день] с наибольшей горячестью за день.
    heats — горячесть видимых сюжетов. До 16.11 там был список сюжетов: их горячесть — 0."""
    top = state.setdefault("top", {})
    today = top.get(day, {})
    today = dict.fromkeys(today, 0.0) if isinstance(today, list) else today
    for sid in sorted(heats, key=heats.get, reverse=True)[:stories.SHOW_TOP]:
        today[sid] = round(max(today.get(sid, 0.0), heats[sid]), 2)
    state["top"] = {d: v for d, v in top.items() if d >= day} | {day: today}


def due(state: dict, now: str, tz) -> str:
    """Проверка, которую пора делать, — «день T час» (по Белграду: последний из AUDIT_HOURS, который уже наступил),
    или пустая строка: эту уже сделали, она у модели пакетом или в топе за день ещё никого нет."""
    local = datetime.fromisoformat(now).astimezone(tz)
    day = local.date().isoformat()
    hours = [h for h in AUDIT_HOURS if local.hour >= h]
    if not hours:
        return ""
    slot = f"{day}T{hours[-1]:02d}"
    sent = state.get("batches", {}).get("audit", {}).get("day") == slot  # уже у модели пакетом
    return slot if slot not in state.get("audits", {}) and state.get("top", {}).get(day) and not sent else ""


def arrived(it: dict, sid: str) -> str:
    """Когда новость попала в тему sid (раскладка, склейка, команда) — последний такой шаг её истории."""
    return max((h["t"] for h in it.get("history", []) if h["story"] == sid), default=it["seen"])


def _path(it: dict) -> tuple[str, str]:
    """Как новость попала в свой нынешний сюжет: (шаг для подсчёта, описание для модели)."""
    step = next((h for h in reversed(it.get("history", [])) if h["story"] == it["story"]), {"why": it["verdict"]})
    why = step["why"]
    if why == "merge":
        return "склейка", f"склейка: её сюжет {step.get('from')} влит" + (f", общее событие — {step['event']}" if step.get("event") else "")
    if why == "join":
        return "раскладка", f"раскладка: join, сходство {step.get('sim', 0):.3f}"
    if why == "copy":
        return "копия", f"копия: почти дословный пересказ новости №{step.get('of', '?')}, легла туда же без модели"
    return PATHS.get(why, why), PATHS.get(why, why) + f": {why}"


def todo(state: dict, slot: str) -> tuple[list[str], list[list[str]]]:
    """Что проверять: (темы топа за день, пачки тем для модели). Модель читает только новости, пришедшие в тему
    с прошлой проверки (audit_seen — когда её проверили); тему без новых новостей — нет. Пачка — до AUDIT_BATCH
    новых новостей, большая тема — одна."""
    seen = state.get("audit_seen", {})
    peaks = state["top"][slot[:10]]
    peaks = dict.fromkeys(peaks, 0.0) if isinstance(peaks, list) else peaks
    sids = sorted((sid for sid in peaks if sid in state["stories"]), key=peaks.get, reverse=True)[:stories.SHOW_TOP]
    fresh = {sid: len(new_news(state, sid, seen.get(sid, ""))) for sid in sids}
    chunks, size = [], 0
    for sid in sorted(sids, key=fresh.get):
        n = fresh[sid]
        if not n:
            continue
        if not chunks or size + n > AUDIT_BATCH:
            chunks.append([])
            size = 0
        chunks[-1].append(sid)
        size += n
    return sids, chunks


def new_news(state: dict, sid: str, since: str) -> list[str]:
    """Новости темы, пришедшие в неё после since (ISO; пустая строка — все)."""
    # до 18.11 в audit_seen было число новостей, а не время: такую тему проверяем целиком
    since = since if isinstance(since, str) else ""
    return [u for u in state["stories"][sid]["urls"] if arrived(state["items"][u], sid) > since]


def request(state: dict, chunk: list[str]) -> str:
    """Запрос проверки пачки тем: новые новости каждой и несколько уже проверенных. Системный промпт — AUDIT_PROMPT."""
    items, seen = state["items"], state.get("audit_seen", {})

    def row(u: str) -> dict:
        return {"n": items[u]["n"], "t": items[u]["seen"][5:16], "source": items[u]["source"],
                "what": items[u].get("what") or items[u]["title"]}

    out = []
    for k, sid in enumerate(chunk):
        new = new_news(state, sid, seen.get(sid, ""))
        known = [u for u in state["stories"][sid]["urls"] if u not in set(new)][-AUDIT_KNOWN:]
        out.append({"id": f"S{k}", "name": state.get("names", {}).get(sid, "")}
                   | ({"event": state["cards"][sid]} if sid in state.get("cards", {}) else {})
                   | {"known": [items[u].get("what") or items[u]["title"] for u in known],
                      "news": [row(u) for u in new]})
    return json.dumps({"stories": out}, ensure_ascii=False)


def parts_todo(state: dict, sids: list[str]) -> list[str]:
    """Большие темы топа, состав которых пора разобрать: в них влились части, которых прошлый разбор не видел."""
    seen = state.get("parts_seen", {})
    out = []
    for sid in sids:
        urls = state["stories"][sid]["urls"]
        parts = stories._parts(state, sid)
        if len(urls) >= PARTS_MIN_NEWS and len(parts) >= PARTS_MIN and not parts <= set(seen.get(sid, [])):
            out.append(sid)
    return out


def _core(state: dict, sid: str) -> str:
    """Ядро темы — её собственная часть, а нет её — самая большая."""
    sizes = Counter(state["items"][u]["part"] for u in state["stories"][sid]["urls"])
    return sid if sid in sizes else sizes.most_common(1)[0][0]


def parts_request(state: dict, sid: str) -> tuple[str, list[str]]:
    """Запрос разбора состава темы: (содержимое, части по порядку их id P…)."""
    items = state["items"]
    by_part = {}
    for u in state["stories"][sid]["urls"]:
        by_part.setdefault(items[u]["part"], []).append(items[u])
    core = _core(state, sid)
    order = sorted(by_part, key=lambda p: (p != core, -len(by_part[p])))[:PARTS_SHOWN]
    rows = []
    for k, p in enumerate(order):
        news = sorted(by_part[p], key=lambda it: it["seen"])
        what = list(dict.fromkeys((it.get("what") or it["title"]) for it in (news[0], news[-1])))
        rows.append({"id": f"P{k}", "news": len(news), "from": news[0]["seen"][5:10], "to": news[-1]["seen"][5:10],
                     "what": what} | ({"core": True} if p == core else {}))
    content = {"name": state.get("names", {}).get(sid, "")} | (
        {"event": state["cards"][sid]} if sid in state.get("cards", {}) else {}) | {"parts": rows}
    return json.dumps(content, ensure_ascii=False), order


def prepare(state: dict, slot: str, now: str) -> tuple[list[tuple], dict]:
    """Запросы проверки (новые новости тем топа пачками, затем разбор состава больших тем) и что понадобится finish."""
    sids, chunks = todo(state, slot)
    big = parts_todo(state, sids)
    calls = [(AUDIT_PROMPT, request(state, chunk), AUDIT_SCHEMA, AUDIT_EFFORT, AUDIT_MODEL, AUDIT_PRICE) for chunk in chunks]
    orders = {}
    for sid in big:
        content, orders[sid] = parts_request(state, sid)
        calls.append((PARTS_PROMPT, content, PARTS_SCHEMA, AUDIT_EFFORT, AUDIT_MODEL, AUDIT_PRICE))
    return calls, {"day": slot, "sids": sids, "chunks": chunks, "sizes": sizes(state, chunks, now),
                   "big": big, "orders": orders}


def finish(state: dict, sent: dict, answers: dict[int, dict], usd: float, ask, vectors: dict, now: str) -> dict:
    """Итоги проверки по ответам модели (answers — номер запроса → ответ; ответ мог прийти в другой сборке; сначала
    пачки новых новостей, за ними разбор состава sent["big"]): находки, куда их переносить (place), уверенные —
    исправить сразу (_fix), советы по правилам (сразу, ask(system, content, schema, effort, model, price)), итоги —
    в state["audits"][день T час]. usd — во что обошлись запросы."""
    slot, sids, chunks, sizes_ = sent["day"], sent["sids"], sent["chunks"], sent["sizes"]
    big, orders = sent.get("big", []), sent.get("orders", {})
    day = slot[:10]
    items, by_n = state["items"], {it["n"]: u for u, it in state["items"].items() if it.get("story")}
    reported = set(state.get("audited_news", []))
    sids = [sid for sid in sids if sid in state["stories"]]
    found = []
    for k, answer in answers.items():
        if k >= len(chunks):
            continue
        chunk = chunks[k]
        for s in answer["stories"]:
            j = s["id"][1:]
            if not (j.isdigit() and int(j) < len(chunk)):
                continue
            sid = chunk[int(j)]
            for f in s["findings"]:
                news = sorted({n for n in f["news"] if n in by_n and items[by_n[n]]["story"] == sid})
                if news and set(news) - reported:
                    found.append({"story": sid, "kind": f["kind"], "news": news, "why": f["why"].strip(), "sure": f["sure"]})
        for sid in chunk:  # проверена; темы пачки без ответа проверим в следующий раз
            state.setdefault("audit_seen", {})[sid] = sizes_[sid]["t"]
    news_total = sum(sizes_[sid]["n"] for k in answers if k < len(chunks) for sid in chunks[k])
    flagged = {n for f in found for n in f["news"]}
    paths = Counter(_path(items[by_n[n]])[0] for n in flagged)  # до исправлений: они меняют историю новостей

    parts_found = []  # разбор состава
    for j, sid in enumerate(big):
        answer = answers.get(len(chunks) + j)
        if answer is None or sid not in state["stories"]:
            continue
        order, core = orders[sid], _core(state, sid)
        state.setdefault("parts_seen", {})[sid] = sorted(order)
        for f in answer["findings"]:
            parts = sorted({order[int(p[1:])] for p in f["parts"] if p[1:].isdigit() and int(p[1:]) < len(order)} - {core})
            news = sorted(it["n"] for u in state["stories"][sid]["urls"] if (it := items[u])["part"] in parts)
            if news:
                parts_found.append({"story": sid, "kind": "foreign", "news": news, "parts": parts,
                                    "why": f["why"].strip(), "sure": f["sure"]})
    found += parts_found

    for f in found:  # как попали — до исправления: после него последний шаг истории — сама проверка
        f["came"] = dict(Counter(_path(items[by_n[n]])[0] for n in f["news"] if n in by_n))
    # ошибки — для советов по правилам раз в неделю (ADVICE_AT): по одной проверке закономерность не видна, а советы
    # в каждом отчёте повторялись и их не читали
    errors = state.setdefault("audit_errors", [])
    errors += [{"d": day, "what": items[by_n[n]].get("what") or items[by_n[n]]["title"],
                "story": state.get("names", {}).get(f["story"], f["story"]), "why": f["why"],
                "path": _path(items[by_n[n]])[1]} for f in found for n in f["news"][:ADVICE_PER_FINDING] if n in by_n]
    week = (datetime.fromisoformat(day) - timedelta(days=7)).date().isoformat()
    state["audit_errors"] = [e for e in errors if e["d"] > week]
    if found and vectors:
        usd += place(state, found, vectors, ask)
    fixed = 0
    for f in found:
        share = len(f["news"]) / max(len(state["stories"].get(f["story"], {}).get("urls", [])), 1)
        if f["sure"] and not f.get("stay") and share <= AUTO_SHARE:
            _fix(state, f, now, vectors)
            fixed += len(f["done"]["moves"])

    advice = []
    if slot[11:13] == ADVICE_AT[1] and datetime.fromisoformat(day).weekday() == ADVICE_AT[0] and len(state["audit_errors"]) >= 3:
        errors = [{k: v for k, v in e.items() if k != "d"} for e in state["audit_errors"]]
        answer, usage = ask(ADVICE_PROMPT, json.dumps({"errors": errors}, ensure_ascii=False), ADVICE_SCHEMA,
                            AUDIT_EFFORT, AUDIT_MODEL, AUDIT_PRICE)
        usd += usage["usd"]
        advice = answer["advice"]
    before = state.get("audit_found", {}).get(day, [])
    for k, f in enumerate(found, len(before) + 1):  # номера подряд за день: кнопка знает только день
        f["id"] = k
    state["audit_found"] = {d: v for d, v in sorted({**state.get("audit_found", {}), day: before + found}.items())[-AUDIT_DAYS:]}
    state["audited_news"] = sorted(reported | {n for f in found for n in f["news"]})
    state["audit_seen"] = {sid: n for sid, n in state.get("audit_seen", {}).items() if sid in state["stories"]}
    state["parts_seen"] = {sid: p for sid, p in state.get("parts_seen", {}).items() if sid in state["stories"]}
    state.setdefault("audits", {})[slot] = {"stories": len(sids), "news": news_total,
                                           "checked": sum(len(chunks[k]) for k in answers if k < len(chunks)),
                                           "flagged": len(flagged),
                                           "sure": len({n for f in found if f["sure"] and "parts" not in f for n in f["news"]}),
                                           "paths": dict(paths), "big": len(big),
                                           "parts": len(parts_found), "parts_news": sum(len(f["news"]) for f in parts_found),
                                           "fixed": fixed, "usd": round(usd, 4)}
    state["audits"] = dict(sorted(state["audits"].items())[-AUDIT_DAYS * len(AUDIT_HOURS):])
    return {"found": found, "advice": advice}


def run(state: dict, slot: str, ask, now: str, vectors: dict) -> dict:
    """Проверка сразу: запросы (prepare) — к модели один за другим, итоги — finish."""
    calls, sent = prepare(state, slot, now)
    answers, usd = {}, 0.0
    for k, call in enumerate(calls):
        answers[k], usage = ask(*call)
        usd += usage["usd"]
    return finish(state, sent, answers, usd, ask, vectors, now)


def sizes(state: dict, chunks: list[list[str]], now: str) -> dict[str, dict]:
    """Что увидит модель: тема → {"t": now, "n": сколько новых новостей}."""
    seen = state.get("audit_seen", {})
    return {sid: {"t": now, "n": len(new_news(state, sid, seen.get(sid, "")))} for c in chunks for sid in c}


def _seen_of(state: dict, sid: str, k: int = 3) -> list[str]:
    """Несколько новостей темы для модели: первая и последние."""
    urls = state["stories"][sid]["urls"]
    chosen = list(dict.fromkeys([urls[0]] + urls[-(k - 1):]))
    return list(dict.fromkeys(state["items"][u].get("what") or state["items"][u]["title"] for u in chosen))


def place(state: dict, found: list[dict], vectors: dict, ask) -> float:
    """Куда переносить находки: f["target"] — тема, к ситуации которой относятся её новости, или "" — в свою.
    Выбрала ту же тему, где они лежат, — f["stay"]: сама такую находку не исправляем. Возвращает расход, $."""
    import numpy as np

    items, by_n = state["items"], {it["n"]: u for u, it in state["items"].items() if it.get("story")}
    cutoff = max((s["updated"] for s in state["stories"].values()), default="")
    cutoff = (datetime.fromisoformat(cutoff) - timedelta(hours=PLACE_WINDOW_H)).isoformat() if cutoff else ""
    live, owners = [], []
    for sid, s in state["stories"].items():
        if s["updated"] >= cutoff and stories.visible(state, sid):
            for u in s["urls"][-stories.STORY_ITEMS:]:
                if u in vectors:
                    live.append(u)
                    owners.append(sid)
    if not live:
        return 0.0
    matrix = np.stack([vectors[u] for u in live])
    groups = []
    for f in found:
        urls = [by_n[n] for n in f["news"] if n in by_n]
        vs = [vectors[u] for u in urls if u in vectors][:30]
        if not vs:
            continue
        sims = (np.stack(vs) @ matrix.T).max(axis=0)
        best = {}
        for sim, sid in zip(sims, owners):
            if sid != f["story"] and sim >= stories.CANDIDATE_SIM and sim > best.get(sid, 0):
                best[sid] = float(sim)
        cands = [f["story"]] + sorted(best, key=best.get, reverse=True)[:PLACE_CANDIDATES]
        what = list(dict.fromkeys(items[u].get("what") or items[u]["title"] for u in urls))
        step = max(len(what) // PLACE_SHOWN, 1)
        groups.append((f, cands, what[::step][:PLACE_SHOWN]))
    usd = 0.0
    for start in range(0, len(groups), PLACE_BATCH):
        batch = groups[start:start + PLACE_BATCH]
        ids = list(dict.fromkeys(sid for _, cands, _ in batch for sid in cands))
        alias = {sid: f"S{k}" for k, sid in enumerate(ids)}
        content = {"groups": [{"id": f"G{k}", "from": alias[f["story"]], "news": what,
                               "candidates": [alias[c] for c in cands]} for k, (f, cands, what) in enumerate(batch)],
                   "stories": [{"id": alias[sid], "name": state.get("names", {}).get(sid, "")}
                               | ({"event": state["cards"][sid]} if sid in state.get("cards", {}) else {})
                               | {"news": _seen_of(state, sid)} for sid in ids]}
        answer, usage = ask(PLACE_PROMPT, json.dumps(content, ensure_ascii=False), PLACE_SCHEMA, stories.RECHECK_EFFORT,
                            stories.MODEL, None)
        usd += usage["usd"]
        back = {v: k for k, v in alias.items()}
        for g in answer["groups"]:
            j = g["id"][1:]
            if not (j.isdigit() and int(j) < len(batch)):
                continue
            f, cands, _ = batch[int(j)]
            target = back.get(g["target"].strip(), "")
            target = target if target in cands else ""
            f["event"] = g["event"].strip()
            if target == f["story"]:
                f["stay"] = True
            f["target"] = "" if target == f["story"] else target
    return usd


def _remedy(state: dict, finding: dict) -> tuple[str, str]:
    """Как убрать находку: есть тема, куда её нести (place), — ("to", тема); вся часть пришла склейкой —
    ("unmerge", часть); находка — ровно одна часть — ("split", часть); иначе ("move", "") — новости находки вместе
    в новый сюжет."""
    if finding.get("target") in state["stories"] and finding["target"] != finding["story"]:
        return "to", finding["target"]
    items = state["items"]
    news = set(finding["news"])
    parts = {it["part"] for it in items.values() if it.get("story") and it["n"] in news}
    if len(parts) > 1 and set(finding.get("parts", [])) == parts:  # разбор состава: несколько целых частей
        return "split", ",".join(sorted(parts))
    if len(parts) == 1:
        part = parts.pop()
        whole = {it["n"] for it in items.values() if it.get("story") == finding["story"] and it["part"] == part}
        if whole == news and part != finding["story"]:
            merged = any(m["gone"] == part for m in state.get("merges", []))
            return ("unmerge", part) if merged else ("split", part)
    return "move", ""


def _fix(state: dict, f: dict, now: str, vectors: dict | None = None) -> str:
    """Убрать находку: новости, всё ещё лежащие в её теме, и их копии — туда, куда велит _remedy. Что сделано —
    в f["done"] (откуда каждая новость ушла и какие запреты склейки добавлены): по нему _undo вернёт как было."""
    items, vectors = state["items"], vectors or {}
    by_n = {it["n"]: u for u, it in items.items() if it.get("story")}
    source = f["story"]
    urls = [by_n[n] for n in f["news"] if n in by_n and items[by_n[n]]["story"] == source]
    label = f"{f['id']}: " if "id" in f else ""
    if not urls:
        return f"{label}уже не в {source}"
    ns = {items[u]["n"] for u in urls}
    urls += [u for u in state["stories"][source]["urls"] if u not in urls and (h := items[u].get("history"))
             and h[-1]["why"] == "copy" and h[-1].get("of") in ns]  # копии идут за оригиналом
    moves = [[u, source, items[u]["part"]] for u in urls]
    apart = len(state.get("apart", []))
    how, part = _remedy(state, {**f, "news": [items[u]["n"] for u in urls]})
    if how == "to":
        whole = {p for p in {items[u]["part"] for u in urls}
                 if all(items[v]["part"] != p or v in urls for v in state["stories"][source]["urls"])}
        for u in urls:  # целые части переезжают как есть, отдельные новости — в ближайшую часть темы
            keep = items[u]["part"] if items[u]["part"] in whole else (
                stories._nearest_part(state, part, vectors[u], vectors) if u in vectors else part)
            stories.move(state, u, part, now, part=keep, why="audit")
        sid = part
        if source in state["stories"]:
            state.setdefault("apart", []).append([sorted(stories._parts(state, sid)), sorted(stories._parts(state, source))])
    elif how == "unmerge":
        sid = stories.unmerge(state, part, now)
    elif how == "split":
        sid = stories.split(state, part.split(","), now)
    else:
        sid = stories.move(state, urls[0], None, now, why="audit")
        for u in urls[1:]:
            stories.move(state, u, sid, now, part=sid, why="audit")
        if source in state["stories"]:
            state.setdefault("apart", []).append([[sid], sorted(stories._parts(state, source))])
    f["done"] = {"t": now, "how": how, "to": sid, "moves": moves, "apart": state.get("apart", [])[apart:]}
    return f"{label}{', '.join('№' + str(items[u]['n']) for u in urls)} → {sid}"


def _undo(state: dict, f: dict, now: str) -> str:
    """Вернуть исправленную находку: новости — в тему и часть, откуда их вынесли; запреты склейки, добавленные
    исправлением, — снять."""
    items, back = state["items"], []
    for u, story, part in f["done"]["moves"]:
        it = items.get(u)
        if it and it["story"] and it["story"] != story:
            stories.move(state, u, story, now, part=part, why="undo")
            back.append(it["n"])
    added = f["done"]["apart"]
    state["apart"] = [p for p in state.get("apart", []) if p not in added]
    f["undone"] = now
    if not back:
        return f"{f['id']}: возвращать нечего — новостей уже нет там, куда их вынесли"
    return f"{f['id']}: вернула {', '.join(f'№{n}' for n in back[:20])}{' и др.' if len(back) > 20 else ''} в {f['story']}"


def apply_fix(state: dict, day: str, which, now: str, vectors: dict | None = None) -> str:
    """Кнопка отчёта: which — номер находки дня или "sure" — все уверенные. Исправленную сама находку кнопка
    возвращает (_undo), остальные — исправляет (_fix). Находка, чьих новостей в теме уже нет (убрали руками,
    ушли склейкой), — мимо; часть новостей ушла — выносим оставшиеся. Вынесенное склейка обратно не вернёт
    (state["apart"]). Возвращает итог для ответа в Telegram."""
    found = state.get("audit_found", {}).get(day)
    if found is None:
        raise ValueError(f"находок за {day} нет: проверки не было или она уже забыта")
    if which == "sure":
        todo = [f for f in found if f["sure"] and "done" not in f]
    else:
        todo = [f for f in found if f["id"] == which]
    if not todo:
        raise ValueError("уверенных находок нет" if which == "sure" else f"находки {which} за {day} нет")
    done = []
    for f in todo:
        if "undone" in f:
            done.append(f"{f['id']}: уже вернули")
        elif "done" in f:
            done.append(_undo(state, f, now))
        else:
            done.append(_fix(state, f, now, vectors))
    return (f"находки {day[8:10]}.{day[5:7]}: " if which == "sure" else "") + "; ".join(done)


def report(state: dict, slot: str, result: dict) -> list[dict]:
    """Сообщения в Telegram ({"text", "buttons"}), коротко: итог одной строкой, исправленное (кнопка «Вернуть»),
    сомнительное (кнопка «Убрать», можно не трогать) — по строке на находку; раз в неделю — советы по правилам.
    Ничего не нашлось и советов нет — ничего не шлём. Кнопка шлёт боту fix:<день>:<номер находки>."""
    a, day = state["audits"][slot], slot[:10]
    by_n = {it["n"]: it for it in state["items"].values() if it.get("n") is not None}
    found = [f for f in result["found"] if all(n in by_n for n in f["news"])]  # новости могли забыться до отчёта
    messages = []
    if found:
        before = [v for d, v in sorted(state["audits"].items()) if d < slot]
        share = a["flagged"] / max(a["news"], 1) * 100
        head = (f"Проверка тем {day[8:10]}.{day[5:7]}, {slot[11:13]}:00 · ≈ ${a['usd']:.2f}\n"
                f"Прочитала {a['news']} новых новостей в {a['checked']} темах"
                + (f", разобрала состав больших ({a['big']})" if a.get("big") else "")
                + f". Чужих {a['flagged']} ({share:.1f}%"
                + (f", в прошлый раз {before[-1]['flagged'] / max(before[-1]['news'], 1) * 100:.1f}%" if before else "") + ").")
        done = [f for f in found if "done" in f]
        left = [f for f in found if "done" not in f]
        lines, k = [], 0  # (строка, кнопка или None, ссылка — отдельной строкой под ней)
        if done:
            lines.append(("\nИсправила сама — ошиблась, нажми «Вернуть»:", None, ""))
            for f in done:
                k += 1
                to = f["done"]["to"]
                where = f"«{_name(state, to)}»" if f["done"]["how"] == "to" else "отдельная тема"
                lines.append((f"{k}. «{_name(state, f['story'])}» → {where}: {_short(f, by_n)} — {f['why']}",
                               {"text": f"Вернуть {k}", "data": f"fix:{day}:{f['id']}"}, _link(state, f, by_n)))
        if left:
            lines.append(("\nНе уверена, оставила — можно не трогать:", None, ""))
            for f in left:
                k += 1
                doubt = " (раскладчик считает, что на месте)" if f.get("stay") else ""
                lines.append((f"{k}. «{_name(state, f['story'])}»: {_short(f, by_n)} — {f['why']}{doubt}",
                               {"text": f"Убрать {k}", "data": f"fix:{day}:{f['id']}"}, _link(state, f, by_n)))
        cur, buttons = head, []
        for line, button, link in lines:
            line = line[:MESSAGE // 2] + (f"\n   {link}" if link else "")  # обрезаем находку, ссылку — нет
            if len(cur) + len(line) + 1 > MESSAGE:
                messages.append({"text": cur, "buttons": _rows(buttons)})
                cur, buttons = line.lstrip("\n"), []
            else:
                cur += "\n" + line
            if button:
                buttons.append(button)
        messages.append({"text": cur, "buttons": _rows(buttons)})
    for k, tip in enumerate(result["advice"], 1):
        messages += [{"text": m} for m in _split(f"Совет по правилам за неделю, {k} из {len(result['advice'])}\n\n"
                                                 f"Что повторяется: {tip['pattern']}\n\nЧто поменять: {tip['change']}")]
    if len(messages) > REPORT_MAX:
        cut = len(messages) - REPORT_MAX + 1
        messages = messages[:REPORT_MAX - 1] + [{"text": f"…и ещё {cut} сообщений: больше {REPORT_MAX} за проверку не шлём."}]
    return messages


def _name(state: dict, sid: str) -> str:
    return state.get("names", {}).get(sid) or sid


def _short(f: dict, by_n: dict) -> str:
    """Новости находки коротко: до трёх — номерами, больше — первые три и сколько ещё."""
    shown = ", ".join(f"№{n}" for n in f["news"][:3])
    return shown if len(f["news"]) <= 3 else f"{shown} и ещё {len(f['news']) - 3}"


def _link(state: dict, f: dict, by_n: dict) -> str:
    """Ссылка на первую новость находки — на странице темы, где она лежит сейчас (исправленная уже переехала).
    У темы из одного издания страницы нет — тогда без ссылки."""
    sid = by_n[f["news"][0]]["story"]
    if sid not in state["stories"] or not stories.visible(state, sid):
        return ""
    return f"{PLOT_URL}{sid}/#n{f['news'][0]}"


def _rows(buttons: list[dict]) -> list[list[dict]]:  # по три в ряд
    return [buttons[k:k + 3] for k in range(0, len(buttons), 3)]


def _split(text: str) -> list[str]:
    """Длинное — на сообщения до MESSAGE знаков, по строкам."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > MESSAGE:
            out, cur = out + ([cur] if cur else []) + [line[:MESSAGE]], ""
            line = line[MESSAGE:]
        if len(cur) + len(line) + 1 > MESSAGE:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    return out + ([cur] if cur else [])
