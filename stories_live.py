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
    /move 1234 s471   — новость №1234 в сюжет s471 (new — в новый сюжет); несколько номеров — вместе:
                        /move 1234 1240 1302 new
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
import stories_audit
import translate

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "cache" / "stories.json"
VECTORS_FILE = ROOT / "cache" / "story-vectors.npz"
PAGE_DIR = ROOT / "site" / "plot"
COMMANDS_FILE = ROOT / "stories-commands.json"
ORIG_FILE = ROOT / "stories-orig.json"  # оригинальные заголовки старых новостей, разовый перенос
COMMANDS_SHOWN = 30  # последних команд с итогом — на странице
# начало новой страницы: 12.11 17:05 по Белграду — с этой сборки у новостей части, история переносов и номера для бота
START = "2026-11-12T16:05:00+00:00"
# новость без перевода ждёт одну сборку (перевод мог опоздать), потом идёт по оригинальному заголовку
WAIT_TRANSLATION = timedelta(minutes=20)
# константы stories.py, которые не меняют раскладку, — не в отпечатке: их правка не раскладывает всё заново
NOT_RULES = {"LINES_STEP", "MERGE_EVERY"}
# прежние отпечатки, при которых раскладку не пересобираем — новые правила действуют на следующие новости:
# 93f151fd03 — до 13.11, когда перепроверку больших сюжетов перевели с роста в 1.5 раза на LINES_STEP;
# dfadd2988f — до 13.11, когда склейка стала называть общее событие пары (event);
# 9e34e14300 — до 14.11, когда склейку перевели с раза в час на раз в два часа (MERGE_EVERY ушло из отпечатка);
# a0cbf341ea — до 15.11, когда повод сюжета с карточкой в склейке стал самой карточкой (пересборка с карточками стоила $6);
# 74b3c97a3d — до 16.11, когда копии пошли за оригиналом без модели, перепроверка и склейка стали выбирать тип связи
# (LINKS) вместо «да / нет», а сильная модель — работать только на SHOW_TOP сюжетов страницы
# 37beed8aa5 — до 16.11, когда горячесть стала таять за 12 часов, а не за 6, и зарубежным сюжетам — вчетверо меньше
# 842ae523cb — до 16.11, когда Косово и сербские организации и СМИ стали связью с Сербией (SERBIA_MARKS)
# a4183da04a — до 17.11, когда Sonnet стал перепроверять только join ниже RECHECK_SONNET_MAX, а склейку — только пары,
# которые Haiku не назвал несвязанными (MERGE_FILTER_DROP)
# ebdeb2625d — до 18.11, когда склейка стала называть development / consequence / reaction только при прямой причине
# или том же деле (MERGE_PROMPT): уже склеенное остаётся, новое правило — для следующих пар;
# a965920995 — до 18.11, когда из контрпримеров убран арест владельца банка: по RULES это то же дело, что его крах
SAME_RULES = {"93f151fd03", "dfadd2988f", "9e34e14300", "a0cbf341ea", "74b3c97a3d", "37beed8aa5", "842ae523cb",
              "a4183da04a", "ebdeb2625d", "a965920995"}
# сюжет, который не обновлялся дольше, убираем из состояния вместе с его новостями: архив их уже забыл
# (ARCHIVE_DAYS в build.py — 4 дня), так что заново раскладывать их не придётся
FORGET = timedelta(days=5)
SPENT_DAYS = 14
PAGE_WINDOW = timedelta(hours=24)  # на странице — видимые сюжеты, обновлявшиеся за это время (не больше stories.SHOW_TOP)
# порядок сюжетов — по горячести (stories.heat): новый поворот, о котором пишут многие, поднимает сюжет
# заголовок сюжета — из его новостей за столько часов до последней: самая типичная за всё время 13.11 держала
# анонс «пройдут консультации», когда они давно прошли
HEADLINE_RECENT = timedelta(hours=6)
# склейка, деление больших сюжетов и проверка дня — пакетами (Message Batches API): вдвое дешевле, ответ — обычно
# за минуты, самое позднее за сутки. Пакет отправляет одна сборка, ответы применяет первая сборка после того, как они
# пришли; пока пакет у модели, нового того же вида не шлём. Раскладка заново — сразу: она идёт по сборкам подряд,
# и каждой нужны склейки предыдущей
BATCH_DISCOUNT = 0.5
BATCH_KINDS = {"merge": "склейка", "lines": "деление", "audit": "проверка дня", "recheck": "перепроверка"}
BATCH_GIVE_UP = timedelta(days=2)  # пакет, который столько не удаётся проверить, забываем: модель держит его сутки
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


def known_sources() -> set[str]:
    """Издания из sources.json — по имени, как в новостях."""
    return {src["name"] for s in json.loads(SOURCES_FILE.read_text("utf-8"))["sections"] for src in s.get("sources", [])}


def drop_gone_sources(state: dict, vectors: dict, known: set[str]) -> int:
    """Новости изданий, которых больше нет в sources.json, — вон из сюжетов и из памяти вместе с векторами, а сюжеты,
    в которых ничего не осталось, — тоже (имена, карточки и прочее за ними подчистит forget_old). Иначе убранное
    издание (18.11 — Google News EN и RU, мелкие сайты со всего мира) ещё дни сидело бы в растущих сюжетах и в счёте
    изданий. Архив помнит их ARCHIVE_DAYS — новые новости таких изданий в сюжеты не берём (run). Возвращает, сколько убрано."""
    gone = [u for u, it in state["items"].items() if it["source"] not in known]
    for u in gone:
        it = state["items"].pop(u)
        vectors.pop(u, None)
        story = state["stories"].get(it["story"]) if it["story"] else None
        if story and u in story["urls"]:
            story["urls"].remove(u)
    for sid in [sid for sid, story in state["stories"].items() if not story["urls"]]:
        del state["stories"][sid]
    return len(gone)


def forget_old(state: dict, vectors: dict, now: str) -> None:
    """Убирает давно не обновлявшиеся сюжеты вместе с векторами их новостей."""
    cutoff = (datetime.fromisoformat(now) - FORGET).isoformat()
    for sid in [s for s, story in state["stories"].items() if story["updated"] < cutoff]:
        for u in state["stories"].pop(sid)["urls"]:
            state["items"].pop(u, None)
    # новости без сюжета (до 12.11 так оставались «непонятные», unclear): забываем по времени
    state["items"] = {u: it for u, it in state["items"].items() if it["story"] or it["seen"] >= cutoff}
    if "audited_news" in state:  # отмеченные вечерней проверкой — пока новость помним
        known = {it["n"] for it in state["items"].values()}
        state["audited_news"] = [n for n in state["audited_news"] if n in known]
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
    # карточка склеенного сюжета живёт, пока жива его часть: unmerge вернёт сюжет с ней
    state["cards"] = {sid: c for sid, c in state.get("cards", {}).items() if sid in state["stories"] or sid in parts}
    state["serbia"] = {sid: c for sid, c in state.get("serbia", {}).items() if sid in state["cards"]}
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
    if c["do"] == "move":  # news — номер или список: несколько новостей — вместе в один сюжет
        numbers = c["news"] if isinstance(c["news"], list) else [c["news"]]
        missing = [n for n in numbers if n not in by_n]
        if missing:
            raise ValueError(f"новост{'и' if len(missing) == 1 else 'ей'} {', '.join(f'№{n}' for n in missing)} нет: "
                             "забыт{'а' if len(missing) == 1 else 'ы'} или ещё не разложен{'а' if len(missing) == 1 else 'ы'}")
        urls = [by_n[n] for n in numbers]
        news = ", ".join(f"№{n}" for n in numbers)
        olds = sorted({state["items"][u]["story"] for u in urls}, key=lambda s: int(s[1:]))
        if c["to"] == "new":
            sid = stories.move(state, urls[0], None, now)
            for u in urls[1:]:
                stories.move(state, u, sid, now, part=sid)
            for old in olds:
                if old in stories_:  # новая история: склейка не вернёт её туда, откуда сняли
                    state.setdefault("apart", []).append([[sid], sorted(stories._parts(state, old))])
            return f"{news}: {', '.join(olds)} → новый сюжет {sid}"
        if c["to"] not in stories_:
            raise ValueError(f"сюжета {c['to']} нет")
        parts = []
        for u in urls:
            if state["items"][u]["story"] != c["to"]:
                part = stories._nearest_part(state, c["to"], vectors[u], vectors) if u in vectors else c["to"]
                stories.move(state, u, c["to"], now, part=part)
                parts.append(part)
        if not parts:
            return f"{news} уже в {c['to']}"
        return f"{news}: {', '.join(olds)} → {c['to']} (часть {', '.join(sorted(set(parts)))})"
    if c["do"] == "fix":  # кнопка отчёта проверки дня
        return stories_audit.apply_fix(state, c["day"], c["which"], now)
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


def make_client():
    import anthropic

    return anthropic.Anthropic(max_retries=translate.API_RETRIES)


def step_of(system: str) -> str:
    """Шаг сборки по системному промпту запроса — для расхода по шагам."""
    return {stories.SYSTEM_PROMPT: "раскладка", stories.RECHECK_PROMPT: "перепроверка", stories.MERGE_PROMPT: "склейка",
            stories.LINES_PROMPT: "деление", NAME_PROMPT: "имена", stories.CARD_PROMPT: "карточки", stories_audit.AUDIT_PROMPT: "аудит",
            stories_audit.ADVICE_PROMPT: "аудит", SR_HEAD_PROMPT: "заголовки sr"}.get(system, "другое")


def spender(state: dict, day: str):
    """spend(шаг, usage): расход запроса — в state["spent"][день] и по шагам — в state["spent"][день]["steps"]."""
    spent = state.setdefault("spent", {})

    def spend(step: str, usage: dict) -> None:
        total = spent.setdefault(day, {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
        total["calls"] += 1
        for k in ("in", "out", "usd", "read"):  # read — часть входа из кэша
            total[k] = round(total.get(k, 0) + usage[k], 6)
        step = total.setdefault("steps", {}).setdefault(step, {"calls": 0, "usd": 0.0})
        step["calls"] += 1
        step["usd"] = round(step["usd"] + usage["usd"], 6)

    return spend


def ask_with_spending(client, spend):
    """ask для stories.update и stories.merge: запрос к модели сразу, расход — spend."""

    def ask(system: str, content: str, schema: dict, effort: str = stories.EFFORT,
            model: str = stories.MODEL, price: tuple[float, float] | None = None) -> tuple[dict, dict]:
        answer, usage = stories.ask_model(client, system, content, schema, effort, model, price)
        spend(step_of(system), usage)
        return answer, usage

    return ask


def send(client, state: dict, kind: str, calls: list[tuple], sent: dict, now: str, ask) -> tuple[dict, float] | None:
    """Запросы calls — [(system, content, schema, effort, model, price)] — пакетом вида kind (BATCH_KINDS); sent —
    что понадобится, чтобы применить ответы. Ушли пакетом — None: ответы соберёт collect. Пакет не ушёл или
    спрашивать нечего — ответы сразу: ({номер запроса: ответ}, во что обошлись)."""
    if calls:
        try:
            batch = client.messages.batches.create(requests=[{"custom_id": str(k), "params": stories.request(*call[:5])}
                                                             for k, call in enumerate(calls)])
            state.setdefault("batches", {})[kind] = {
                "id": batch.id, "t": now, "steps": [step_of(c[0]) for c in calls],
                "prices": [c[5] or stories.PRICES[c[4]] for c in calls], **sent}
            return None
        except Exception as exc:  # noqa: BLE001
            print(f"Сюжеты: пакет «{BATCH_KINDS[kind]}» не ушёл, спрашиваем сразу — {type(exc).__name__}: {exc}"[:300],
                  file=sys.stderr)
    answers, usd = {}, 0.0
    for k, call in enumerate(calls):
        answers[k], usage = ask(*call)
        usd += usage["usd"]
    return answers, usd


def collect(client, state: dict, now: str, spend) -> dict[str, tuple[dict, dict, float]]:
    """Пакеты, на которые модель уже ответила: вид → (что отправили, ответы {номер запроса: ответ}, во что обошлись).
    Расход — со скидкой BATCH_DISCOUNT. Запрос без ответа (ошибка, обрезан) пропадает: его сюжеты попадут в следующий
    пакет того же вида, если всё ещё подходят."""
    done = {}
    for kind, sent in list(state.get("batches", {}).items()):
        try:
            if client.messages.batches.retrieve(sent["id"]).processing_status != "ended":
                continue
            results = list(client.messages.batches.results(sent["id"]))
        except Exception as exc:  # noqa: BLE001
            print(f"Сюжеты: пакет «{BATCH_KINDS[kind]}» не проверить — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
            if datetime.fromisoformat(now) - datetime.fromisoformat(sent["t"]) > BATCH_GIVE_UP:
                del state["batches"][kind]
            continue
        answers, usd = {}, 0.0
        for r in results:
            k = int(r.custom_id)
            if r.result.type != "succeeded":
                continue
            try:
                answer, usage = stories.answer_of(r.result.message, sent["prices"][k])
            except (RuntimeError, ValueError) as exc:
                print(f"Сюжеты: пакет «{BATCH_KINDS[kind]}», запрос {k} — {exc}"[:300], file=sys.stderr)
                continue
            usage = {**usage, "usd": usage["usd"] * BATCH_DISCOUNT}
            spend(sent["steps"][k], usage)
            answers[k], usd = answer, usd + usage["usd"]
        if len(answers) < len(results):
            print(f"Сюжеты: пакет «{BATCH_KINDS[kind]}» — без ответа {len(results) - len(answers)} из {len(results)}",
                  file=sys.stderr)
        del state["batches"][kind]
        done[kind] = (sent, answers, usd)
    return done


def cancel(client, state: dict) -> None:
    """Раскладка заново: пакеты, ещё ждущие ответа, — отменить. Их ответы про сюжеты прежней раскладки, применить их
    не к чему, а запросы, до которых модель не дошла, при отмене не оплачиваются."""
    for kind, sent in state.get("batches", {}).items():
        try:
            client.messages.batches.cancel(sent["id"])
            print(f"Сюжеты: пакет «{BATCH_KINDS[kind]}» отменён — раскладываем заново", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — уже закончился или недоступен: заплачено, ответы просто не нужны
            print(f"Сюжеты: пакет «{BATCH_KINDS[kind]}» не отменить — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)


def merge_later(client, state: dict, now: str, vectors: dict, ask) -> int:
    """Склейка пакетом: пары — к модели, ответы применит сборка, в которую они придут. Сколько склеено сразу (пакет
    не ушёл)."""
    asked = stories.merge_todo(state, now, vectors)
    todo, haiku = stories.merge_filter(state, now, asked, vectors, ask)
    print(f"Сюжеты: склейка — пар {len(asked)}, Haiku отсеял {len(asked) - len(todo)}, Sonnet — {len(todo)}", file=sys.stderr)
    calls = [(stories.MERGE_PROMPT, c, stories.MERGE_SCHEMA, stories.MERGE_EFFORT, stories.MERGE_MODEL, None)
             for c in stories.merge_requests(state, todo, vectors)]
    sent = {"todo": todo, "haiku": haiku}
    got = send(client, state, "merge", calls, sent, now, ask)
    return 0 if got is None else apply_merge(state, now, sent, got[0])


def recheck_later(client, state: dict, now: str, ask) -> None:
    """Перепроверка пакетом: запросы, накопленные раскладкой (state["recheck_queue"]), — к модели одним пакетом,
    если прежний уже вернулся; ответы применит сборка, в которую они придут. Пакет не ушёл — ответы сразу."""
    if not state.get("recheck_queue") or "recheck" in state.get("batches", {}):
        return
    queue = state.pop("recheck_queue")
    calls = [(stories.RECHECK_PROMPT, r["content"], stories.RECHECK_SCHEMA, stories.RECHECK_EFFORT, r["model"], None)
             for r in queue]
    sent = {"news": [r["news"] for r in queue]}
    if got := send(client, state, "recheck", calls, sent, now, ask):
        apply_recheck(state, now, sent, got[0])


def apply_recheck(state: dict, now: str, sent: dict, answers: dict) -> int:
    return sum(stories.recheck_apply(state, sent["news"][k], answer, now) for k, answer in answers.items())


def apply_merge(state: dict, now: str, sent: dict, answers: dict) -> int:
    todo = [tuple(pair) for pair in sent["todo"]]
    same, asked = {}, set()
    for k, answer in answers.items():
        same |= stories.merge_answer(state, todo, answer, k)
        asked |= set(range(k * stories.MERGE_BATCH, min((k + 1) * stories.MERGE_BATCH, len(todo))))
    stories.merge_note(state, now, todo, sent.get("haiku", []), answers)
    return stories.merge_apply(state, now, todo, same, asked)


def apply_lines(state: dict, now: str, sent: dict, answers: dict) -> tuple[int, int]:
    """Ответы проверки больших сюжетов на отдельные события: (проверено, выделено событий)."""
    found = 0
    for k, answer in answers.items():
        sid = sent["sids"][k]
        if sid in state["stories"]:  # мог уйти склейкой, пока ждали ответа
            lines = stories.lines_apply(state, now, sid, answer, sent["sizes"][k])
            name_spun(state, [(line["to"], line["name"]) for line in lines])
            found += len(lines)
    return len(answers), found


def name_spun(state: dict, spun: list[tuple[str, str]]) -> None:
    """Выделенному сюжету — имя события, которое дала модель; исходный назовём заново: его имя могло быть и про
    выделенное событие («Adriatic нефтепровод акцизы»)."""
    for sid, name in spun:
        state.setdefault("names", {})[sid] = name
        state.setdefault("named_at", {})[sid] = len(state["stories"][sid]["urls"])
        state["named_at"][state["spun"][sid]] = 0


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
    """Одна сборка: ответы пакетов, пришедшие с прошлой сборки, — в раскладку; новые новости архива — в сюжеты,
    раз в MERGE_EVERY склейка, деление больших сюжетов топа, вечером проверка дня, потом страница. sections — {id: название}."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state, vectors = load_state()
    client = make_client()
    rebuilt = ""
    if state.get("fingerprint") in SAME_RULES:
        state["fingerprint"] = fingerprint()
    if state.get("fingerprint") != fingerprint() or state.get("start") != START:
        rebuilt = "правила изменились" if state.get("start") == START else "новое начало" if state.get("start") else "начало"
        cancel(client, state)
        # номера новостей и сюжетов дальше растут, а не начинаются заново: старый №1234 из Telegram не попадёт в другую
        # новость. Применённые команды заново не применяем: к новой раскладке они могут не подходить
        keep = {k: state[k] for k in ("spent", "next_n", "next_id", "commands_done", "commands") if k in state}
        state = {**stories.new_state(), **keep, "start": START, "fingerprint": fingerprint(), "rebuilt": now}
    known = known_sources()
    if gone := drop_gone_sources(state, vectors, known):
        print(f"Сюжеты: убрано {gone} новостей изданий, которых больше нет в sources.json", file=sys.stderr)
    since = max(START, (datetime.fromisoformat(now) - stories.LIVE).isoformat())
    wait = (datetime.fromisoformat(now) - WAIT_TRANSLATION).isoformat()
    items = [(u, a) for u, a in archive.items()
             if u not in state["items"] and a["source"] in known and stories.eligible(a) and a["seen"] >= since
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
    spend = spender(state, day)
    ask = ask_with_spending(client, spend)
    usd, merges = state.get("spent", {}).get(day, {}).get("usd", 0), len(state.get("merges", []))
    # ответы пакетов прошлых сборок — до новых новостей: склейки и выделения применяем к раскладке, которую видела модель
    arrived = collect(client, state, now, spend)
    checked = found = 0
    merged = apply_merge(state, now, *arrived["merge"][:2]) if "merge" in arrived else 0
    if "lines" in arrived:
        checked, found = apply_lines(state, now, *arrived["lines"][:2])
    if "recheck" in arrived:
        apply_recheck(state, now, *arrived["recheck"][:2])
    # по сборкам, в которых новости впервые появились (seen), как в прогонах на снимках: при старте с нуля так
    # время сюжетов настоящее, а не время этой сборки. Новость, ждавшая перевода, идёт со временем прошлой сборки
    builds = {}
    for url, item in items:
        builds.setdefault(max(item["seen"], state.get("built", "")), []).append((url, item))
    decided = laid = 0
    started, step = time.monotonic(), max(len(items) // 10, 1)  # большая раскладка — в лог примерно каждые 10%
    for seen, group in sorted(builds.items()):
        decided += stories.update(state, group, seen, vectors, ask, later=not rebuilt)["decided"]
        if stories.merge_due(state, seen) and "merge" not in state.get("batches", {}):
            merged += (stories.merge(state, seen, vectors, ask)["merged"] if rebuilt
                       else merge_later(client, state, seen, vectors, ask))
        laid += len(group)
        if len(items) >= PROGRESS_FROM and laid // step > (laid - len(group)) // step and laid < len(items):
            took = time.monotonic() - started
            print(f"Сюжеты: разложено {laid} из {len(items)} ({laid * 100 // len(items)}%), склеено {merged}, "
                  f"≈ ${state['spent'].get(day, {}).get('usd', 0) - usd:.2f}, прошло {took / 60:.0f} мин, "
                  f"осталось ≈ {took * (len(items) - laid) / laid / 60:.0f} мин", file=sys.stderr)
    if len(items) >= PROGRESS_FROM:
        print(f"Сюжеты: разложено всё за {(time.monotonic() - started) / 60:.0f} мин; "
              "имена и поиск отдельных событий в больших сюжетах…", file=sys.stderr)
    recheck_later(client, state, now, ask)
    name_stories(state, vectors, ask)
    # сюжеты, которые сейчас в топе, — для вечерней проверки и деления: на отдельные события проверяем только топ
    # (сюжеты за день, хоть раз бывшие в нём), остальные читатель почти не открывает
    cutoff = (datetime.fromisoformat(now) - PAGE_WINDOW).isoformat()
    stories_audit.note_top(state, day, {sid: heat(state, sid, now) for sid, s in state["stories"].items()
                                        if s["updated"] >= cutoff and stories.visible(state, sid)})
    top = set(state["top"][day])
    if rebuilt:
        lines = stories.find_lines(state, now, ask, top)
        name_spun(state, lines["spun"])
        checked, found = checked + lines["checked"], found + lines["found"]
    elif "lines" not in state.get("batches", {}):
        sids = stories.lines_todo(state, top)
        calls = [(stories.LINES_PROMPT, stories.lines_request(state, sid), stories.LINES_SCHEMA, stories.LINES_EFFORT,
                  stories.MERGE_MODEL, None) for sid in sids]
        sent = {"sids": sids, "sizes": [len(state["stories"][sid]["urls"]) for sid in sids]}
        if got := send(client, state, "lines", calls, sent, now, ask):
            c, f = apply_lines(state, now, sent, got[0])
            checked, found = checked + c, found + f
    # проверка дня — раз в сутки после AUDIT_HOUR, пакетом; отчёт — в сборке, в которую придут ответы.
    # Ошибка проверки не роняет сборку: не пришли ответы — отправим заново в следующей
    audit, audit_day = None, ""
    if "audit" in arrived or stories_audit.due(state, now, LOCAL_TZ):
        try:
            if "audit" in arrived:
                sent, answers, spent_usd = arrived["audit"]
                audit_day = sent["day"]
                audit = stories_audit.finish(state, audit_day, sent["sids"], sent["chunks"], sent["sizes"], answers,
                                             spent_usd, ask)
            else:
                audit_day = stories_audit.due(state, now, LOCAL_TZ)
                sids, chunks = stories_audit.todo(state, audit_day)
                calls = [(stories_audit.AUDIT_PROMPT, stories_audit.request(state, chunk), stories_audit.AUDIT_SCHEMA,
                          stories_audit.AUDIT_EFFORT, stories_audit.AUDIT_MODEL, stories_audit.AUDIT_PRICE) for chunk in chunks]
                sizes = {sid: len(state["stories"][sid]["urls"]) for chunk in chunks for sid in chunk}
                sent = {"day": audit_day, "sids": sids, "chunks": chunks, "sizes": sizes}
                if got := send(client, state, "audit", calls, sent, now, ask):
                    audit = stories_audit.finish(state, audit_day, sids, chunks, sizes, *got, ask)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            print(f"Сюжеты: проверка дня не прошла — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
    if rebuilt:  # во сколько обошлась раскладка заново — отдельно: в обычный день её нет
        today = state["spent"].setdefault(day, {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
        today["rebuild"] = round(today.get("rebuild", 0) + today["usd"] - usd, 6)
    # оригиналы новостей старше архива, разложенных до 18.11, — из снимков архива (snapshot.py), один раз: файл
    # можно удалить, когда сборка их перенесла (у всех новостей в состоянии есть orig)
    backfill = json.loads(ORIG_FILE.read_text("utf-8")) if ORIG_FILE.exists() else {}
    # время публикации из RSS — для порядка новостей в сюжете. Позже, чем мы её увидели, — неверный часовой пояс у
    # издания (RTS и др.): тогда время, когда увидели
    for u, it in state["items"].items():
        if "published" not in it and u in archive:
            it["published"] = min(archive[u].get("date") or it["seen"], it["seen"])
        # оригинальный заголовок — для /sr/plot/: в it["title"] — перевод, а архив помнит новость только ARCHIVE_DAYS
        if "orig" not in it and (archive.get(u, {}).get("title") or u in backfill):
            it["orig"] = archive.get(u, {}).get("title") or backfill[u]
    # что сделала эта сборка — сводкой наверху страницы
    state["last"] = {"t": now, "rebuilt": rebuilt, "news": [u for u, _ in items], "commands": [c["result"] for c in done],
                     "merges": [[m["keep"], m["gone"]] for m in state.get("merges", [])[merges:]],
                     "lines": [checked, found],
                     "usd": round(state.get("spent", {}).get(day, {}).get("usd", 0) - usd, 4)}
    forget_old(state, vectors, now)
    state["spent"] = {d: s for d, s in sorted(state.get("spent", {}).items())[-SPENT_DAYS:]}
    state["built"] = now
    try:
        sr_headlines(state, vectors, ask)
    except Exception as exc:  # noqa: BLE001 — без перевода /sr/plot/ покажет оригинальный заголовок
        traceback.print_exc()
        print(f"Сюжеты: заголовки по-сербски не переведены — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
    save_state(state, vectors)
    if done:  # ответы в Telegram — после сохранения: не сохранилось — команды применятся заново в следующей сборке
        translate.reports_api({"results": [{"message": c.get("message"), "text": c["result"]} for c in done]}, "stories")
    if audit is not None:
        a = state["audits"][audit_day]
        print(f"Сюжеты: проверка дня — {a['flagged']} из {a['news']} новостей не на месте, ≈ ${a['usd']:.2f}", file=sys.stderr)
        messages = stories_audit.report(state, audit_day, audit)
        for k in range(0, len(messages), stories_audit.MESSAGES):  # приёмник берёт не больше MESSAGES за раз
            translate.reports_api({"results": messages[k:k + stories_audit.MESSAGES]}, "stories")
    today = state["spent"].get(day, {})
    print(f"Сюжеты: решено {decided}, склеено {merged}, сюжетов {len(state['stories'])}, "
          f"за сегодня ≈ ${today.get('usd', 0):.3f} (входа из кэша {today.get('read', 0) * 100 // max(today.get('in', 0), 1)}%"
          f"{'; ' + by_steps(today) if today.get('steps') else ''})", file=sys.stderr)
    write_page(state, vectors, sections)
    write_sr_page(state, vectors, archive)


def by_steps(spent: dict) -> str:
    """Расход дня по шагам, дорогие первыми; пересборка — отдельно (она входит в шаги)."""
    parts = [f"{k} ${v['usd']:.2f}" for k, v in sorted(spent.get("steps", {}).items(), key=lambda kv: -kv[1]["usd"])]
    if spent.get("rebuild"):
        parts.append(f"из них раскладка заново ${spent['rebuild']:.2f}")
    return " · ".join(parts)


def count(n: int, one: str, few: str, many: str) -> str:
    """Число со словом: 1 новость, 3 новости, 5 новостей."""
    word = one if n % 10 == 1 and n % 100 != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many
    return f"{n} {word}"


NEWS, OUTLETS = ("новость", "новости", "новостей"), ("издание", "издания", "изданий")
LINK_NAMES = {"same": "то же событие", "development": "развитие", "consequence": "последствие", "reaction": "реакция"}


def heat(state: dict, sid: str, now: str) -> float:
    return stories.heat(state, sid, now)


def typical(state: dict, vectors: dict, urls: list[str], fits) -> str | None:
    """Самая типичная из свежих новостей сюжета (HEADLINE_RECENT до последней), подходящих под fits: больше всех
    похожая на остальные такие же. Свежих нет — из всех подходящих; подходящих с вектором нет — None."""
    last = max(datetime.fromisoformat(state["items"][u]["seen"]) for u in urls)
    good = [u for u in urls if u in vectors and fits(u)]
    good = [u for u in good if last - datetime.fromisoformat(state["items"][u]["seen"]) <= HEADLINE_RECENT] or good
    if not good:
        return None
    v = np.stack([vectors[u] for u in good]).astype(np.float32)
    return good[int((v @ v.T).mean(axis=1).argmax())]


def page_stories(state: dict, now: str) -> tuple[list[str], dict[str, float], int]:
    """Сюжеты страницы и их порядок — общие для /plot/ и /sr/plot/: видимые за PAGE_WINDOW, по горячести, SHOW_TOP
    первых. (сюжеты, горячесть, сколько видимых всего)."""
    cutoff = (datetime.fromisoformat(now) - PAGE_WINDOW).isoformat()
    shown = [sid for sid, s in state["stories"].items() if s["updated"] >= cutoff and stories.visible(state, sid)]
    heats = {sid: heat(state, sid, now) for sid in shown}
    shown.sort(key=lambda sid: (heats[sid], state["stories"][sid]["updated"]), reverse=True)
    return shown[:stories.SHOW_TOP], heats, len(shown)


def headline_url(state: dict, vectors: dict, urls: list[str]) -> str:
    """Новость, по которой назван сюжет: самая типичная из свежих с what (typical), без них — первая."""
    return typical(state, vectors, urls, lambda u: bool(state["items"][u].get("what"))) or urls[0]


def headline_of(state: dict, vectors: dict, urls: list[str]) -> str:
    """Пока у сюжетов нет своих названий — «что произошло» (what, ровный пересказ без кликбейта из перевода)
    новости headline_url. Без what — её заголовок."""
    it = state["items"][headline_url(state, vectors, urls)]
    return it.get("what") or it["title"]


def write_page(state: dict, vectors: dict, sections: dict, error: str = "") -> None:
    """site/plot/index.html — статичная страница: видимые сюжеты за PAGE_WINDOW, крупные сверху; у каждого видимого
    сюжета — своя страница site/plot/sX/."""
    e = html.escape
    names = state.get("names", {})
    now = state.get("built") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    cutoff = (datetime.fromisoformat(now) - PAGE_WINDOW).isoformat()
    shown, heats, visible_count = page_stories(state, now)

    def sources(sid):
        return {state["items"][u]["source"] for u in state["stories"][sid]["urls"]}

    def local_time(iso: str) -> str:
        return datetime.fromisoformat(iso).astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")

    def headline(urls: list[str]) -> str:
        return headline_of(state, vectors, urls)

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
        if it["history"][0]["story"] and it["history"][0]["why"] not in ("join", "copy"):
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
        elif first["why"] == "copy":
            text = f'копия №{first.get("of", "?")} — легла за ней в {where(first["story"])}' + (
                f' (сходство {first["sim"]:.3f})' if sim and first.get("sim") else "")
        else:
            others = sorted(m for m in opened.get((first["story"], first["t"]), []) if m != it["n"])
            text = (f'открыла {where(first["story"])}' + (" вместе с " + ", ".join(f"№{m}" for m in others) if others else "")
                    + {"alone": ": похожих сюжетов не было", "new": ": модель сочла событие новым",
                       "unclear": ": модель не поняла, о чём новость"}.get(first["why"], ""))
        steps = {"recheck": "перепроверка не согласилась{event} — новость ушла в {}", "merge": "её сюжет {from_} влит в {}{event}",
                 "line": "выделена отдельным событием в {}", "move": "перенесена командой /move в {}", "split": "отделена командой /split в {}",
                 "unmerge": "склейка отменена командой /unmerge — вернулась в {}"}
        for h in later:
            text += "; " + steps.get(h["why"], h["why"] + " → {}").format(
                where(h["story"]) if h["story"] else "без сюжета", from_=e(h.get("from") or "—"),
                event=(f" ({e(h['link'])}: {e(h['event'])})" if h.get("link") else f" (общее: {e(h['event'])})")
                if h.get("event") else "")
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

    came = {}  # часть → склейка, которой она пришла: своя склейка сюжета-части важнее той, что принесла её в составе другого
    for m in state.get("merges", []):
        for p in m["parts"]:
            if p == m["gone"] or p not in came:
                came[p] = m

    def chain(sid: str) -> list[dict]:
        """Сюжет как цепочка событий — проба двух уровней (заметка «Событие и цепочка»): событие — часть сюжета
        (stories.py: сюжет, в котором новость появилась), связь — та, что назвала склейка (журнал state["merges"]).
        Часть, пришедшая как same, — то же событие, что и то, в которое её влили. По времени первой новости;
        у события: urls, link, event (общее по словам склейки), parent — к какому событию связь, t — когда влито."""
        by_part = {}
        for u in state["stories"][sid]["urls"]:
            by_part.setdefault(state["items"][u]["part"], []).append(u)

        def first(urls: list[str]) -> str:
            return min(state["items"][u].get("published", state["items"][u]["seen"]) for u in urls)

        root = sid if sid in by_part else min(by_part, key=lambda p: first(by_part[p]))

        def parent(p: str) -> str:
            keep = came[p]["keep"] if p in came else root
            return keep if keep in by_part and keep != p else root

        def event_of(p: str) -> str:  # same — в то же событие; круг (склеили, отменили, склеили иначе) обрываем
            seen = set()
            while p != root and p not in seen and came.get(p, {}).get("link") == "same":
                seen.add(p)
                p = parent(p)
            return p

        events = {}
        for p, urls in by_part.items():
            events.setdefault(event_of(p), []).extend(urls)
        out = [{"part": p, "urls": urls, "first": first(urls), "main": p == root,
                **({"link": came[p].get("link"), "event": came[p].get("event"), "t": came[p]["t"],
                    "parent": event_of(parent(p))} if p in came and p != root else {})}
               for p, urls in events.items()]
        return sorted(out, key=lambda ev: ev["first"])

    chains = {}

    def chain_note(sid: str) -> str:
        """Цепочка событий сюжета, если их больше одного: строка на событие. Новости ниже — по-прежнему общим списком."""
        chains[sid] = events = chain(sid)
        if len(events) < 2:
            return ""
        titles = {ev["part"]: headline(ev["urls"]) for ev in events}
        root = next(ev["part"] for ev in events if ev["main"])

        def short(text: str) -> str:
            return text if len(text) <= 60 else text[:60].rsplit(" ", 1)[0] + "…"

        def row(ev: dict) -> str:
            numbers = sorted(state["items"][u]["n"] for u in ev["urls"])
            if ev["main"]:
                link = "основное событие"
            elif "t" not in ev:
                link = "новости перенесены из другого сюжета (перепроверка или команда), связь не названа"
            else:
                link = (f'{LINK_NAMES.get(ev["link"], ev["link"]) if ev["link"] else "связь не записана (склейка до 16.11)"}'
                        + (f' к «{e(short(titles[ev["parent"]]))}»' if ev["parent"] != root and ev["parent"] in titles else "")
                        + (f': {e(ev["event"])}' if ev.get("event") else "") + f' · влито {e(local_time(ev["t"]))}')
            return (f'<li>{e(local_time(ev["first"]))} · <b>{e(titles[ev["part"]])}</b><br><span class="meta">{link} · '
                    f'{count(len(ev["urls"]), *NEWS)} · {count(len({state["items"][u]["source"] for u in ev["urls"]}), *OUTLETS)}'
                    f' · {", ".join(f"№{n}" for n in numbers[:10])}{" …" if len(numbers) > 10 else ""}</span></li>')

        return ('<div class="lines">Цепочка событий — проба: события — части сюжета, связи назвала склейка. '
                f'Новости ниже — общим списком.<ul>{"".join(row(ev) for ev in events)}</ul></div>')

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

        # новости по частям (stories.py) не делим: 12.11 части делили сюжеты случайно, по тому, где новость оказалась
        # первой. Части со связями склейки — отдельной строкой над списком (chain_note), список общий
        body = rows(urls)
        about = (f'{f"горячесть {heats[sid]:.1f} · " if sid in heats else ""}'
                 f'{count(len(urls), *NEWS)} · {count(len(sources(sid)), *OUTLETS)} · '
                 f'{e(", ".join(f"{c} {n}" for c, n in camps.most_common()))} · '
                 f'{local_time(story["created"])} — {local_time(story["updated"])} · {sid}'
                 f'{f" «{e(names[sid])}»" if sid in names else ""}'
                 f'{" · выделен из " + page_link(state["spun"][sid]) if sid in state.get("spun", {}) else ""}')
        mark = {"serbia": "в Сербии", "affects": "за рубежом, касается Сербии",
                "foreign": f"за рубежом, без связи с Сербией — горячесть ×{stories.FOREIGN_HEAT}"}.get(state.get("serbia", {}).get(sid))
        card = (f'<p class="meta">Событие сюжета: {e(state["cards"][sid])}{f" · {e(mark)}" if mark else ""}</p>'
                if sid in state.get("cards", {}) else "")
        return headline(urls), about, card + chain_note(sid) + lines_note(sid), body

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
    hidden = [u for sid, s in state["stories"].items() if s["updated"] >= cutoff and not stories.visible(state, sid)
              for u in s["urls"]]
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
        args = {"move": lambda: f"{' '.join(map(str, c['news'])) if isinstance(c['news'], list) else c['news']} {c['to']}",
                "split": lambda: " ".join(c["parts"]), "unmerge": lambda: c["story"],
                "fix": lambda: f"{c['day']} {c['which']} (кнопка отчёта)"}.get(c["do"], lambda: "")()
        return f"<li>{e(local_time(c['applied']))} · /{e(c['do'])} {e(args)} — {e(c['result'])}</li>"

    commands = "".join(command(c) for c in reversed(state.get("commands", [])))

    spent = "".join(f"<tr><td>{d}</td><td>{s['calls']}</td><td>${s['usd']:.3f}</td><td>{e(by_steps(s))}</td></tr>"
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
        kinds["без сюжета" if not h["story"] else "добавлены в сюжет" if h["why"] == "join"
              else "копии" if h["why"] == "copy" else "открыли сюжет"] += 1
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
            f"запросы к модели ≈ ${last.get('usd', 0):.3f}",
            "ждут ответа модели: " + ", ".join(f"{BATCH_KINDS[k]} (с {e(local_time(b['t']))})"
                                               for k, b in state.get("batches", {}).items()) if state.get("batches") else ""]
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
<p class="meta">Сборка {e(local_time(now))} · видимых сюжетов (от двух изданий) за сутки: {visible_count}, на странице {stories.SHOW_TOP} самых горячих · всего в памяти: {total}.
Новости с {e(local_time(START))}; правила {e(state.get("fingerprint", ""))}, по ним разложено с {e(local_time(state.get("rebuilt", now)))}:
поменялись правила — страница раскладывается заново целиком. Прежняя страница сюжетов-тем — <a href="/plot1/">/plot1/</a>.
Страница не индексируется и на сайте нигде не упомянута.
Цепочки событий (проба): из {len(shown)} сюжетов страницы больше одного события в {sum(len(chains[sid]) > 1 for sid in shown)}.</p>
{f'<p>В этой сборке: {summary}.</p>' if summary else ""}
{f'<details><summary>Новости этой сборки — что сделала каждая</summary>{rebuilt_note}<ul>{arrived}</ul></details>' if arrived else ""}
{f'<p class="err">Сюжеты в этой сборке не обновились: {e(error)}</p>' if error else ""}
<p class="meta">Сюжеты — по горячести: каждое издание даёт очко за свою последнюю новость в сюжете, очко вдвое тает
за {int(stories.HEAT_HALF_LIFE.total_seconds() // 3600)} ч (час назад — почти 1, {int(stories.HEAT_HALF_LIFE.total_seconds() // 3600)} ч назад — 0.5, сутки назад — {0.5 ** (24 / (stories.HEAT_HALF_LIFE.total_seconds() / 3600)):.2f}).
Сюжет о событии за рубежом без прямой связи с Сербией (так решает карточка) горяч в {1 / stories.FOREIGN_HEAT:.0f} раза меньше.
Цифра у новости — сходство с самой похожей другой новостью сюжета (у одиночных — с новостью видимого сюжета).
Модель видит новость с сюжетом, только если сходство не ниже {stories.CANDIDATE_SIM}; <span class="sim mid">жёлтые</span> — ниже
{stories.RECHECK_SIM}, такое добавление (и любое в сюжет с карточкой) перепроверяет Haiku, в сюжет этой страницы со сходством ниже {stories.RECHECK_SONNET_MAX} — Sonnet (пакетом: снятая новость уходит через сборку); копии (сходство от {stories.COPY_SIM}) идут за оригиналом без модели; <span class="sim low">красные</span> — ниже {stories.CANDIDATE_SIM}.
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
<h2 id="spent">Расход</h2><table><tr><td>день</td><td>запросов</td><td>≈ $</td><td>по шагам</td></tr>{spent}</table>
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


SR_PAGE_DIR = ROOT / "site" / "sr" / "plot"
SOURCES_FILE = ROOT / "sources.json"
SR_NEWS, SR_OUTLETS = ("vest", "vesti", "vesti"), ("medij", "medija", "medija")
# заголовок сюжета на /sr/plot/ — тот же, что на /plot/ (headline_of), переведённый на сербский: оригинальные заголовки
# изданий бывают кликбейтом, а русский — ровный пересказ (what). Переводится только новый текст заголовка; кэш —
# state["sr_heads"] {русский: сербский}, в нём только заголовки нынешней страницы. Haiku, около цента в день
SR_HEAD_PROMPT = """Ты переводишь заголовки сюжетов агрегатора новостей о Сербии с русского на сербский язык, латиницей.
Вход — JSON: heads — заголовки, id вида H…: ru — заголовок по-русски, orig — оригинальный заголовок новости, по которой он написан (бывает пустым, на сербском, английском или русском).
Для каждого верни text — точный перевод ru: тот же смысл и тот же нейтральный тон, без добавлений и без кликбейта. Имена людей, мест и организаций — в сербском написании латиницей; как они пишутся, смотри в orig (Vukićević, Galerija, Stara Pazova, SNS, REM). Точку в конце не ставь."""
SR_HEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "heads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
                "required": ["id", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["heads"],
    "additionalProperties": False,
}


def sr_headlines(state: dict, vectors: dict, ask) -> int:
    """Перевод заголовков сюжетов страницы на сербский (SR_HEAD_PROMPT), только новых. Возвращает, сколько перевели."""
    shown, _, _ = page_stories(state, state["built"])
    heads = {}  # русский заголовок → оригинал новости, по которой он написан
    for sid in shown:
        urls = state["stories"][sid]["urls"]
        heads.setdefault(headline_of(state, vectors, urls), state["items"][headline_url(state, vectors, urls)].get("orig", ""))
    cache = state.get("sr_heads", {})
    todo = [ru for ru in heads if ru not in cache]
    if todo:
        content = json.dumps({"heads": [{"id": f"H{k}", "ru": ru, "orig": heads[ru]} for k, ru in enumerate(todo)]},
                             ensure_ascii=False)
        answer, _ = ask(SR_HEAD_PROMPT, content, SR_HEAD_SCHEMA)
        for h in answer["heads"]:
            k = h["id"][1:]
            if k.isdigit() and int(k) < len(todo) and h["text"].strip():
                cache[todo[int(k)]] = stories.clean_name(h["text"])
    state["sr_heads"] = {ru: cache[ru] for ru in heads if ru in cache}
    return len(todo)


def write_sr_page(state: dict, vectors: dict, archive: dict) -> None:
    """site/sr/plot/index.html — /plot/ для читателя по-сербски: те же сюжеты в том же порядке, тот же заголовок
    (переведённый, sr_headlines), те же новости в том же порядке — с оригинальными заголовками. Служебного нет.
    Ошибка здесь не роняет сюжеты: страница пробная."""
    try:
        _write_sr_page(state, vectors, archive)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print("Сюжеты: сербская страница не обновилась", file=sys.stderr)


SR_FIRST = 5  # сколько свежих новостей сюжета видно на главной; все — на странице сюжета
SR_CAMP_FIRST = 8  # на странице сюжета — сколько новостей каждой стороны видно сразу
SR_COLORS = {"independent": "#2563eb", "investigations": "#0d9488", "progov": "#dc2626", "state": "#db2777",
             "mainstream": "#6b7280", "economy": "#ca8a04", "foreign-sr": "#7c3aed", "foreign": "#d97706",
             "reddit": "#ff4500", "trends": "#1a73e8", "regional": "#65a30d"}  # как --c-* в template.html
SR_STYLE = """:root{--bg:#fff;--text:#333;--strong:#000;--muted:#8a8a8a;--faint:#a8a8a8;--soft:#ececec;--hover:#9b9b9b;COLORS}
@media (prefers-color-scheme:dark){:root{--bg:#282c35;--text:#ddd;--strong:#fff;--muted:#9a9ca3;--faint:#7c7f87;--soft:#383d49}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.3 "Nunito",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
a{color:inherit}
.wrap{max-width:820px;margin:0 auto;padding:18px 20px 40px}
.brand{display:inline-flex;align-items:center;gap:10px;text-decoration:none;color:var(--muted);font-weight:700;font-size:14px}
.brand img{width:22px;height:22px}.brand:hover{color:var(--strong)}
h1{margin:14px 0 4px;font-size:32px;font-weight:900;color:var(--strong);line-height:1.15}
.lead{margin:0 0 8px;color:var(--muted);font-size:14px}
.story{padding:26px 0 12px;border-bottom:1.5px solid var(--soft)}
.story h2{margin:0;font-size:22px;line-height:1.25;font-weight:700;color:var(--strong);display:flex;gap:12px}
.story h2 a{text-decoration:none}.story h2 a:hover{color:var(--hover)}
.rank{flex:none;min-width:22px;color:var(--faint);font-weight:900}
.meta{margin:6px 0 10px;color:var(--muted);font-size:13px}
.bar{display:flex;gap:2px;height:6px;border-radius:3px;overflow:hidden}
.legend{margin:6px 0 14px;color:var(--muted);font-size:12px;line-height:1.7}
.legend>span{display:inline-flex;align-items:center;gap:5px;margin-right:12px;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;flex:none;display:inline-block}
.items{list-style:none;margin:0;padding:0}
.row{display:flex;gap:10px;margin-bottom:9px}
.time{flex:none;width:44px;white-space:nowrap;color:var(--faint);font-size:12px;padding-top:2px;font-variant-numeric:tabular-nums}
.head{flex:1;min-width:0;text-decoration:none;overflow-wrap:anywhere}
.head:hover{color:var(--hover)}
.src{font-size:13px;font-weight:700;color:var(--muted);display:inline-flex;align-items:center;gap:5px;margin-right:6px;white-space:nowrap}
.all{display:inline-block;margin:2px 0 8px;color:var(--muted);font-size:14px;font-weight:700;text-decoration:none}
.all:hover{color:var(--strong)}
.indent{margin-left:34px}
.seg{display:inline-flex;background:var(--soft);border-radius:20px;padding:3px;margin:18px 0 6px}
.seg button{border:0;background:none;padding:5px 14px;border-radius:20px;cursor:pointer;font:inherit;font-size:14px;color:var(--muted)}
.seg button[aria-pressed=true]{background:var(--strong);color:var(--bg);font-weight:700}
.group{margin:22px 0 4px;font-size:17px;font-weight:700;color:var(--strong);display:flex;align-items:center;gap:8px}
.group .n{color:var(--faint);font-weight:400;font-size:14px}
.more summary{cursor:pointer;color:var(--muted);font-size:14px;font-weight:700;padding:2px 0 8px;list-style:none}
.more summary::-webkit-details-marker{display:none}.more summary::after{content:" ↓"}.more[open] summary::after{content:" ↑"}
.more summary:hover{color:var(--strong)}
.nav{display:flex;justify-content:space-between;gap:16px;margin-top:28px;font-size:14px;font-weight:700;color:var(--muted)}
.nav a{text-decoration:none}.nav a:hover{color:var(--strong)}
@media (max-width:600px){.wrap{padding:14px 16px 32px}h1{font-size:26px}.story h2{font-size:19px;gap:8px}.indent{margin-left:0}.head{font-size:15px}}
""".replace("COLORS", "".join(f"--c-{c}:{v};" for c, v in SR_COLORS.items()))
SR_HEAD = """<!doctype html><html lang="sr-Latn"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>{title}</title>
<link rel="icon" href="/favicon.ico" sizes="32x32"><link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;700;900&display=swap">
<style>{style}</style></head><body><div class="wrap">"""
# время новости — как на главной: «5 min», «3 h», старше суток — дата. Переключатель на странице сюжета — по сторонам
# (разделам) или по времени (по дням)
SR_SCRIPT = """<script>
const now = Date.now();
document.querySelectorAll('time.time').forEach(t => {
  const at = Date.parse(t.getAttribute('datetime')), m = Math.round((now - at) / 60000);
  if (m < 0) return;
  if (m < 60) t.textContent = m + ' min';
  else if (m < 24 * 60) t.textContent = Math.floor(m / 60) + ' h';
  else { const d = new Date(at); t.textContent = d.getDate() + '.' + (d.getMonth() + 1) + '.'; }
});
document.querySelectorAll('.seg button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.seg button').forEach(x => x.setAttribute('aria-pressed', x === b));
  document.querySelectorAll('[data-view]').forEach(v => v.hidden = v.dataset.view !== b.dataset.show);
}));
</script>"""


def _write_sr_page(state: dict, vectors: dict, archive: dict) -> None:
    """Оформление — как у главной (template.html): Nunito, те же цвета и строки новостей «время · издание заголовок».
    Главная /sr/plot/: сюжет — номер, заголовок, полоса лагерей (доля новостей каждого раздела), SR_FIRST свежих
    новостей и ссылка на его страницу /sr/plot/sX/: там все новости — по сторонам (разделам) или по времени (по дням).
    Состав и порядок — как на /plot/."""
    e = html.escape
    config = json.loads(SOURCES_FILE.read_text("utf-8"))
    camp_names = {s["id"]: s["title"] for s in config["sections"]}
    camp_order = [s["id"] for s in config["sections"]]
    now = state.get("built") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    shown, _, _ = page_stories(state, now)
    sr_heads = state.get("sr_heads", {})
    days = ["ponedeljak", "utorak", "sreda", "četvrtak", "petak", "subota", "nedelja"]
    months = ["januara", "februara", "marta", "aprila", "maja", "juna", "jula", "avgusta", "septembra", "oktobra",
              "novembra", "decembra"]

    def local(iso: str, fmt: str) -> str:
        return datetime.fromisoformat(iso).astimezone(LOCAL_TZ).strftime(fmt)

    def orig(u: str) -> str:  # старше архива и без orig — остаётся только перевод
        it = state["items"][u]
        return it.get("orig") or archive.get(u, {}).get("title") or it["title"]

    def when(u: str) -> str:
        return state["items"][u].get("published", state["items"][u]["seen"])

    def dot(section: str) -> str:
        return f'<span class="dot" style="background:var(--c-{e(section)})"></span>'

    def row(u: str) -> str:
        it = state["items"][u]
        return (f'<li class="row"><time class="time" datetime="{e(when(u))}">{local(when(u), "%H:%M")}</time>'
                f'<a class="head" href="{e(u)}" target="_blank" rel="noopener"><span class="src">{dot(it["section"])}'
                f'{e(it["source"])}</span>{e(orig(u))}</a></li>')

    def day_name(iso: str) -> str:  # без «danas / juče»: страница статична, а читают её и назавтра
        d = datetime.fromisoformat(iso).astimezone(LOCAL_TZ).date()
        return f"{days[d.weekday()].capitalize()}, {d.day}. {months[d.month - 1]}"

    def head(title: str) -> str:
        return SR_HEAD.format(title=e(title), style=SR_STYLE)

    brand = '<a class="brand" href="/"><img src="/icon.svg" alt="">Vestomat</a>'
    cards, pages = [], {}
    for k, sid in enumerate(shown, 1):
        story = state["stories"][sid]
        urls = story["urls"]
        items = [state["items"][u] for u in urls]
        title = sr_heads.get(headline_of(state, vectors, urls)) or orig(headline_url(state, vectors, urls))
        # стороны — по числу изданий, а не новостей: «Bliski SNS-u» пишут одно и то же десятком заметок и иначе
        # всегда наверху. Порядок сторон — постоянный (как в sources.json), чтобы полосы сюжетов сравнивались
        per_camp = Counter(c for c, _ in {(it["section"], it["source"]) for it in items})
        camps = sorted(per_camp.items(), key=lambda cn: camp_order.index(cn[0]) if cn[0] in camp_order else 99)
        bar = "".join(f'<span style="flex:{n};background:var(--c-{e(c)})" title="{e(camp_names.get(c, c))}: {n}"></span>'
                      for c, n in camps)
        legend = "".join(f'<span>{dot(c)}{e(camp_names.get(c, c))} {n}</span>' for c, n in camps)
        news = sorted(urls, key=lambda u: (when(u), state["items"][u]["n"]), reverse=True)
        outlets = len({it["source"] for it in items})
        meta = (f'{count(len(urls), *SR_NEWS)} · {count(outlets, *SR_OUTLETS)} · od {local(story["created"], "%d.%m.")}')
        balance = (f'<div class="bar" role="img" aria-label="{e(", ".join(f"{camp_names.get(c, c)} {n}" for c, n in camps))}">'
                   f'{bar}</div><p class="legend">{legend}</p>')
        more = (f'<a class="all" href="{sid}/">Sve {count(len(urls), *SR_NEWS)} →</a>' if len(news) > SR_FIRST
                else f'<a class="all" href="{sid}/">Stranica teme →</a>')
        # на главной — SR_FIRST новостей сторон по очереди: последняя новость каждого издания, стороны в постоянном
        # порядке, круг за кругом (как блок «с каждой стороны» на главной сайта); показываем по времени
        latest = {}
        for u in news:
            latest.setdefault(state["items"][u]["source"], u)
        queues = {c: [u for u in latest.values() if state["items"][u]["section"] == c] for c, _ in camps}
        preview = []
        while len(preview) < SR_FIRST and any(queues.values()):
            for c, _ in camps:
                if queues[c] and len(preview) < SR_FIRST:
                    preview.append(queues[c].pop(0))
        preview.sort(key=lambda u: (when(u), state["items"][u]["n"]), reverse=True)
        cards.append(
            f'<article class="story" id="{sid}"><h2><span class="rank">{k}</span><a href="{sid}/">{e(title)}</a></h2>'
            f'<div class="indent"><p class="meta">{meta}</p>{balance}'
            f'<ul class="items">{"".join(row(u) for u in preview)}</ul>{more}</div></article>')

        # страница сюжета: по сторонам — в постоянном порядке, внутри свежие сверху; по времени — по дням
        def camp_block(c: str, n: int) -> str:  # SR_CAMP_FIRST свежих, остальные стороны — раскрываются на месте
            mine = [u for u in news if state["items"][u]["section"] == c]
            rest = mine[SR_CAMP_FIRST:]
            more = (f'<details class="more"><summary>Još {count(len(rest), *SR_NEWS)}</summary>'
                    f'<ul class="items">{"".join(row(u) for u in rest)}</ul></details>' if rest else "")
            return (f'<h3 class="group">{dot(c)}{e(camp_names.get(c, c))} <span class="n">{count(n, *SR_OUTLETS)} · '
                    f'{count(len(mine), *SR_NEWS)}</span></h3>'
                    f'<ul class="items">{"".join(row(u) for u in mine[:SR_CAMP_FIRST])}</ul>{more}')

        by_camp = "".join(camp_block(c, n) for c, n in camps)
        by_day, current = [], None
        for u in news:
            d = day_name(when(u))
            if d != current:
                by_day.append(("</ul>" if current else "") + f'<h3 class="group">{e(d)}</h3><ul class="items">')
                current = d
            by_day.append(row(u))
        by_day.append("</ul>")
        prev = f'<a href="../{shown[k - 2]}/">← Prethodna tema</a>' if k > 1 else "<span></span>"
        after = f'<a href="../{shown[k]}/">Sledeća tema →</a>' if k < len(shown) else "<span></span>"
        pages[sid] = f"""{head(f"{title} — Vestomat")}
<a class="brand" href="../">← Teme dana</a>
<h1>{e(title)}</h1>
<p class="meta">{k}. tema dana · {meta} · poslednja {local(story["updated"], "%d.%m. u %H:%M")}</p>
{balance}
<div class="seg" role="group"><button data-show="camps" aria-pressed="true">Po stranama</button><button data-show="time" aria-pressed="false">Po vremenu</button></div>
<div data-view="camps">{by_camp}</div>
<div data-view="time" hidden>{"".join(by_day)}</div>
<nav class="nav">{prev}{after}</nav>
</div>{SR_SCRIPT}</body></html>"""

    page = f"""{head("Teme dana — Vestomat")}
{brand}
<h1>Teme dana</h1>
<p class="lead">O čemu piše najviše medija u poslednja 24 sata · ažurirano {local(now, "%d.%m. u %H:%M")} · naslovi vesti su originalni ·
traka pokazuje koliko je medija sa svake strane pisalo o temi</p>
{"".join(cards) or "<p>Još nema tema.</p>"}
</div>{SR_SCRIPT}</body></html>"""
    SR_PAGE_DIR.mkdir(parents=True, exist_ok=True)
    (SR_PAGE_DIR / "index.html").write_text(page, "utf-8")
    for sid, text in pages.items():
        (SR_PAGE_DIR / sid).mkdir(exist_ok=True)
        (SR_PAGE_DIR / sid / "index.html").write_text(text, "utf-8")


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
            write_sr_page(*load_state(), archive)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


if __name__ == "__main__":
    config = json.loads((ROOT / "sources.json").read_text("utf-8"))
    archive_file = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "cache" / "archive.json"
    safe_run(json.loads(archive_file.read_text("utf-8")), {s["id"]: s["title_ru"] for s in config["sections"]})
