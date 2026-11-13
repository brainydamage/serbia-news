"""Сюжеты в живой сборке — теневой режим: build.py раскладывает новости архива по сюжетам (stories.py) и пишет
скрытую страницу site/plot/ (vestomat.rs/plot/) — посмотреть, как это было бы на сайте. На главную страницу сюжеты пока не идут.

Состояние — cache/stories.json, векторы новостей живых сюжетов — cache/story-vectors.npz; оба в кэше GitHub Actions
вместе с архивом. Расход по дням копится в состоянии и виден на странице.

Страница — только новости с START, и вся она — по одним правилам: правила раскладки и склейки (константы stories.py)
изменились — сборка выбрасывает раскладку и раскладывает заново все новости с START (последние stories.LIVE часов),
по сборкам, в которых они появились, как будто новые правила были с самого начала. Так же — если пропал кэш.
Прежняя страница сюжетов-тем (до 12.11 18:47) — замороженный снимок static/plot1/.

Править раскладку руками — командами Telegram-боту (worker/): он дописывает их в stories-commands.json, коммит
запускает сборку, сборка применяет новые команды (apply_commands) и отвечает в Telegram, что вышло:
    /move 1234 s471   — новость №1234 в сюжет s471 (new — в новый сюжет)
    /split s191 s298  — части s191 и s298 в отдельный сюжет
    /unmerge s102     — отменить склейку: части, которые принёс s102, снова сюжет s102

    .venv/bin/python stories_live.py [архив.json]   — локально: сборка сюжетов на архиве (по умолчанию
                                                      cache/archive.json) и страница site/plot/
"""
import hashlib
import html
import json
import os
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

import stories
import translate

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "cache" / "stories.json"
VECTORS_FILE = ROOT / "cache" / "story-vectors.npz"
PAGE_DIR = ROOT / "site" / "plot"
COMMANDS_FILE = ROOT / "stories-commands.json"
COMMANDS_SHOWN = 30  # последних команд с итогом — на странице
# начало новой страницы: 12.11 17:05 по Белграду — с этой сборки у новостей части, история переносов и номера для бота
START = "2026-11-12T16:05:00+00:00"
# новость без перевода ждёт одну сборку (перевод мог опоздать), потом идёт по оригинальному заголовку
WAIT_TRANSLATION = timedelta(minutes=20)
# константы stories.py, которые не меняют раскладку, — не в отпечатке: их правка не раскладывает всё заново
NOT_RULES = {"LINES_STEP"}
# отпечаток до 13.11, когда перепроверку больших сюжетов перевели с роста в 1.5 раза на LINES_STEP: остальные
# правила те же — раскладку не пересобираем
SAME_RULES = {"93f151fd03"}
# сюжет, который не обновлялся дольше, убираем из состояния вместе с его новостями: архив их уже забыл
# (ARCHIVE_DAYS в build.py — 4 дня), так что заново раскладывать их не придётся
FORGET = timedelta(days=5)
SPENT_DAYS = 14
PAGE_WINDOW = timedelta(hours=24)  # на странице — видимые сюжеты, обновлявшиеся за это время
# порядок сюжетов — по горячести: каждое издание даёт сюжету очко за свою последнюю новость в нём, очко вдвое тает
# за HEAT_HALF_LIFE. Свежий сюжет на 5 изданий выше вчерашнего на 25, одна реплика большой сюжет не поднимает,
# а новый поворот, о котором пишут многие, — поднимает
HEAT_HALF_LIFE = timedelta(hours=6)
# заголовок сюжета — из его новостей за столько часов до последней: самая типичная за всё время 13.11 держала
# анонс «пройдут консультации», когда они давно прошли
HEADLINE_RECENT = timedelta(hours=6)
PROGRESS_FROM = 200  # с стольких новостей за сборку (раскладка заново) — прогресс в лог
LOCAL_TZ = ZoneInfo("Europe/Belgrade")
# кодовые имена видимых сюжетов — чтобы узнавать сюжет по ссылке, а не по номеру. Имя даётся один раз и не меняется:
# его ценность в том, что оно постоянно. На раскладку не влияет, поэтому и не в отпечатке правил
NAME_PROMPT = """Ты даёшь сюжетам агрегатора новостей о Сербии кодовые имена — чтобы редактор узнавал сюжет по ссылке.
Вход — JSON: stories — сюжеты, id вида S…, у каждого несколько новостей (что произошло).
Для каждого сюжета верни name — не больше трёх слов по-русски: главный участник и предмет, по которым сюжет легко узнать и отличить от соседних. Имя — для сюжета целиком, его общего события, а не подробности одной из новостей. Без кавычек и точки в конце."""
NAME_SCHEMA = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
                "required": ["id", "name"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["names"],
    "additionalProperties": False,
}
NAME_BATCH = 30
NAME_GROW = 3  # имя — заново, когда сюжет вырос во столько раз с тех пор, как его назвали
NAME_SHOWN = 6  # сколько новостей сюжета видит модель: первая и самые типичные


def load_state() -> tuple[dict, dict]:
    try:
        state = json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return stories.new_state(), {}
    try:
        z = np.load(VECTORS_FILE)
        vectors = dict(zip(json.loads(str(z["urls"])), z["vectors"].astype(np.float32)))
    except (OSError, ValueError, KeyError):
        vectors = {}
    return state, vectors


def save_state(state: dict, vectors: dict) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    urls = list(vectors)
    np.savez(VECTORS_FILE, urls=np.array(json.dumps(urls)),
             vectors=np.stack([vectors[u] for u in urls]).astype(np.float16) if urls else np.zeros((0, 0), np.float16))


def forget_old(state: dict, vectors: dict, now: str) -> None:
    """Убирает давно не обновлявшиеся сюжеты вместе с векторами их новостей."""
    cutoff = (datetime.fromisoformat(now) - FORGET).isoformat()
    for sid in [s for s, story in state["stories"].items() if story["updated"] < cutoff]:
        for u in state["stories"].pop(sid)["urls"]:
            state["items"].pop(u, None)
    # новости без сюжета (до 12.11 так оставались «непонятные», unclear): забываем по времени
    state["items"] = {u: it for u, it in state["items"].items() if it["story"] or it["seen"] >= cutoff}
    state["checked"] = {k: v for k, v in state.get("checked", {}).items()
                        if all(s in state["stories"] for s in k.split("|"))}
    state.pop("merged", None)  # кто в кого склеился — только для разбора прогонов; журнал склеек — merges
    # до 13.11 у имени срезалась и закрывающая кавычка названия внутри: «Разрешение на «Югославию»
    state["names"] = {sid: name + "»" * (name.count("«") - name.count("»"))
                      for sid, name in state.get("names", {}).items() if sid in state["stories"]}
    state["named_at"] = {sid: n for sid, n in state.get("named_at", {}).items() if sid in state["stories"]}
    state.pop("names_whole", None)  # прежняя разовая чистка обрезанных имён; теперь имена обновляются, когда сюжет вырос
    state["lines"] = {sid: v for sid, v in state.get("lines", {}).items() if sid in state["stories"]}
    state["spun"] = {sid: p for sid, p in state.get("spun", {}).items() if sid in state["stories"]}
    # события, выделенные из одного сюжета до 13.11, склейка могла соединить друг с другом — теперь не соединит
    for a, parent in state["spun"].items():
        for b, other in state["spun"].items():
            if a < b and parent == other and not stories._apart(state, a, b):
                state.setdefault("apart", []).append([sorted(stories._parts(state, a)), sorted(stories._parts(state, b))])
    # журнал склеек и ручные разделения — пока живы их части
    parts = {it["part"] for it in state["items"].values()}
    state["merges"] = [m for m in state.get("merges", []) if parts & set(m["parts"])]
    state["apart"] = [pair for pair in state.get("apart", []) if parts & set(pair[0]) and parts & set(pair[1])]
    for u in set(vectors) - set(state["items"]):
        del vectors[u]


def apply_commands(state: dict, vectors: dict, now: str) -> list[dict]:
    """Новые команды из stories-commands.json (id больше state["commands_done"]): правка раскладки без модели.
    Итог каждой — в result; последние COMMANDS_SHOWN — в state["commands"] для страницы. Возвращает применённые."""
    try:
        commands = json.loads(COMMANDS_FILE.read_text("utf-8"))["commands"]
    except (OSError, ValueError, KeyError):
        return []
    fresh = [c for c in commands if c["id"] > state.get("commands_done", 0)]
    by_n = {it["n"]: u for u, it in state["items"].items() if it.get("story")}
    for c in fresh:
        try:
            c["result"] = "✓ " + _apply(state, vectors, now, c, by_n)
        except (ValueError, KeyError, StopIteration) as exc:
            c["result"] = f"✗ {exc}" if str(exc) else f"✗ {type(exc).__name__}"
        state["commands_done"] = c["id"]
        print(f"Сюжеты: команда {c['id']} {c['do']}: {c['result']}", file=sys.stderr)
    state["commands"] = (state.get("commands", []) + [{**c, "applied": now} for c in fresh])[-COMMANDS_SHOWN:]
    return fresh


def _apply(state: dict, vectors: dict, now: str, c: dict, by_n: dict) -> str:
    """Одна команда; ошибка — ValueError с текстом для ответа в Telegram."""
    stories_ = state["stories"]
    if c["do"] == "move":
        url = by_n.get(c["news"])
        if not url:
            raise ValueError(f"новости №{c['news']} нет: забыта или ещё не разложена")
        old = state["items"][url]["story"]
        if c["to"] == "new":
            sid = stories.move(state, url, None, now)
            if old in stories_:  # новая история: склейка не вернёт её туда, откуда сняли
                state.setdefault("apart", []).append([[sid], sorted(stories._parts(state, old))])
            return f"№{c['news']}: {old} → новый сюжет {sid}"
        if c["to"] not in stories_:
            raise ValueError(f"сюжета {c['to']} нет")
        if c["to"] == old:
            return f"№{c['news']} уже в {old}"
        part = stories._nearest_part(state, c["to"], vectors[url], vectors) if url in vectors else c["to"]
        stories.move(state, url, c["to"], now, part=part)
        return f"№{c['news']}: {old} → {c['to']} (часть {part})"
    if c["do"] == "split":
        known = {it["part"] for it in state["items"].values() if it.get("story")}
        missing = [p for p in c["parts"] if p not in known]
        if missing:
            raise ValueError(f"частей {', '.join(missing)} нет")
        olds = sorted({it["story"] for it in state["items"].values() if it.get("story") and it["part"] in c["parts"]})
        sid = stories.split(state, c["parts"], now)
        return f"{', '.join(c['parts'])}: {', '.join(olds)} → свой сюжет {sid}"
    if c["do"] == "unmerge":
        if not any(m["gone"] == c["story"] for m in state.get("merges", [])):
            raise ValueError(f"склейки {c['story']} в журнале нет")
        sid = stories.unmerge(state, c["story"], now)
        return f"склейка {c['story']} отменена: снова сюжет {sid}"
    raise ValueError(f"не знаю команду {c['do']}")


def ask_with_spending(state: dict, day: str):
    """ask для stories.update и stories.merge: запрос к модели, расход — в state["spent"][день]."""
    import anthropic

    client = anthropic.Anthropic(max_retries=translate.API_RETRIES)
    spent = state.setdefault("spent", {})

    def ask(system: str, content: str, schema: dict, effort: str = stories.EFFORT,
            model: str = stories.MODEL) -> tuple[dict, dict]:
        answer, usage = stories.ask_model(client, system, content, schema, effort, model)
        total = spent.setdefault(day, {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
        total["calls"] += 1
        for k in ("in", "out", "usd", "read"):  # read — часть входа из кэша
            total[k] = round(total.get(k, 0) + usage[k], 6)
        return answer, usage

    return ask


def name_stories(state: dict, vectors: dict, ask) -> int:
    """Кодовые имена видимым сюжетам (Haiku, пачками): тем, у кого имени ещё нет, и тем, кто с тех пор вырос в
    NAME_GROW раз — имя по первым новостям бывает про одну из них (12.11 матч Сербия — Германия назвался по прогнозу
    одного букмекера). Возвращает, скольким дали имя."""
    names = state.setdefault("names", {})
    named_at = state.setdefault("named_at", {})  # сюжет → сколько в нём было новостей, когда его назвали
    todo = [sid for sid, story in state["stories"].items() if stories.visible(state, sid)
            and (sid not in names or len(story["urls"]) >= NAME_GROW * named_at.get(sid, 2))]
    for start in range(0, len(todo), NAME_BATCH):
        chunk = todo[start:start + NAME_BATCH]
        content = json.dumps({"stories": [{"id": f"S{n}", "news": stories._shown(state, sid, vectors, NAME_SHOWN)}
                                          for n, sid in enumerate(chunk)]}, ensure_ascii=False)
        answer, _ = ask(NAME_PROMPT, content, NAME_SCHEMA)
        for named in answer["names"]:
            n = named["id"][1:]
            if n.isdigit() and int(n) < len(chunk) and named["name"].strip():
                name, sid = stories.clean_name(named["name"]), chunk[int(n)]
                # длинное — по словам, не посреди слова
                names[sid] = name if len(name) <= 50 else name[:50].rsplit(" ", 1)[0]
                named_at[sid] = len(state["stories"][sid]["urls"])
    return len(todo)


def fingerprint() -> str:
    """Отпечаток правил: правила, промпты, модели и пороги stories.py (константы), LAYOUT_VERSION — для логики."""
    # множество — отсортированным: его repr меняет порядок от запуска к запуску, и каждая вторая сборка раскладывала заново
    rules = {k: repr(sorted(v) if isinstance(v, (set, frozenset)) else v) for k, v in sorted(vars(stories).items())
             if k.isupper() and k not in NOT_RULES}
    return hashlib.sha1(json.dumps(rules, ensure_ascii=False).encode()).hexdigest()[:10]


def run(archive: dict, sections: dict) -> None:
    """Одна сборка: новые новости архива — в сюжеты, раз в час склейка, потом страница. sections — {id: название}."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state, vectors = load_state()
    rebuilt = ""
    if state.get("fingerprint") in SAME_RULES:
        state["fingerprint"] = fingerprint()
    if state.get("fingerprint") != fingerprint() or state.get("start") != START:
        rebuilt = "правила изменились" if state.get("start") == START else "новое начало" if state.get("start") else "начало"
        # номера новостей и сюжетов дальше растут, а не начинаются заново: старый №1234 из Telegram не попадёт в другую
        # новость. Применённые команды заново не применяем: к новой раскладке они могут не подходить
        keep = {k: state[k] for k in ("spent", "next_n", "next_id", "commands_done", "commands") if k in state}
        state = {**stories.new_state(), **keep, "start": START, "fingerprint": fingerprint(), "rebuilt": now}
    since = max(START, (datetime.fromisoformat(now) - stories.LIVE).isoformat())
    wait = (datetime.fromisoformat(now) - WAIT_TRANSLATION).isoformat()
    items = [(u, a) for u, a in archive.items()
             if u not in state["items"] and stories.eligible(a) and a["seen"] >= since
             and (a.get("ru") or a.get("what") or a["seen"] < wait)]
    items.sort(key=lambda x: x[1]["seen"])
    print(f"Сюжеты: новых новостей {len(items)}{f' (раскладываем заново: {rebuilt})' if rebuilt else ''}", file=sys.stderr)

    # векторы всех новостей в памяти: для раскладки нужны последние новости живых сюжетов, а для страницы — все
    need = [u for u, _ in items] + [u for u in state["items"] if u not in vectors]
    texts = {u: stories.text_of(archive.get(u) or state["items"][u]) for u in need}
    if need:
        t = time.monotonic()
        for u, v in zip(need, stories.embed([texts[u] for u in need])):
            vectors[u] = v
        if len(need) >= PROGRESS_FROM:
            print(f"Сюжеты: векторы для {len(need)} новостей — {time.monotonic() - t:.0f} с", file=sys.stderr)
    done = apply_commands(state, vectors, now)

    day = datetime.fromisoformat(now).astimezone(LOCAL_TZ).date().isoformat()
    ask = ask_with_spending(state, day)
    usd, merges = state.get("spent", {}).get(day, {}).get("usd", 0), len(state.get("merges", []))
    # по сборкам, в которых новости впервые появились (seen), как в прогонах на снимках: при старте с нуля так
    # время сюжетов настоящее, а не время этой сборки. Новость, ждавшая перевода, идёт со временем прошлой сборки
    builds = {}
    for url, item in items:
        builds.setdefault(max(item["seen"], state.get("built", "")), []).append((url, item))
    decided = merged = laid = 0
    started, step = time.monotonic(), max(len(items) // 10, 1)  # большая раскладка — в лог примерно каждые 10%
    for seen, group in sorted(builds.items()):
        decided += stories.update(state, group, seen, vectors, ask)["decided"]
        if stories.merge_due(state, seen):
            merged += stories.merge(state, seen, vectors, ask)["merged"]
        laid += len(group)
        if len(items) >= PROGRESS_FROM and laid // step > (laid - len(group)) // step and laid < len(items):
            took = time.monotonic() - started
            print(f"Сюжеты: разложено {laid} из {len(items)} ({laid * 100 // len(items)}%), склеено {merged}, "
                  f"≈ ${state['spent'].get(day, {}).get('usd', 0) - usd:.2f}, прошло {took / 60:.0f} мин, "
                  f"осталось ≈ {took * (len(items) - laid) / laid / 60:.0f} мин", file=sys.stderr)
    if len(items) >= PROGRESS_FROM:
        print(f"Сюжеты: разложено всё за {(time.monotonic() - started) / 60:.0f} мин; "
              "имена и поиск отдельных событий в больших сюжетах…", file=sys.stderr)
    name_stories(state, vectors, ask)
    lines = stories.find_lines(state, now, ask)
    for sid, name in lines["spun"]:  # выделенному сюжету — имя события, которое дала модель; исходный назовём заново:
        # его имя могло быть и про выделенное событие («Adriatic нефтепровод акцизы»)
        state["names"][sid], state.setdefault("named_at", {})[sid] = name, len(state["stories"][sid]["urls"])
        state["named_at"][state["spun"][sid]] = 0
    # время публикации из RSS — для порядка новостей в сюжете. Позже, чем мы её увидели, — неверный часовой пояс у
    # издания (RTS и др.): тогда время, когда увидели
    for u, it in state["items"].items():
        if "published" not in it and u in archive:
            it["published"] = min(archive[u].get("date") or it["seen"], it["seen"])
    # что сделала эта сборка — сводкой наверху страницы
    state["last"] = {"t": now, "rebuilt": rebuilt, "news": [u for u, _ in items], "commands": [c["result"] for c in done],
                     "merges": [[m["keep"], m["gone"]] for m in state.get("merges", [])[merges:]],
                     "lines": [lines["checked"], lines["found"]],
                     "usd": round(state.get("spent", {}).get(day, {}).get("usd", 0) - usd, 4)}
    forget_old(state, vectors, now)
    state["spent"] = {d: s for d, s in sorted(state.get("spent", {}).items())[-SPENT_DAYS:]}
    state["built"] = now
    save_state(state, vectors)
    if done:  # ответы в Telegram — после сохранения: не сохранилось — команды применятся заново в следующей сборке
        translate.reports_api({"results": [{"message": c.get("message"), "text": c["result"]} for c in done]}, "stories")
    today = state["spent"].get(day, {})
    print(f"Сюжеты: решено {decided}, склеено {merged}, сюжетов {len(state['stories'])}, "
          f"за сегодня ≈ ${today.get('usd', 0):.3f} (входа из кэша {today.get('read', 0) * 100 // max(today.get('in', 0), 1)}%)",
          file=sys.stderr)
    write_page(state, vectors, sections)


def count(n: int, one: str, few: str, many: str) -> str:
    """Число со словом: 1 новость, 3 новости, 5 новостей."""
    word = one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many
    return f"{n} {word}"


NEWS, OUTLETS = ("новость", "новости", "новостей"), ("издание", "издания", "изданий")


def write_page(state: dict, vectors: dict, sections: dict, error: str = "") -> None:
    """site/plot/index.html — статичная страница: видимые сюжеты за PAGE_WINDOW, крупные сверху; у каждого видимого
    сюжета — своя страница site/plot/sX/."""
    e = html.escape
    names = state.get("names", {})
    now = state.get("built") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    cutoff = (datetime.fromisoformat(now) - PAGE_WINDOW).isoformat()
    shown = [sid for sid, s in state["stories"].items() if s["updated"] >= cutoff and stories.visible(state, sid)]

    def sources(sid):
        return {state["items"][u]["source"] for u in state["stories"][sid]["urls"]}

    def heat(sid: str) -> float:
        latest = {}  # издание → время его последней новости в сюжете (публикации, не позже, чем увидели)
        for u in state["stories"][sid]["urls"]:
            it = state["items"][u]
            latest[it["source"]] = max(latest.get(it["source"], ""), it.get("published", it["seen"]))
        moment = datetime.fromisoformat(now)
        return sum(0.5 ** ((moment - datetime.fromisoformat(t)) / HEAT_HALF_LIFE) for t in latest.values())

    heats = {sid: heat(sid) for sid in shown}
    shown.sort(key=lambda sid: (heats[sid], state["stories"][sid]["updated"]), reverse=True)

    def local_time(iso: str) -> str:
        return datetime.fromisoformat(iso).astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")

    def headline(urls: list[str]) -> str:
        """Пока у сюжетов нет своих названий — «что произошло» (what, ровный пересказ без кликбейта из перевода)
        самой типичной из свежих новостей (HEADLINE_RECENT до последней): больше всех похожей на остальные свежие.
        Нет свежих с what — из всех. Без what — заголовок."""
        last = max(datetime.fromisoformat(state["items"][u]["seen"]) for u in urls)
        with_what = [u for u in urls if state["items"][u].get("what") and u in vectors]
        with_what = [u for u in with_what
                     if last - datetime.fromisoformat(state["items"][u]["seen"]) <= HEADLINE_RECENT] or with_what
        if not with_what:
            return state["items"][urls[0]]["title"]
        v = np.stack([vectors[u] for u in with_what])
        return state["items"][with_what[int((v @ v.T).mean(axis=1).argmax())]]["what"]

    def closest(urls: list[str], among: list[str]) -> dict[str, tuple[float, int]]:
        """Для каждой новости urls — лучшее сходство с новостями among (кроме неё самой) и номер той новости."""
        have = [u for u in urls if u in vectors]
        other = [u for u in among if u in vectors]
        if not have or not other:
            return {}
        sims = np.stack([vectors[u] for u in have]).astype(np.float32) @ np.stack([vectors[u] for u in other]).astype(np.float32).T
        sims[np.array(have)[:, None] == np.array(other)[None, :]] = -1
        return {u: (float(sims[n].max()), int(sims[n].argmax())) for n, u in enumerate(have)}

    def sim_mark(sim: float | None) -> str:
        """Сходство цифрой: ниже RECHECK_SIM — жёлтым, ниже CANDIDATE_SIM — красным."""
        if sim is None or sim < -0.5:
            return ""
        level = "low" if sim < stories.CANDIDATE_SIM else "mid" if sim < stories.RECHECK_SIM else "ok"
        return f' · <span class="sim {level}">{sim:.3f}</span>'

    opened = {}  # (сюжет, время) → номера новостей, открывших его одной сборкой
    for it in state["items"].values():
        if it["history"][0]["story"] and it["history"][0]["why"] != "join":
            opened.setdefault((it["history"][0]["story"], it["history"][0]["t"]), []).append(it["n"])

    def how(u: str, here: str = "", link=None, sim: bool = False) -> str:
        """Что случилось с новостью — словами: как попала в сюжет и куда её потом переносили. here — сюжет, в котором
        её показываем («этот сюжет»); link — как писать другой сюжет (ссылкой или текстом)."""
        it = state["items"][u]

        def where(sid: str) -> str:
            if sid == here:
                return "этот сюжет"
            return link(sid) if link else e(sid + (f' «{names[sid]}»' if sid in names else ""))

        first, *later = it["history"]
        if not first["story"]:
            text = "без сюжета: модель не поняла, о чём новость"
        elif first["why"] == "join":
            text = f'модель добавила в {where(first["story"])}' + (
                f' (сходство {first["sim"]:.3f})' if sim and first.get("sim") else "")
        else:
            others = sorted(m for m in opened.get((first["story"], first["t"]), []) if m != it["n"])
            text = (f'открыла {where(first["story"])}' + (" вместе с " + ", ".join(f"№{m}" for m in others) if others else "")
                    + {"alone": ": похожих сюжетов не было", "new": ": модель сочла событие новым",
                       "unclear": ": модель не поняла, о чём новость"}.get(first["why"], ""))
        steps = {"recheck": "перепроверка не согласилась — новость ушла в {}", "merge": "её сюжет {from_} влит в {}",
                 "line": "выделена отдельным событием в {}", "move": "перенесена командой /move в {}", "split": "отделена командой /split в {}",
                 "unmerge": "склейка отменена командой /unmerge — вернулась в {}"}
        for h in later:
            text += "; " + steps.get(h["why"], h["why"] + " → {}").format(
                where(h["story"]) if h["story"] else "без сюжета", from_=e(h.get("from") or "—"))
        return text

    def item_row(u: str, it: dict, extra: str) -> str:
        what = it.get("what", "")
        return (f'<li><span class="meta">№{it.get("n", "")} · {local_time(it.get("published", it["seen"]))} · {e(it["source"])} · '
                f'{e(sections.get(it["section"], it["section"]))}{extra}</span><br>'
                f'<a href="{e(u)}" rel="noopener" target="_blank">{e(it["title"])}</a>'
                f'{f"<br><span class=what>{e(what)}</span>" if what and what != it["title"] else ""}</li>')

    children = {}  # сюжет → выделенные из него
    for child, parent in state.get("spun", {}).items():
        children.setdefault(parent, []).append(child)

    def page_link(sid: str) -> str:  # абсолютная ссылка: работает и на главной, и на странице сюжета
        text = sid + (f' «{names[sid]}»' if sid in names else "")
        return (f'<a href="/plot/{sid}/" target="_blank">{e(text)}</a>' if stories.visible(state, sid)
                else e(text)) if sid in state["stories"] else e(sid)

    def lines_note(sid: str) -> str:
        """Отдельные события, выделенные из сюжета в свои (stories.find_lines), — или что их не нашлось."""
        check = state.get("lines", {}).get(sid)
        why = {line.get("to"): line["why"] for c in state.get("lines", {}).values() for line in c["found"]}
        out = [c for c in children.get(sid, []) if c in state["stories"]]
        checked = (f'проверено {e(local_time(check["t"]))}, когда в сюжете стало {count(check["size"], *NEWS)}'
                   if check else "")
        if out:
            return ('<div class="lines">Из этого сюжета ушли в свои сюжеты (новости перенесены туда):<ul>'
                    + "".join(f'<li>→ {page_link(c)} — {count(len(state["stories"][c]["urls"]), *NEWS)}'
                              f'{" · " + e(why[c]) if c in why else ""}</li>' for c in out)
                    + f'</ul><span class="meta">{checked}</span></div>')
        if check and not check["found"]:
            return f'<p class="meta">Отдельных событий внутри не нашлось: {checked}.</p>'
        return ""

    def story_parts(sid: str) -> tuple[str, str, str, str]:
        """Сюжет: (заголовок, строка о нём, что можно выделить, список новостей)."""
        story = state["stories"][sid]
        urls = story["urls"]
        items = [state["items"][u] for u in urls]
        camps = Counter(sections.get(it["section"], it["section"]) for it in items)
        near = closest(urls, urls)

        def nearby(u: str) -> str:  # самая похожая новость этого же сюжета
            if u not in near:
                return ""
            sim, k = near[u]
            return f' · ближе всего к №{state["items"][urls[k]]["n"]}{sim_mark(sim)}'

        def rows(part_urls: list[str]) -> str:  # свежие сверху, по времени публикации
            return "".join(item_row(u, state["items"][u], f' · {how(u, sid)}{nearby(u)}')
                           for u in sorted(part_urls, key=lambda u: (state["items"][u].get("published", state["items"][u]["seen"]),
                                                                     state["items"][u]["n"]), reverse=True))

        # части (stories.py) на странице пока не показываем: 12.11 они делили сюжеты случайно, по тому, где новость
        # оказалась первой. Откуда новость пришла склейкой, видно по её пути
        body = rows(urls)
        about = (f'{f"горячесть {heats[sid]:.1f} · " if sid in heats else ""}'
                 f'{count(len(urls), *NEWS)} · {count(len(sources(sid)), *OUTLETS)} · '
                 f'{e(", ".join(f"{c} {n}" for c, n in camps.most_common()))} · '
                 f'{local_time(story["created"])} — {local_time(story["updated"])} · {sid}'
                 f'{f" «{e(names[sid])}»" if sid in names else ""}'
                 f'{" · выделен из " + page_link(state["spun"][sid]) if sid in state.get("spun", {}) else ""}')
        return headline(urls), about, lines_note(sid), body

    # у каждого видимого сюжета — своя страница /plot/sX/; у сюжетов, которые уже не на главной странице (старше
    # PAGE_WINDOW), тоже: на них ведут ссылки, отправленные раньше
    pages = {sid: story_parts(sid) for sid in state["stories"] if stories.visible(state, sid)}

    def story_link(sid: str, text: str = "") -> str:
        """Ссылка на страницу сюжета — в новом окне, с кодовым именем; у сюжета из одного издания страницы нет."""
        text = text or sid + (f' «{names[sid]}»' if sid in names else "")
        return f'<a href="{sid}/" target="_blank">{e(text)}</a>' if sid in pages else e(text)

    def label(sid: str) -> str:  # короткая подпись сюжета для оглавления и переходов
        title = names.get(sid) or pages[sid][0]
        return title if len(title) <= 60 else title[:60].rsplit(" ", 1)[0] + "…"

    def neighbours(sid: str, href: str) -> str:
        """Переходы к соседним сюжетам в порядке главной страницы; href — как ссылаться на сюжет."""
        if sid not in shown:
            return ""
        n = shown.index(sid)
        prev = f'<a href="{href.format(shown[n - 1])}" title="{e(label(shown[n - 1]))}">← пред.</a>' if n else ""
        after = (f'<a href="{href.format(shown[n + 1])}" title="{e(label(shown[n + 1]))}">след. →</a>'
                 if n + 1 < len(shown) else "")
        return " · ".join(x for x in (prev, after) if x)

    cards = [f'<details class="story" id="{sid}" open><summary><b>{e(pages[sid][0])}</b><br><span class="meta">'
             f'{pages[sid][1]} · {story_link(sid, "страница сюжета")} · '
             f'{neighbours(sid, "#{}") + " · " if len(shown) > 1 else ""}<a href="#top">↑</a>'
             f'</span></summary>{pages[sid][2]}<ul>{pages[sid][3]}</ul></details>'
             for sid in shown]
    toc = ('<nav class="toc"><details id="toc"><summary>Оглавление</summary><ol>'
           + "".join(f'<li><a href="#{sid}">{e(label(sid))}</a> <span class="meta">{len(state["stories"][sid]["urls"])}</span></li>'
                     for sid in shown)
           + '</ol><p><a href="#lonely">Не попали в видимые</a><br><a href="#commands">Правки через бота</a><br>'
             '<a href="#spent">Расход</a><br><a href="#top">↑ Наверх</a></p></details></nav>')

    # новости, которых на странице не видно: сюжет из одного издания. Сверху — самые похожие на видимый сюжет:
    # среди них и те, что должны были в него попасть
    hidden = [u for sid, s in state["stories"].items() if s["updated"] >= cutoff and sid not in shown for u in s["urls"]]
    visible_urls = [u for sid in shown for u in state["stories"][sid]["urls"]]
    near = closest(hidden, visible_urls)
    hidden.sort(key=lambda u: near.get(u, (-1,))[0], reverse=True)

    def nearest(u: str) -> str:
        if u not in near:
            return ""
        sim, k = near[u]
        target = state["items"][visible_urls[k]]["story"]
        return f' · из видимых ближе всего {story_link(target)}{sim_mark(sim)}'

    def company(u: str) -> str:  # другие новости того же сюжета из одного издания
        sid = state["items"][u]["story"]
        others = [state["items"][x]["n"] for x in state["stories"][sid]["urls"] if x != u] if sid else []
        return f' · там же {", ".join(f"№{m}" for m in others)}' if others else ""

    lonely = "".join(item_row(u, state["items"][u], f' · {how(u)}{company(u)}{nearest(u)}') for u in hidden)

    def command(c: dict) -> str:
        args = {"move": lambda: f"{c['news']} {c['to']}", "split": lambda: " ".join(c["parts"]),
                "unmerge": lambda: c["story"]}.get(c["do"], lambda: "")()
        return f"<li>{e(local_time(c['applied']))} · /{e(c['do'])} {e(args)} — {e(c['result'])}</li>"

    commands = "".join(command(c) for c in reversed(state.get("commands", [])))

    spent = "".join(f"<tr><td>{d}</td><td>{s['calls']}</td><td>${s['usd']:.3f}</td></tr>"
                    for d, s in sorted(state.get("spent", {}).items(), reverse=True))
    total = len(state["stories"])
    style = """<style>body{font:15px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#222;background:#fff}
summary{cursor:pointer;padding:6px 0}details{border-bottom:1px solid #eee}ul{margin:4px 0 12px;padding-left:20px}
li{margin:6px 0}.meta{color:#888;font-size:13px}a{color:#1a5fb4}table{border-collapse:collapse;font-size:13px}
td{padding:2px 14px 2px 0}.what{color:#777;font-size:13px}
.toc ol{padding-left:3em;font-size:13px}.toc li{margin:3px 0}
@media (min-width:1400px){.toc{position:fixed;left:16px;top:16px;width:calc(50vw - 490px);max-height:calc(100vh - 32px);overflow:auto}}
.lines{background:#f2f2f2;border-radius:6px;padding:8px 12px;margin:8px 0}.lines ul{margin:4px 0}
.sim.mid{color:#b7791f}.sim.low{color:#c0392b;font-weight:600}.err{background:#fde8d8;color:#a33;padding:8px 12px}
@media (prefers-color-scheme:dark){body{background:#161616;color:#ddd}details{border-color:#333}a{color:#78aeed}.lines{background:#262626}}</style>"""
    head = '<!doctype html><html lang="ru"><head><meta charset="utf-8">\n<meta name="viewport" ' \
           'content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">'
    toggle = "document.querySelectorAll('details.story').forEach(d => d.open = %s)"
    last = state.get("last") or {}
    merges = last.get("merges", [])

    # что сделала каждая новость этой сборки — словами (how): открыла сюжет, добавлена в сюжет, без сюжета; и куда её
    # перенесли потом (перепроверка, склейка)
    fresh = [u for u in last.get("news", []) if u in state["items"]] if isinstance(last.get("news"), list) else []
    kinds, notes = Counter(), {}
    for u in fresh:
        h = state["items"][u]["history"][0]
        kinds["без сюжета" if not h["story"] else "добавлены в сюжет" if h["why"] == "join" else "открыли сюжет"] += 1
        notes[u] = how(u, link=story_link, sim=True)
    arrived = "".join(item_row(u, state["items"][u], f" · {notes[u]}")
                      for u in sorted(fresh, key=lambda u: (state["items"][u]["seen"], state["items"][u]["n"])))
    done = [f"<b>раскладка заново ({e(last['rebuilt'])})</b>: все новости с {e(local_time(START))} по нынешним правилам"
            if last.get("rebuilt") else "",
            f"новых новостей {len(fresh)}" + (": " + ", ".join(f"{e(k)} — {n}" for k, n in kinds.most_common()) if fresh else ""),
            f"склеено {len(merges)}" + (": " + ", ".join(f'{story_link(k)} ← {e(g)}' for k, g in merges[:20])
                                       + (" …" if len(merges) > 20 else "") if merges and not last.get("rebuilt") else ""),
            "команды: " + "; ".join(e(r) for r in last["commands"]) if last.get("commands") else "",
            (f"большие сюжеты проверены на отдельные события: {last['lines'][0]}, выделено событий {last['lines'][1]}"
             if last.get("lines", [0])[0] else ""),
            f"запросы к модели ≈ ${last.get('usd', 0):.3f}"]
    summary = " · ".join(x for x in done if x)
    rebuilt_note = (f'<p class="meta">Раскладка заново: здесь все новости с {e(local_time(START))} по порядку, как они '
                    f'приходили; «открыла сюжет» — была в нём первой, остальные добавлялись позже.</p>'
                    if last.get("rebuilt") else "")
    page = f"""{head}
<title>Сюжеты — теневой режим</title>
{style}
</head><body>
<h1 id="top">Сюжеты — теневой режим</h1>
{toc}
<p class="meta">Сборка {e(local_time(now))} · видимых сюжетов (от двух изданий) за сутки: {len(shown)} · всего в памяти: {total}.
Новости с {e(local_time(START))}; правила {e(state.get("fingerprint", ""))}, по ним разложено с {e(local_time(state.get("rebuilt", now)))}:
поменялись правила — страница раскладывается заново целиком. Прежняя страница сюжетов-тем — <a href="/plot1/">/plot1/</a>.
Страница не индексируется и на сайте нигде не упомянута.</p>
{f'<p>В этой сборке: {summary}.</p>' if summary else ""}
{f'<details><summary>Новости этой сборки — что сделала каждая</summary>{rebuilt_note}<ul>{arrived}</ul></details>' if arrived else ""}
{f'<p class="err">Сюжеты в этой сборке не обновились: {e(error)}</p>' if error else ""}
<p class="meta">Сюжеты — по горячести: каждое издание даёт очко за свою последнюю новость в сюжете, очко вдвое тает
за {int(HEAT_HALF_LIFE.total_seconds() // 3600)} ч (час назад — почти 1, {int(HEAT_HALF_LIFE.total_seconds() // 3600)} ч назад — 0.5, сутки назад — 0.06).
Цифра у новости — сходство с самой похожей другой новостью сюжета (у одиночных — с новостью видимого сюжета).
Модель видит новость с сюжетом, только если сходство не ниже {stories.CANDIDATE_SIM}; <span class="sim mid">жёлтые</span> — ниже
{stories.RECHECK_SIM}, такое добавление перепроверяет Haiku; <span class="sim low">красные</span> — ниже {stories.CANDIDATE_SIM}.
Серым под заголовком — «что произошло» (what): по нему считается сходство.</p>
<p><button onclick="{toggle % 'true'}">Раскрыть все</button> <button onclick="{toggle % 'false'}">Свернуть все</button>
<span class="meta">· клавиши j / k — следующий / предыдущий сюжет</span></p>
{"".join(cards) or "<p>Пока пусто.</p>"}
<h2 id="lonely">Не попали в видимые сюжеты</h2>
<details><summary>{len(hidden)} новостей за сутки в сюжетах из одного издания — самые похожие на видимый сюжет сверху</summary>
<ul>{lonely}</ul></details>
<h2 id="commands">Правки через бота</h2>
<p class="meta">Боту в Telegram: /move 1234 s471 — новость №1234 в сюжет s471 (new — в новый), /split s191 — часть
в отдельный сюжет, /unmerge s102 — отменить склейку.</p>
{f"<ul>{commands}</ul>" if commands else "<p class=meta>Пока не было.</p>"}
<h2 id="spent">Расход</h2><table><tr><td>день</td><td>запросов</td><td>≈ $</td></tr>{spent}</table>
<script>
// оглавление сбоку на широком экране открыто, на узком — свёрнуто наверху
if (matchMedia('(min-width:1400px)').matches) document.getElementById('toc').open = true;
// j / k — к следующему / предыдущему сюжету: к первому, чей верх ниже (выше) верха окна
document.addEventListener('keydown', ev => {{
  if (ev.target.closest('input,textarea') || ev.metaKey || ev.ctrlKey || ev.altKey || !'jk'.includes(ev.key)) return;
  const cards = [...document.querySelectorAll('details.story')];
  const next = ev.key === 'j' ? cards.find(c => c.getBoundingClientRect().top > 5)
                              : cards.reverse().find(c => c.getBoundingClientRect().top < -5);
  if (next) next.scrollIntoView();
}});
</script>
</body></html>"""
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    (PAGE_DIR / "index.html").write_text(page, "utf-8")
    for sid, (title, about, lines, body) in pages.items():
        (PAGE_DIR / sid).mkdir(exist_ok=True)
        (PAGE_DIR / sid / "index.html").write_text(f"""{head}
<title>{e(title)}</title>
{style}
</head><body>
<p class="meta"><a href="../#{sid}">← все сюжеты</a>{" · " + neighbours(sid, "../{}/") if sid in shown else ""} · сборка {e(local_time(now))}</p>
<h1>{e(title)}</h1>
<p class="meta">{about}</p>
{lines}
<ul>{body}</ul>
</body></html>""", "utf-8")


def safe_run(archive: dict, sections: dict) -> None:
    """Для build.py: ошибка в сюжетах не роняет сборку — пишем в лог, а на странице — прошлое состояние и ошибку."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("Сюжеты пропущены: не задан ANTHROPIC_API_KEY", file=sys.stderr)
        return
    try:
        run(archive, sections)
    except Exception as exc:  # noqa: BLE001 — сюжеты в теневом режиме, сайт важнее
        traceback.print_exc()
        print(f"Сюжеты: ошибка, состояние не сохранено — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
        try:
            write_page(*load_state(), sections, f"{type(exc).__name__}: {exc}"[:300])
        except Exception:  # noqa: BLE001
            traceback.print_exc()


if __name__ == "__main__":
    config = json.loads((ROOT / "sources.json").read_text("utf-8"))
    archive_file = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "cache" / "archive.json"
    safe_run(json.loads(archive_file.read_text("utf-8")), {s["id"]: s["title_ru"] for s in config["sections"]})
