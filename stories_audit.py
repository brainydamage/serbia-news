"""Ежедневная проверка сюжетов из топа — раз в сутки, в первой сборке после AUDIT_HOUR по Белграду.

Топ — stories.SHOW_TOP сюжетов с самой высокой за день горячестью среди тех, что хоть раз были на странице
(stories.hot); их горячесть отмечает каждая сборка (note_top). Цель — 99.9% новостей в них на своём месте.
Модель (AUDIT_MODEL) читает каждый такой сюжет целиком и отмечает новости, которым в нём не место: чужие и отдельные
события. Отчёт — в Telegram через приёмник жалоб (path="stories", как итоги команд): доля ошибок, откуда они пришли
(раскладка, склейка, перепроверка…), готовые команды для бота и предложения по правилам. Итоги дня — в
state["audits"], уже отмеченные новости — в state["audited_news"]: на следующий день их не повторяем, а сюжет,
не изменившийся с прошлой проверки, модель не читает заново (state["audit_seen"]).
"""
import json
from collections import Counter
from datetime import datetime

import stories

AUDIT_HOUR = 23  # по Белграду: поток новостей к этому времени уже небольшой
AUDIT_MODEL, AUDIT_EFFORT = "claude-opus-5-5", "medium"
AUDIT_PRICE = (4.0, 20.0)  # вход / выход за 1M токенов, $
AUDIT_BATCH = 200  # новостей в одном запросе: большой сюжет — один, мелкие — пачкой
AUDIT_DAYS = 14  # сколько дней итогов хранить — для сравнения со вчера
MESSAGE = 1000  # приёмник жалоб режет сообщение до 1000 знаков
MESSAGES = 20  # и берёт не больше 20 за раз: отчёт длиннее уходит несколькими порциями
REPORT_MAX = 60  # больше сообщений за вечер не шлём

AUDIT_PROMPT = f"""Ты проверяешь сюжеты агрегатора новостей о Сербии перед показом читателям. {stories.RULES}

Вход — JSON: stories — сюжеты, id вида S…: name — кодовое имя, event — событие, ради которого сюжет заведён (если определено; основная история сюжета — оно), news — новости по времени: n — номер, t — время UTC, source — издание, what — что произошло.
Для каждого сюжета верни findings — новости, которым в нём не место:
- kind foreign — новость не про историю сюжета: другой повод или другое дело, общие только участник, ведомство или тема;
- kind event — отдельное событие внутри сюжета: у него свой повод и о нём пишут как о самостоятельном. Подробности, реакции и последствия основной истории — не отдельное событие.
У находки: news — номера n её новостей (новости одного отдельного события — в одной находке), why — одной фразой, почему им здесь не место, sure — true, если уверен; false — если стоит посмотреть редактору. Основная история сюжета в находки не входит. Всё на месте — пустой findings."""

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
                            "properties": {"kind": {"type": "string", "enum": ["foreign", "event"]},
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

ADVICE_PROMPT = f"""Ты настраиваешь раскладку новостей по сюжетам в агрегаторе новостей о Сербии. Правила сюжета сейчас: {stories.RULES}
Как новость попадает в сюжет: у видимого сюжета (от двух изданий) — карточка события, одна фраза, её пишет Haiku один раз; раскладка (Haiku решает по новости и похожим сюжетам с их карточками: join — в сюжет, new — новый сюжет; новость, почти дословно повторяющая уже разложенную, идёт за ней без модели; join с низким сходством и в сюжет с карточкой перепроверяет Haiku, в сюжет страницы (30 самых горячих) — Sonnet: называет повод новости и выбирает, как он связан с событием сюжета — то же событие, развитие, последствие, реакция (остаётся) или новый случай того же рода, общая тема, другое (уходит в свой сюжет)), склейка дублей раз в два часа — только пары, где хоть один сюжет на странице (Sonnet по паре сюжетов сравнивает их поводы — карточку, а у сюжета без неё называет повод сам — и выбирает тот же тип связи; одиночка — сюжет из одной новости — тоже идёт в склейку), выделение отдельных событий из больших сюжетов, ручные команды.
Вход — JSON: errors — ошибки, найденные проверкой за день: what — новость, story — сюжет, в который она попала, why — почему ей там не место, path — как она туда попала.
Верни advice — до трёх предложений: pattern — что повторяется в ошибках, change — что поменять и на каком шаге (правило, промпт раскладки или склейки, порог, перепроверка). Единичную ошибку без закономерности не обобщай. Закономерностей нет — пустой advice."""

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
         "unmerge": "команда", "copy": "копия"}


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
    """День (по Белграду), за который пора проверять, или пустая строка."""
    local = datetime.fromisoformat(now).astimezone(tz)
    day = local.date().isoformat()
    sent = state.get("batches", {}).get("audit", {}).get("day") == day  # уже у модели пакетом
    return day if (local.hour >= AUDIT_HOUR and day not in state.get("audits", {}) and state.get("top", {}).get(day)
                   and not sent) else ""


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


def todo(state: dict, day: str) -> tuple[list[str], list[list[str]]]:
    """Что проверять за день: (сюжеты топа, пачки сюжетов для модели). Модель читает только сюжеты, изменившиеся
    с прошлой проверки (state["audit_seen"] — сколько в сюжете было новостей, когда его проверили): в неизменном
    находки уже были в прошлом отчёте. Пачка — до AUDIT_BATCH новостей, большой сюжет — один."""
    seen = state.get("audit_seen", {})
    peaks = state["top"][day]
    peaks = dict.fromkeys(peaks, 0.0) if isinstance(peaks, list) else peaks
    sids = sorted((sid for sid in peaks if sid in state["stories"]), key=peaks.get, reverse=True)[:stories.SHOW_TOP]
    chunks, size = [], 0
    for sid in sorted(sids, key=lambda s: len(state["stories"][s]["urls"])):
        n = len(state["stories"][sid]["urls"])
        if seen.get(sid) == n:
            continue
        if not chunks or size + n > AUDIT_BATCH:
            chunks.append([])
            size = 0
        chunks[-1].append(sid)
        size += n
    return sids, chunks


def request(state: dict, chunk: list[str]) -> str:
    """Запрос проверки пачки сюжетов. Системный промпт — AUDIT_PROMPT."""
    items = state["items"]
    return json.dumps({"stories": [
        {"id": f"S{k}", "name": state.get("names", {}).get(sid, "")}
        | ({"event": state["cards"][sid]} if sid in state.get("cards", {}) else {})
        | {"news": [{"n": items[u]["n"], "t": items[u]["seen"][5:16], "source": items[u]["source"],
                   "what": items[u].get("what") or items[u]["title"]} for u in state["stories"][sid]["urls"]]}
        for k, sid in enumerate(chunk)]}, ensure_ascii=False)


def finish(state: dict, day: str, sids: list[str], chunks: list[list[str]], sizes: dict[str, int],
           answers: dict[int, dict], usd: float, ask) -> dict:
    """Итоги дня по ответам модели на пачки (answers — номер пачки → ответ; ответ мог прийти в другой сборке):
    находки, советы по правилам (сразу, ask(system, content, schema, effort, model, price)), итоги — в
    state["audits"][day]. sizes — сколько новостей было в сюжете, когда его отдали модели; usd — во что обошлись
    пачки."""
    items, by_n = state["items"], {it["n"]: u for u, it in state["items"].items() if it.get("story")}
    reported = set(state.get("audited_news", []))
    sids = [sid for sid in sids if sid in state["stories"]]
    found = []
    for k, answer in answers.items():
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
        for sid in chunk:  # проверен; сюжеты пачки без ответа проверим в следующий раз
            state.setdefault("audit_seen", {})[sid] = sizes[sid]

    news_total = sum(len(state["stories"][sid]["urls"]) for sid in sids)
    flagged = {n for f in found for n in f["news"]}
    paths = Counter(_path(items[by_n[n]])[0] for n in flagged)
    advice = []
    if found:
        errors = [{"what": items[by_n[n]].get("what") or items[by_n[n]]["title"], "story": state.get("names", {}).get(f["story"], f["story"]),
                   "why": f["why"], "path": _path(items[by_n[n]])[1]} for f in found for n in f["news"]]
        answer, usage = ask(ADVICE_PROMPT, json.dumps({"errors": errors}, ensure_ascii=False), ADVICE_SCHEMA,
                            AUDIT_EFFORT, AUDIT_MODEL, AUDIT_PRICE)
        usd += usage["usd"]
        advice = answer["advice"]
    for k, f in enumerate(found, 1):
        f["id"] = k
    state["audit_found"] = {d: v for d, v in sorted({**state.get("audit_found", {}), day: found}.items())[-AUDIT_DAYS:]}
    state["audited_news"] = sorted(reported | flagged)
    state["audit_seen"] = {sid: n for sid, n in state.get("audit_seen", {}).items() if sid in state["stories"]}
    state.setdefault("audits", {})[day] = {"stories": len(sids), "news": news_total,
                                           "checked": sum(len(chunks[k]) for k in answers), "flagged": len(flagged),
                                           "sure": len({n for f in found if f["sure"] for n in f["news"]}),
                                           "paths": dict(paths), "usd": round(usd, 4)}
    state["audits"] = dict(sorted(state["audits"].items())[-AUDIT_DAYS:])
    return {"found": found, "advice": advice}


def run(state: dict, day: str, ask) -> dict:
    """Проверка дня сразу: пачки (todo) — к модели одна за другой, итоги — finish."""
    sids, chunks = todo(state, day)
    answers, usd = {}, 0.0
    for k, chunk in enumerate(chunks):
        answers[k], usage = ask(AUDIT_PROMPT, request(state, chunk), AUDIT_SCHEMA, AUDIT_EFFORT, AUDIT_MODEL, AUDIT_PRICE)
        usd += usage["usd"]
    return finish(state, day, sids, chunks, {sid: len(state["stories"][sid]["urls"]) for c in chunks for sid in c},
                  answers, usd, ask)


def _remedy(state: dict, finding: dict) -> tuple[str, str]:
    """Как убрать находку: вся часть пришла склейкой — ("unmerge", часть); находка — ровно одна часть — ("split",
    часть); иначе ("move", "") — новости находки вместе в новый сюжет."""
    items = state["items"]
    news = set(finding["news"])
    parts = {it["part"] for it in items.values() if it.get("story") and it["n"] in news}
    if len(parts) == 1:
        part = parts.pop()
        whole = {it["n"] for it in items.values() if it.get("story") == finding["story"] and it["part"] == part}
        if whole == news and part != finding["story"]:
            merged = any(m["gone"] == part for m in state.get("merges", []))
            return ("unmerge", part) if merged else ("split", part)
    return "move", ""


def _remedy_text(state: dict, finding: dict) -> str:
    """Что сделает кнопка находки — словами."""
    how, part = _remedy(state, finding)
    if how == "unmerge":
        return f"отменит склейку {part} — её новости снова отдельный сюжет"
    if how == "split":
        return f"вынесет часть {part} в свой сюжет"
    news = ", ".join(f"№{n}" for n in finding["news"])
    return f"вынесет {news} в новый сюжет" + (" — вместе" if len(finding["news"]) > 1 else "")


def apply_fix(state: dict, day: str, which, now: str) -> str:
    """Кнопка отчёта: which — номер находки дня или "sure" — все уверенные. Находка, чьих новостей в сюжете уже нет
    (убрали руками, ушли склейкой), — мимо; часть новостей ушла — выносим оставшиеся. Вынесенное склейка обратно
    не вернёт (state["apart"]). Возвращает итог для ответа в Telegram."""
    found = state.get("audit_found", {}).get(day)
    if found is None:
        raise ValueError(f"находок за {day} нет: проверки не было или она уже забыта")
    todo = [f for f in found if f["sure"]] if which == "sure" else [f for f in found if f["id"] == which]
    if not todo:
        raise ValueError("уверенных находок нет" if which == "sure" else f"находки {which} за {day} нет")
    by_n = {it["n"]: u for u, it in state["items"].items() if it.get("story")}
    done = []
    for f in todo:
        urls = [by_n[n] for n in f["news"] if n in by_n and state["items"][by_n[n]]["story"] == f["story"]]
        if not urls:
            done.append(f"{f['id']}: уже не в {f['story']}")
            continue
        how, part = _remedy(state, {**f, "news": [state["items"][u]["n"] for u in urls]})
        if how == "unmerge":
            sid = stories.unmerge(state, part, now)
        elif how == "split":
            sid = stories.split(state, [part], now)
        else:
            sid = stories.move(state, urls[0], None, now)
            for u in urls[1:]:
                stories.move(state, u, sid, now, part=sid)
            if f["story"] in state["stories"]:
                state.setdefault("apart", []).append([[sid], sorted(stories._parts(state, f["story"]))])
        done.append(f"{f['id']}: {', '.join('№' + str(state['items'][u]['n']) for u in urls)} → {sid}")
    return (f"находки {day[8:10]}.{day[5:7]}: " if which == "sure" else "") + "; ".join(done)


def report(state: dict, day: str, result: dict) -> list[dict]:
    """Сообщения в Telegram ({"text", "buttons"}): итоги с подсказкой, как читать, и кнопкой «убрать все уверенные»;
    по сообщению на сюжет — пронумерованные находки, под сообщением — кнопка на каждую; советы по правилам.
    Режем между находками, а не посреди. Кнопка шлёт боту fix:<день>:<номер находки или sure>."""
    a = state["audits"][day]
    names = state.get("names", {})
    by_n = {it["n"]: it for it in state["items"].values() if it.get("story")}
    share = a["flagged"] / max(a["news"], 1) * 100
    before = [v for d, v in sorted(state["audits"].items()) if d < day]
    paths = sorted(a["paths"].items(), key=lambda kv: -kv[1])
    by_story = {}
    for f in result["found"]:  # сюжет и новости могли забыться между проверкой и отчётом
        if f["story"] in state["stories"] and all(n in by_n for n in f["news"]):
            by_story.setdefault(f["story"], []).append(f)
    sure = [f for found in by_story.values() for f in found if f["sure"]]
    head = [f"Проверка сюжетов за {day[8:10]}.{day[5:7]}",
            "",
            f"В топе за день было {a['stories']} сюжетов, в них {a['news']} новостей. Лишних нашлось {a['flagged']} "
            f"({share:.1f}%), цель — не больше 0.1%."
            + (f" Заново прочитаны только изменившиеся с прошлой проверки ({a['checked']}), в остальных находки были в прошлых отчётах."
               if a.get("checked", a["stories"]) < a["stories"] else "")
            + (f" В прошлый раз было {before[-1]['flagged'] / max(before[-1]['news'], 1) * 100:.1f}%." if before else ""),
            *([f"Откуда лишние: {', '.join(f'{k} — {v}' for k, v in paths)}."] if paths else []),
            "",
            "Как читать: дальше по сообщению на сюжет. У каждой находки — что не так, номера новостей (как на /plot/) "
            "и что сделает её кнопка под сообщением. «Уверенно» — проверяющий не сомневается, «посмотри» — реши сам; так подписаны "
            "и кнопки. "
            + (f"Кнопка ниже убирает все {len(sure)} уверенных разом — тогда их отдельные кнопки пропадут. " if sure else "")
            + "Нажатие — команда боту: применится в ближайшей сборке, бот ответит, что вышло. В конце — советы, какие правила поменять.",
            "",
            f"Проверка стоила ≈ ${a['usd']:.2f}."]
    messages = [{"text": "\n".join(head),
                 "buttons": [[{"text": f"Убрать все уверенные ({len(sure)})", "data": f"fix:{day}:sure"}]] if sure else []}]
    for sid, found in sorted(by_story.items(), key=lambda kv: -sum(len(f["news"]) for f in kv[1])):
        title = f"{sid} «{names.get(sid, '')}» ({len(state['stories'][sid]['urls'])} новостей)"
        blocks = []
        for k, f in enumerate(sorted(found, key=lambda f: not f["sure"]), 1):
            kind = "Чужая" if f["kind"] == "foreign" else "Отдельное событие"
            how = Counter(_path(by_n[n])[0] for n in f["news"])
            came = ", ".join(f"{p} ({c})" if len(how) > 1 else p for p, c in how.most_common())
            news = ", ".join(f"№{n}" for n in f["news"])
            block = (f"{k}. {kind}, {'уверенно' if f['sure'] else 'посмотри'}: {f['why']} — {news}\n"
                     f"{'Как попала' if len(f['news']) == 1 else 'Как попали'}: {came}. "
                     f"Кнопка «Убрать {k}» {_remedy_text(state, f)}.")
            # у уверенной находки в данных кнопки «:s»: по «убрать все уверенные» worker снимает такие кнопки со всех
            # сообщений отчёта
            blocks.append((block[:MESSAGE - 100],
                           {"text": f"Убрать {k} — {'уверенно' if f['sure'] else 'посмотри'}",
                            "data": f"fix:{day}:{f['id']}" + (":s" if f["sure"] else "")}))
        cur, buttons = title, []
        for block, button in blocks:
            if len(cur) + len(block) + 2 > MESSAGE:
                messages.append({"text": cur, "buttons": _rows(buttons)})
                cur, buttons = f"{sid} (продолжение)", []
            cur += "\n\n" + block
            buttons.append(button)
        messages.append({"text": cur, "buttons": _rows(buttons)})
    for k, tip in enumerate(result["advice"], 1):
        messages += [{"text": m} for m in _split(f"Совет по правилам {k} из {len(result['advice'])}\n\n"
                                                 f"Что повторяется: {tip['pattern']}\n\nЧто поменять: {tip['change']}")]
    if len(messages) > REPORT_MAX:
        cut = len(messages) - REPORT_MAX + 1
        messages = messages[:REPORT_MAX - 1] + [{"text": f"…и ещё {cut} сообщений: больше {REPORT_MAX} за вечер не шлём."}]
    return messages


def _rows(buttons: list[dict]) -> list[list[dict]]:  # по две: «Убрать 1 — посмотри» длинная, четыре в ряд не влезут
    return [buttons[k:k + 2] for k in range(0, len(buttons), 2)]


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
