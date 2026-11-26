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
import re
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

import daily
import live
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
# новость, опубликованная раньше, чем мы её увидели, на столько, в темы не идёт: это расследования и длинные тексты,
# которые издание отдаёт в RSS с опозданием (BIRN, KRIK, DW). 22.11 DW от 5.11 о делах против СНС попала в тему
# учредительного заседания Скупщины и та на странице «началась 5 ноября». В ленте сайта такие новости остаются
LATE = timedelta(days=3)
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
# f4bbf5a47d — до 23.11, когда в RULES цепочку этапов остановили на новом процессе: формирование правительства — не этап
# учредительного заседания. 19–20.11 склейка 4 раза влила его в s9278 как «развитие», ровно по прежнему правилу;
# проверка тем 22.11 вынесла 35 новостей. Уже разложенное остаётся, новое правило — для следующих новостей
# 2d392b8fc8 — до 24.11, когда склейка стала видеть название темы, а у большой темы вместо застывшей первой карточки —
# самые типичные новости за всё время (stories.merge_requests). Раскладка новостей не меняется
# 3be278d4b3 — до 25.11, когда то же увидела раскладка (stories._decide): разложенное остаётся, новое — для свежих новостей
SAME_RULES = {"93f151fd03", "dfadd2988f", "9e34e14300", "a0cbf341ea", "74b3c97a3d", "37beed8aa5", "842ae523cb",
              "a4183da04a", "ebdeb2625d", "a965920995", "f4bbf5a47d", "2d392b8fc8",
              "3be278d4b3"}
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
# название темы — её заголовок на страницах (headline_of). До 18.11 это было кодовое имя сюжета для редактора, данное
# один раз; у темы ситуация меняется (выборы → учредительное заседание → выбор председателя), и название — по свежим
# новостям, заново раз в NAME_EVERY, если в теме прибавилось NAME_NEW новостей. Карточка темы (stories.cards) при
# новом названии пишется заново. На раскладку не влияет, поэтому и не в отпечатке правил
NAME_PROMPT = """Ты называешь темы агрегатора новостей о Сербии. Название — заголовок темы для читателя.
Вход — JSON: stories — темы, id вида S…: name — прежнее название, если было; news — свежие и самые типичные новости темы (что произошло).
Для каждой темы верни name — по-русски, от двух до семи слов: о чём тема сейчас, по свежим новостям. Как рубрика: именительный падеж, без глагола в личной форме («Учредительное заседание Скупщины Сербии», «Крах «Алта банка»», «Травмы детей на эскалаторе в ТЦ «Галерия»»). Только слова, которые правда говорят по-русски. Отглагольное существительное — лишь привычное («задержание», «избиение», «поджог», «отставка»); не сочиняй таких, как «облитие», «закидывание», «подписание заявления об уходе». Если привычного слова нет, назови событие другим существительным или через участника: не «Облитие йогуртом депутатки Лидии Вукичевич», а «Нападение на депутатку Лидию Вукичевич» или «Инцидент с йогуртом у Скупщины». Если прежнее название нарушает эти правила — исправь его. Ситуация целиком, а не один её эпизод и не одно заявление. Если прежнее название ещё верно — верни его как есть. Без оценок, без кавычек вокруг и без точки в конце.
У большой темы есть event — её ситуация — и whole — самые типичные её новости за всё время. Её название — про всю ситуацию по event и whole: о ней пишут несколько дней, и под названием читатель видит все её новости. Свежие news — только чтобы заметить, что ситуация перешла в следующий этап; последний эпизод, спор или заявление — не название большой темы (не «Жалоба СНС на захват постов в Скупщине», а «Учредительное заседание Скупщины Сербии»)."""
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
NAME_EVERY = timedelta(hours=4)
NAME_NEW = 5
NAME_RECENT = timedelta(hours=24)  # свежие новости темы — за столько до её последней
NAME_SHOWN = 8  # сколько новостей темы видит модель: самые типичные из свежих
NAME_MAX = 80
# большая тема — по всей ситуации: 22.11 s9278 (1999 новостей за неделю учредительного заседания Скупщины) называлась
# «Жалоба СНС на захват постов в Скупщине» — по последним суткам, а с новым названием и карточку переписывали по ним
# с stories.BIG новостей тема большая; самые типичные — из stories.WHOLE_POOL её новостей, равномерно за всё время


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


def late(published: str | None, seen: str) -> bool:
    """Опубликована больше чем за LATE до того, как мы её увидели."""
    try:
        return bool(published) and datetime.fromisoformat(seen) - datetime.fromisoformat(published) > LATE
    except (ValueError, TypeError):  # нет даты или она без часового пояса
        return False


def drop_items(state: dict, vectors: dict, gone: list[str]) -> int:
    """Новости — вон из сюжетов и из памяти вместе с векторами, а сюжеты, в которых ничего не осталось, — тоже (имена,
    карточки и прочее за ними подчистит forget_old). Возвращает, сколько убрано."""
    for u in gone:
        it = state["items"].pop(u)
        vectors.pop(u, None)
        story = state["stories"].get(it["story"]) if it["story"] else None
        if story and u in story["urls"]:
            story["urls"].remove(u)
    for sid in [sid for sid, story in state["stories"].items() if not story["urls"]]:
        del state["stories"][sid]
    return len(gone)


def drop_gone_sources(state: dict, vectors: dict, known: set[str]) -> int:
    """Новости изданий, которых больше нет в sources.json, — вон из сюжетов. Иначе убранное издание (18.11 — Google
    News EN и RU, мелкие сайты со всего мира) ещё дни сидело бы в растущих сюжетах и в счёте изданий. Архив помнит их
    ARCHIVE_DAYS — новые новости таких изданий в сюжеты не берём (run)."""
    return drop_items(state, vectors, [u for u, it in state["items"].items() if it["source"] not in known])


def drop_late(state: dict, vectors: dict) -> int:
    """Поздние новости (LATE), разложенные до 22.11, — вон из тем; новые такие в темы не берём (run)."""
    return drop_items(state, vectors, [u for u, it in state["items"].items() if late(it.get("published"), it["seen"])])


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
    state["checked_names"] = {k: v for k, v in state.get("checked_names", {}).items()
                              if all(s in state["stories"] for s in k.split("|"))}
    state.pop("merged", None)  # кто в кого склеился — только для разбора прогонов; журнал склеек — merges
    # до 13.11 у имени срезалась и закрывающая кавычка названия внутри: «Разрешение на «Югославию»
    state["names"] = {sid: name + "»" * (name.count("«") - name.count("»"))
                      for sid, name in state.get("names", {}).items() if sid in state["stories"]}
    state["named_at"] = {sid: n for sid, n in state.get("named_at", {}).items() if sid in state["stories"]}
    state["named_t"] = {sid: t for sid, t in state.get("named_t", {}).items() if sid in state["stories"]}
    state["episodes"] = {sid: ep for sid, ep in state.get("episodes", {}).items() if sid in state["stories"]}
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
    state["routine"] = {sid: c for sid, c in state.get("routine", {}).items() if sid in state["cards"]}
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
        return stories_audit.apply_fix(state, c["day"], c["which"], now, vectors)
    if c["do"] == "split":
        known = {it["part"] for it in state["items"].values() if it.get("story")}
        missing = [p for p in c["parts"] if p not in known]
        if missing:
            raise ValueError(f"частей {', '.join(missing)} нет")
        olds = sorted({it["story"] for it in state["items"].values() if it.get("story") and it["part"] in c["parts"]})
        sid = stories.split(state, c["parts"], now)
        return f"{', '.join(c['parts'])}: {', '.join(olds)} → свой сюжет {sid}"
    if c["do"] == "merge":  # тема story — в тему to целиком; /unmerge story отменит
        for sid in (c["story"], c["to"]):
            if sid not in stories_:
                raise ValueError(f"сюжета {sid} нет")
        if c["story"] == c["to"]:
            raise ValueError("это один сюжет")
        size = len(stories_[c["story"]]["urls"])
        stories.join(state, c["to"], c["story"], now, "склейка командой /merge", "command")
        state["merge_again"] = sorted(set(state.get("merge_again", [])) | {c["to"]})
        return f"{c['story']} ({size} новостей) влит в {c['to']}: теперь {len(stories_[c['to']]['urls'])}"
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
            stories.LINES_PROMPT: "деление", NAME_PROMPT: "имена", stories.CARD_PROMPT: "карточки",
            stories_audit.AUDIT_PROMPT: "аудит", stories_audit.PARTS_PROMPT: "аудит", stories_audit.PLACE_PROMPT: "аудит",
            stories_audit.ADVICE_PROMPT: "аудит", SR_HEAD_PROMPT: "заголовки sr", EPISODE_PROMPT: "эпизоды"}.get(system, "другое")


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


def name_stories(state: dict, vectors: dict, ask, now: str) -> int:
    """Названия видимым темам (Haiku, пачками): тем, у кого названия ещё нет, и тем, в которых с прошлого названия
    прошло NAME_EVERY и прибавилось NAME_NEW новостей. Новое название — новая карточка (её напишет stories.cards в
    следующей сборке). Возвращает, скольких спросили."""
    names = state.setdefault("names", {})
    named_at = state.setdefault("named_at", {})  # тема → сколько в ней было новостей, когда её назвали
    named_t = state.setdefault("named_t", {})  # тема → когда её назвали
    due = (datetime.fromisoformat(now) - NAME_EVERY).isoformat()
    todo = [sid for sid, story in state["stories"].items() if stories.visible(state, sid)
            and (sid not in names or named_t.get(sid, "") <= due and len(story["urls"]) >= named_at.get(sid, 0) + NAME_NEW)]
    for start in range(0, len(todo), NAME_BATCH):
        chunk = todo[start:start + NAME_BATCH]
        content = json.dumps({"stories": [{"id": f"S{n}"} | ({"name": names[sid]} if sid in names else {})
                                          | (big_theme(state, vectors, sid) if is_big(state, sid) else {})
                                          | {"news": recent_news(state, vectors, sid)} for n, sid in enumerate(chunk)]},
                             ensure_ascii=False)
        answer, _ = ask(NAME_PROMPT, content, NAME_SCHEMA)
        for named in answer["names"]:
            n = named["id"][1:]
            if n.isdigit() and int(n) < len(chunk) and named["name"].strip():
                name, sid = stories.clean_name(named["name"]), chunk[int(n)]
                name = name if len(name) <= NAME_MAX else name[:NAME_MAX].rsplit(" ", 1)[0]  # по словам
                if sid in names and names[sid] != name and not is_big(state, sid):
                    # ситуация сменилась — и карточка заново. У большой — нет: карточку пишут по последним новостям,
                    # а она про всю тему, и название большой темы берётся из неё
                    state.get("cards", {}).pop(sid, None)
                names[sid] = name
                named_at[sid] = len(state["stories"][sid]["urls"])
                named_t[sid] = now
    return len(todo)


def is_big(state: dict, sid: str) -> bool:
    return stories.is_big(state, sid)


def big_theme(state: dict, vectors: dict, sid: str) -> dict:
    """Что модель видит о большой теме целиком: event — карточка, whole — NAME_SHOWN самых типичных новостей за всё
    время (stories.whole)."""
    return ({"event": state["cards"][sid]} if sid in state.get("cards", {}) else {}) | {
        "whole": stories.whole(state, vectors, sid, NAME_SHOWN)}


def recent_news(state: dict, vectors: dict, sid: str) -> list[str]:
    """Что модель видит о теме, когда называет её: NAME_SHOWN самых типичных из свежих (NAME_RECENT до последней)
    новостей, по времени."""
    urls = state["stories"][sid]["urls"]
    last = datetime.fromisoformat(state["items"][urls[-1]]["seen"])
    recent = [u for u in urls if u in vectors and last - datetime.fromisoformat(state["items"][u]["seen"]) <= NAME_RECENT] \
        or urls[-NAME_SHOWN:]
    if len(recent) > NAME_SHOWN:
        v = np.stack([vectors[u] for u in recent]).astype(np.float32)
        keep = set(np.argsort(-(v @ v.T).mean(axis=1))[:NAME_SHOWN])
        recent = [u for k, u in enumerate(recent) if k in keep]
    return list(dict.fromkeys(stories.text_of(state["items"][u]) for u in recent))


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
    if gone := drop_late(state, vectors):
        print(f"Сюжеты: убрано из тем {gone} поздних новостей (опубликованы раньше, чем пришли, больше чем на "
              f"{LATE.days} дня)", file=sys.stderr)
    since = max(START, (datetime.fromisoformat(now) - stories.LIVE).isoformat())
    wait = (datetime.fromisoformat(now) - WAIT_TRANSLATION).isoformat()
    items = [(u, a) for u, a in archive.items()
             if u not in state["items"] and a["source"] in known and stories.eligible(a) and a["seen"] >= since
             and not late(a.get("date"), a["seen"]) and (a.get("ru") or a.get("what") or a["seen"] < wait)]
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
    name_stories(state, vectors, ask, now)
    try:  # эпизоды больших тем — для страниц тем; не вышло — темы покажутся без них, по дням
        name_episodes(state, vectors, ask, page_stories(state, now)[0])
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        print(f"Сюжеты: эпизоды не названы — {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
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
    elif stories.LINES_ON and "lines" not in state.get("batches", {}):
        sids = stories.lines_todo(state, top)
        calls = [(stories.LINES_PROMPT, stories.lines_request(state, sid), stories.LINES_SCHEMA, stories.LINES_EFFORT,
                  stories.MERGE_MODEL, None) for sid in sids]
        sent = {"sids": sids, "sizes": [len(state["stories"][sid]["urls"]) for sid in sids]}
        if got := send(client, state, "lines", calls, sent, now, ask):
            c, f = apply_lines(state, now, sent, got[0])
            checked, found = checked + c, found + f
    # проверка топа — три раза в день (stories_audit.AUDIT_HOURS), пакетом; отчёт — в сборке, в которую придут ответы.
    # Ошибка проверки не роняет сборку: не пришли ответы — отправим заново в следующей
    audit, audit_day = None, ""
    if "audit" in arrived or stories_audit.due(state, now, LOCAL_TZ):
        try:
            if "audit" in arrived:
                sent, answers, spent_usd = arrived["audit"]
                audit_day = sent["day"]
                audit = stories_audit.finish(state, sent, answers, spent_usd, ask, vectors, now)
            else:
                audit_day = stories_audit.due(state, now, LOCAL_TZ)
                calls, sent = stories_audit.prepare(state, audit_day, now, vectors)
                if got := send(client, state, "audit", calls, sent, now, ask):
                    audit = stories_audit.finish(state, sent, *got, ask, vectors, now)
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
    # сводка за сутки в @vestomat_daily — вечером, раз в день (daily.py); до сохранения: отметка «отправлено» — в состоянии
    daily.post(state, vectors, now, LOCAL_TZ, spend)
    # срочное в @vestomat_live — каждую сборку, если тема резко пошла и модель сочла её срочной (live.py)
    live.post(state, vectors, now, LOCAL_TZ, spend)
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
    """Заголовок темы — её название (name_stories). Пока названия нет — «что произошло» (what, ровный пересказ без
    кликбейта из перевода) новости headline_url, без what — её заголовок. urls — не вся тема (часть на /plot/) — тоже."""
    sid = state["items"][urls[0]]["story"]
    if len(urls) == len(state["stories"][sid]["urls"]) and (name := state.get("names", {}).get(sid)):
        return name
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
        anchor = f' id="n{it["n"]}"' if it.get("n") is not None else ""  # ссылки из отчёта проверки тем ведут сюда
        return (f'<li{anchor}><span class="meta">№{it.get("n", "")} ·{local_time(it.get("published", it["seen"]))} · {e(it["source"])} · '
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
        if state.get("routine", {}).get(sid):
            mark = (f"{mark} · " if mark else "") + f"регулярная сводка — горячесть ×{stories.ROUTINE_HEAT}"
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
                "merge": lambda: f"{c['story']} {c['to']}",
                "fix": lambda: f"{c['day']} {c['which']} (кнопка отчёта)"}.get(c["do"], lambda: "")()
        return f"<li>{e(local_time(c['applied']))} · /{e(c['do'])} {e(args)} — {e(c['result'])}</li>"

    commands = "".join(command(c) for c in reversed(state.get("commands", [])))

    spent = "".join(f"<tr><td>{d}</td><td>{s['calls']}</td><td>${s['usd']:.3f}</td><td>{e(by_steps(s))}</td></tr>"
                    for d, s in sorted(state.get("spent", {}).items(), reverse=True))
    total = len(state["stories"])
    style = """<style>body{font:15px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#222;background:#fff}
summary{cursor:pointer;padding:6px 0}details{border-bottom:1px solid #eee}ul{margin:4px 0 12px;padding-left:20px}
li{margin:6px 0}li:target{background:#fff3b0}.meta{color:#888;font-size:13px}a{color:#1a5fb4}table{border-collapse:collapse;font-size:13px}
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
{stories.RECHECK_SIM}, такое добавление (и любое в тему этой страницы) перепроверяет Haiku (пакетом: снятая новость уходит через сборку); копии (сходство от {stories.COPY_SIM}) идут за оригиналом без модели; <span class="sim low">красные</span> — ниже {stories.CANDIDATE_SIM}.
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


# основные стороны в порядке спектра: от критичных к SNS через нейтральные. Место в строке — не ранг: порядок один для
# всех сюжетов. Ни «кто больше» (всегда побеждала сторона с большим числом изданий), ни ротация по дням
SR_SPECTRUM = ["independent", "mainstream", "state", "progov"]
SR_FIRST = 5  # сколько свежих новостей сюжета видно на главной; все — на странице сюжета
SR_CAMP_FIRST = 8  # на странице сюжета — сколько новостей каждой стороны видно сразу
# большая тема на странице — по эпизодам: 22.11 s9278 (учредительное заседание Скупщины, 2000 новостей за неделю)
# сплошной лентой по дням читать было невозможно. Эпизоды — группы похожих новостей (k-means по векторам), названия
# им даёт Haiku по самым типичным новостям группы (EPISODE_PROMPT); на странице — самые горячие сверху, как темы. Склеенные части
# тут не помогают: 1400 новостей s9278 раскладка сразу положила в саму тему. Подписывать эпизод самой типичной новостью
# — обманчиво: 163 новости о нападении на Вукичевич и споре в Скупщине об извинениях звались «Лидию облили йогуртом».
# Эпизоды хранятся в state["episodes"]: заново — когда тема выросла в EPISODES_REDO раза, а пока новые новости идут в
# ближайший эпизод. Иначе от сборки к сборке менялись бы и группы, и названия (и запросы к модели)
EPISODES_FROM = 300  # со скольких новостей тема показывается по эпизодам (22.11: со 100 делились и темы, которые лентой читаются и так)
EPISODE_SIZE = 150  # примерно столько новостей на эпизод
EPISODES_MIN, EPISODES_MAX = 4, 12
EPISODE_SMALL = 0.015  # эпизод меньше этой доли темы (и меньше 5 новостей) уходит в ближайший
EPISODES_REDO = 1.25
EPISODE_SHOWN = 8  # сколько самых типичных новостей эпизода видит модель
EPISODE_PROMPT = """Ты делишь большую тему агрегатора новостей о Сербии на эпизоды для читателя. Новости уже разбиты на группы; ты даёшь каждой группе название.
Вход — JSON: theme — название всей темы; episodes — группы, id вида E…: count — сколько в ней новостей, from и to — даты первой и последней, news — самые типичные её новости.
Для каждой группы верни ru — название по-русски и sr — то же по-сербски латиницей. Название — от двух до шести слов, как подзаголовок раздела: именительный падеж, без глагола в личной форме, без оценок, без кавычек вокруг и без точки в конце («Выбор председателя Скупщины», «Нападение на Лидию Вукичевич», «Выборы вице-спикеров»). Оно о том, что объединяет новости группы, а не об одной из них. Не повторяй название темы и не давай двум группам одинаковых названий: если группы похожи, назови, чем они различаются. Имена людей и организаций по-сербски — в сербском написании (Vukićević, Čupić, SNS)."""
EPISODE_SCHEMA = {
    "type": "object",
    "properties": {
        "episodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "ru": {"type": "string"}, "sr": {"type": "string"}},
                "required": ["id", "ru", "sr"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["episodes"],
    "additionalProperties": False,
}
SR_COLORS = {"independent": "#1d4ed8", "investigations": "#8ec7c3", "progov": "#dc2626", "state": "#c084fc",
             "mainstream": "#71827a", "economy": "#9e8f55", "foreign-sr": "#e6b690", "foreign": "#ef7f16",
             "reddit": "#8a423a", "trends": "#3087b3", "regional": "#38d64e"}  # как --c-* в template.html
SR_STYLE = """:root{--bg:#fff;--text:#333;--strong:#000;--muted:#8a8a8a;--faint:#a8a8a8;--soft:#ececec;--read:#afafaf;--hover:#9b9b9b;--tip-bg:#282c35;--tip-text:#ddd;COLORS}
@media (prefers-color-scheme:dark){:root{--bg:#282c35;--text:#ddd;--strong:#fff;--muted:#9a9ca3;--faint:#7c7f87;--soft:#383d49;--read:#737373;--tip-bg:#3a404d;--tip-text:#eee}}
*{box-sizing:border-box}
[hidden]{display:none!important}
/* как на главной: в приложении на iPhone размыта строка состояния и полоса ~28pt под ней — опускаем страницу ниже */
:root{--safe-top:env(safe-area-inset-top)}
@supports (-webkit-touch-callout:none){@media (display-mode:standalone){:root{--safe-top:calc(env(safe-area-inset-top) + 28px)}}}
body{padding:var(--safe-top) env(safe-area-inset-right) 0 env(safe-area-inset-left);margin:0;background:var(--bg);color:var(--text);font:16px/1.3 "Nunito",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
a{color:inherit}
.wrap{max-width:820px;margin:0 auto;padding:18px 20px 40px}
h1{margin:14px 0 4px;font-size:32px;font-weight:900;color:var(--strong);line-height:1.15}
.lead{margin:0 0 8px;color:var(--muted);font-size:14px}
.story{padding:26px 0 12px;border-bottom:1.5px solid var(--soft)}
.story h2{margin:0;font-size:22px;line-height:1.25;font-weight:700;color:var(--strong);display:flex;gap:12px}
.story h2 a{text-decoration:none}.story h2 a:hover{color:var(--hover)}
.rank{flex:none;min-width:22px;color:var(--faint);font-weight:900}
.meta{margin:6px 0 10px;color:var(--muted);font-size:13px}
.cov{margin:4px 0 14px;color:var(--muted);font-size:12px;line-height:1.9}
.cov>span,.pair>span{display:inline-flex;align-items:center;gap:5px;margin-right:14px;white-space:nowrap}
.cov .what{font-weight:700;display:block}
.cov .none{opacity:.55}
.cov .rest{display:block;white-space:normal}
.cov .sides{display:block;white-space:normal;margin:0}
.cov .pair{display:inline-block;white-space:nowrap;margin:0}
.dot{width:8px;height:8px;border-radius:50%;flex:none;display:inline-block}
.items{list-style:none;margin:0;padding:0}
.row{display:flex;gap:10px;margin-bottom:9px}
.time{flex:none;width:44px;white-space:nowrap;color:var(--faint);font-size:12px;padding-top:2px;font-variant-numeric:tabular-nums}
.head{flex:1;min-width:0;text-decoration:none;overflow-wrap:anywhere;-webkit-user-drag:none}
.head:hover{color:var(--hover)}
.src{font-size:13px;font-weight:700;color:var(--muted);display:inline-flex;align-items:center;gap:5px;margin-right:6px;white-space:nowrap}
.all{display:inline-block;margin:2px 0 8px;color:var(--muted);font-size:14px;font-weight:700;text-decoration:none}
.all:hover{color:var(--strong)}
.indent{margin-left:34px}
.seg{display:inline-flex;background:var(--soft);border-radius:20px;padding:3px;margin:18px 0 6px}
.seg button{border:0;background:none;padding:5px 14px;border-radius:20px;cursor:pointer;font:inherit;font-size:14px;color:var(--muted)}
.seg button[aria-pressed=true]{background:var(--strong);color:var(--bg);font-weight:700}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0 4px}
.chip{border:1.5px solid var(--soft);background:var(--bg);color:var(--text);border-radius:20px;padding:3px 12px 3px 9px;font:inherit;font-size:14px;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.chip:hover{border-color:var(--muted)}
.chip.none{cursor:default;opacity:.55;font-weight:400}.chip.none:hover{border-color:var(--soft)}
.chip[aria-pressed=true]{background:var(--tip-bg);color:var(--tip-text);border-color:var(--tip-bg)}
.group{margin:22px 0 4px;font-size:17px;font-weight:700;color:var(--strong);display:flex;align-items:center;gap:8px}
.group.none{opacity:.55;font-weight:400}
.group .n{color:var(--faint);font-weight:400;font-size:14px}
.more summary{cursor:pointer;color:var(--muted);font-size:14px;font-weight:700;padding:2px 0 8px;list-style:none}
.more summary::-webkit-details-marker{display:none}.more summary::after{content:" ↓"}.more[open] summary::after{content:" ↑"}
.more summary:hover{color:var(--strong)}
/* «По сюжетам»: каждый сюжет раскрыт — название, полоска по дням, 3 свежие новости и «Ещё», остальные по дням */
.ep{padding:18px 0 8px;border-bottom:1.5px solid var(--soft)}
.ep-t{margin:0;font-size:19px;line-height:1.3;color:var(--strong)}
.ep-line{display:flex;gap:10px;align-items:center;margin:4px 0 10px;color:var(--faint);font-size:13px}
.spark{display:inline-flex;gap:2px;align-items:flex-end;height:16px}
.spark i{display:block;width:6px;background:var(--muted);border-radius:1px;opacity:.55}
.spark i.z{opacity:.2}
.ep .group:first-of-type{margin-top:6px}
.ep-more{border:0;background:none;padding:2px 0 8px;font:inherit;font-size:14px;font-weight:700;color:var(--muted);cursor:pointer}
.ep-more:hover{color:var(--strong)}
.nav{display:flex;justify-content:space-between;gap:16px;margin-top:28px;font-size:14px;font-weight:700;color:var(--muted)}
.nav a{text-decoration:none}.nav a:hover{color:var(--strong)}
/* компьютер: на главной сюжет в две колонки — слева заголовок и стороны, справа новости; на странице сюжета
   «Po stranama» — стороны рядом колонками, «Po vremenu» — узкой колонкой для чтения */
@media (min-width:1100px){.wrap{max-width:1240px;padding:28px 40px 48px}h1{font-size:38px;max-width:1000px}
.lead{font-size:15px}.story{display:grid;grid-template-columns:5fr 7fr;gap:48px;padding:30px 0 18px}
.story h2{font-size:24px}.rhs{margin-left:0;padding-top:4px}
.cols:not([hidden]){display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:8px 44px;align-items:start}
.narrow{max-width:880px}}
/* шапка — как на главной (template.html: .topline, header, .bar, .seg, #lang, #script, #view, .search) */
.topline{max-width:1300px;margin:0 auto;padding:8px 20px 0;color:var(--muted);font-size:13px;text-align:right}
.topline:empty{display:none}#info>span{white-space:nowrap}#info .dot{width:7px;height:7px;margin-left:5px;vertical-align:1px}
.site{position:sticky;top:var(--safe-top);z-index:5;background:color-mix(in srgb,var(--bg) 94%,transparent);backdrop-filter:blur(10px)}
.site .bar{max-width:1300px;margin:0 auto;padding:14px 20px;display:flex;flex-wrap:wrap;gap:10px 20px;align-items:center}
.site .brand{display:flex;flex-direction:column;align-items:flex-start;flex:1 1 0;min-width:280px}
.site .name{font-size:29px;font-weight:700;line-height:1.2;white-space:nowrap;text-decoration:none;color:var(--text)}
.site .logo{width:28px;height:28px;vertical-align:-4px;margin-right:10px}
.site .updated{color:var(--muted);font-size:13px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.site .updated b{font-weight:800}
.site .controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.site .controls-row{display:contents}
.site .seg{display:inline-flex;background:var(--soft);border-radius:20px;padding:3px;margin:0}
.site .seg button,.site .seg a{border:0;background:none;padding:5px 12px;border-radius:20px;cursor:pointer;font:inherit;font-size:14px;font-weight:700;color:var(--muted);text-decoration:none;text-align:center}
.site .seg button:hover,.site .seg a:hover{color:var(--text)}
#script [data-v=orig]{width:92px}#script [data-v=lat]{width:50px}#script [data-v=cyr]{width:54px}#lang a{width:80px}
#view a{width:84px;padding-left:4px;padding-right:4px}
.site .seg button[aria-pressed=true],.site .seg a[aria-current]{background:var(--tip-bg);color:var(--tip-text)}
#lang .short{display:none}
.search{border:1.5px solid var(--soft);background:var(--bg);color:var(--text);border-radius:20px;padding:6px 34px 6px 14px;font:inherit;font-size:14px;width:180px}
.search:focus{outline:none;border-color:var(--text)}.search::-webkit-search-cancel-button{-webkit-appearance:none}
.search-box{position:relative;display:inline-flex}
.search-clear{position:absolute;right:7px;top:50%;transform:translateY(-50%);width:20px;height:20px;padding:0;border:0;border-radius:50%;cursor:pointer;background:var(--soft);color:var(--muted);font:inherit;font-size:15px;line-height:20px}
.search-clear:hover{background:var(--tip-bg);color:var(--tip-text)}
.back{display:inline-block;color:var(--muted);font-size:14px;font-weight:700;text-decoration:none}.back:hover{color:var(--strong)}
@media (max-width:800px){.site .name{font-size:24px}}
@media (max-width:640px){.site{position:static}
.site .bar{display:grid;grid-template-columns:auto 1fr;gap:10px 8px;padding:8px 16px 12px}
.site .controls,.site .controls-row{display:contents}
.site .brand{grid-row:1;grid-column:1/-1;flex-direction:column;align-items:flex-start;gap:0;margin:0;min-width:0}
.site .name{font-size:21px;line-height:1.4}.site .logo{width:22px;height:22px;vertical-align:-3px;margin-right:8px}.site .updated{font-size:12px;white-space:normal}
#lang{grid-row:2;grid-column:1}#lang .full{display:none}#lang .short{display:inline}
#script{grid-row:2;grid-column:2}#script button{flex:1}
#view{grid-row:3;grid-column:1/-1}#view a{flex:1}
.search-box{grid-row:4;grid-column:1/-1;min-width:0}
.site .seg button,.site .seg a,#view a,#script button[data-v],#lang a{width:auto;padding:8px 14px;font-size:15px}
.search{width:100%;height:100%;font-size:16px}.search-clear{width:26px;height:26px;line-height:26px;font-size:18px}
.topline{padding:8px 16px 0;text-align:left;font-size:12px}#info .unit{display:none}
.to-top{position:fixed;right:16px;bottom:calc(16px + env(safe-area-inset-bottom));z-index:6;width:44px;height:44px;border:0;border-radius:50%;cursor:pointer;background:var(--tip-bg);color:var(--tip-text);font:inherit;font-size:20px;font-weight:700;box-shadow:0 4px 16px rgb(0 0 0 / .25)}}
@media (max-width:640px){.chip{padding:7px 14px 7px 11px;font-size:15px}.chips{gap:8px}}
@media (max-width:600px){.wrap{padding:14px 16px 32px}h1{font-size:26px}.story h2{font-size:19px;gap:8px}.indent{margin-left:0}.head{font-size:15px}}
/* плашка (стили — из template.html, см. plate_parts): тёмный фон на 20px за края колонки, текст — под заголовком
   (время 44 + зазор 10); подводка в плашке — не серая мелкая .lead страницы */
.wrap .item{--edge:20px}.details{margin-left:54px}.details .lead{color:inherit;font-size:inherit}
@media (max-width:600px){.wrap .item{--edge:16px}}
""".replace("COLORS", "".join(f"--c-{c}:{v};" for c, v in SR_COLORS.items()))
TEMPLATE_FILE = ROOT / "template.html"
ARTICLES_FILE = ROOT / "cache" / "articles.json"  # essences.py: у каких новостей worker знает текст статьи ("tx")


def plate_parts() -> tuple[str, str]:
    """Плашка новости — тот же код и те же стили, что на главной: куски template.html между метками /*plate*/ и
    /*plate-css*/. Так страница темы не расходится с главной, когда плашку меняют."""
    text = TEMPLATE_FILE.read_text("utf-8")
    js = re.findall(r"/\*plate\*/(.*?)/\*/plate\*/", text, re.S)
    css = re.findall(r"/\*plate-css\*/(.*?)/\*/plate-css\*/", text, re.S)
    if len(js) != 3 or len(css) != 1:
        raise ValueError(f"template.html: меток плашки {len(js)} js и {len(css)} css, ждали 3 и 1")
    return css[0], "".join(js)


# Плашка на страницах тем: строки уже в HTML, скрипт навешивает на них ту же плашку, что на главной (wirePlate).
# Данные новостей (оригинал, подводка, перевод, tx) — в PLATE, только для новостей этой страницы. Вокруг кода из шаблона —
# то, что на главной берётся из остального скрипта: состояние, статистика, высота липкой шапки
PLATE_SCRIPT = """<script>
(() => {
const PLATE = /*PLATE*/;
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
// алфавит плашки — как в оригинале: латиницу или кириллицу ставит applyScript шапки, как и всему тексту страницы
const state = { lang: PLATE.lang, script: "orig", leads: store.get("leads", "ai"), dev: store.get("dev", false),
  read: new Set(store.get("read", [])), open: null };
function markRead(url) {
  state.read.add(url);
  store.set("read", [...state.read].slice(-3000));
}
const HOVER = matchMedia("(hover: hover)").matches, FRESH_MS = 3 * 3600e3;
let now = Date.now();
function track(path, title) {
  window.goatcounter?.count?.({ path, title, event: true });
}
const newsPath = (kind, item) => `${kind}/${item.url.replace(/^https?:\\/\\/(www\\.)?/, "").replace(/[?#].*$/, "")}`;
const trackNews = (kind, item) => track(newsPath(kind, item), item.ru?.title || item.title);
const toTop = document.getElementById("to-top"), header = document.querySelector("header.site");
let stickyHeight = 0;
const measureSticky = () => {
  const s = getComputedStyle(header);
  stickyHeight = s.position === "sticky" ? parseFloat(s.top) + header.getBoundingClientRect().height : 0;
};
addEventListener("resize", measureSticky);
measureSticky();
/*CODE*/
for (const li of document.querySelectorAll(".items li.item")) {
  const head = li.querySelector("a.head"), url = head.getAttribute("href"), d = PLATE.items[url];
  if (!d) continue;
  const item = { url, title: d.title, summary: d.summary, lang: d.lang, ru: d.ru, tx: d.tx };
  const src = { name: d.src, section: li.dataset.s, lang: d.lang };
  if (state.read.has(url)) li.classList.add("read");
  const flag = reportFlag(item, src);
  if (flag) head.after(flag);
  wirePlate(li, head, item, src, null);
}
// по-сербски латиницей или кириллицей: новый текст плашки переписываем тем же алфавитом, что и страницу
if (PLATE.lang === "sr") {
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; window.applyScript?.(); });
  }).observe(document.body, { childList: true, subtree: true });  // и окно жалобы — оно в body
}
})();
</script>
<script data-goatcounter="https://vestomat.goatcounter.com/count" async src="https://gc.zgo.at/count.v5.js"
        integrity="sha384-atnOLvQb9t+jTSipvd75X2yginT4PjVbqDdlJAmxMm+wYElFmeR6EmLP5bYeoRVQ" crossorigin="anonymous"></script>"""
SR_HEAD = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">{robots}
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)"><meta name="theme-color" content="#282c35" media="(prefers-color-scheme: dark)">
<title>{title}</title>
<link rel="icon" href="/favicon.ico" sizes="32x32"><link rel="icon" href="/icon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Nunito:wght@400;700;900&display=swap">
<style>{style}</style></head><body>{site}<div class="wrap">"""
# время новости — как на главной: «5 min», «3 h», старше суток — дата. Переключатель на странице сюжета — по времени
# (по дням) или по сторонам (разделам)
SR_SCRIPT = """<script>
const now = Date.now();
document.querySelectorAll('time.time').forEach(t => {
  const at = Date.parse(t.getAttribute('datetime')), m = Math.round((now - at) / 60000);
  if (m < 0) return;
  if (m < 60) t.textContent = m + ' MIN';
  else if (m < 24 * 60) t.textContent = Math.floor(m / 60) + ' HOURS';
  else { const d = new Date(at); t.textContent = d.getDate() + '.' + (d.getMonth() + 1) + '.'; }
});
// выделили часть заголовка мышью — это не клик по ссылке
document.addEventListener('click', ev => {
  const a = ev.target.closest('a.head'), sel = getSelection();
  if (a && !sel.isCollapsed && a.contains(sel.anchorNode)) ev.preventDefault();
});
document.querySelectorAll('.wrap .seg button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.wrap .seg button').forEach(x => x.setAttribute('aria-pressed', x === b));
  document.querySelectorAll('[data-view]').forEach(v => v.hidden = !v.dataset.view.split(' ').includes(b.dataset.show));
  window.resetChips?.();  // другой вид — фильтр разделов снимается
}));
// «По сюжетам»: в свёрнутом сюжете — EP_FIRST свежих новостей из тех, что проходят фильтр разделов (secOn) и поиск
// (qHit), без заголовков дней; «Ещё» раскрывает остальные по дням. Число новостей — сколько проходят. Сюжет, в котором
// не осталось ни одной, прячется; при поиске сюжеты с находками раскрыты
const EP_FIRST = 3;
window.layoutEps = layoutEps;
function layoutEps() {
  const box = document.querySelector('[data-view=eps]');
  if (!box) return;
  const [one, few, many] = box.dataset.forms.split('|');
  const news = n => n + ' ' + (n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? few : many);
  box.querySelectorAll('section.ep').forEach(ep => {
    const rows = [...ep.querySelectorAll('li[data-s]')];
    const ok = rows.filter(r => (!window.secOn || window.secOn.has(r.dataset.s)) && (!window.qHit || window.qHit(r)));
    const open = ep.dataset.open === '1' || !!window.qHit;
    rows.forEach(r => r.hidden = true);
    ok.forEach((r, i) => r.hidden = !open && i >= EP_FIRST);
    ep.querySelectorAll('.items').forEach(ul => {
      const any = !!ul.querySelector('li[data-s]:not([hidden])'), h = ul.previousElementSibling;
      ul.hidden = !any;
      if (h && h.matches('h3.group')) h.hidden = !any || !open;
    });
    ep.hidden = !ok.length;
    ep.querySelector('.k').textContent = news(ok.length);
    const more = ep.querySelector('.ep-more');
    more.hidden = ok.length <= EP_FIRST || !!window.qHit;
    more.textContent = open ? box.dataset.less + ' ↑' : box.dataset.more + ' ' + news(ok.length - EP_FIRST) + ' ↓';
  });
}
document.addEventListener('click', ev => {
  const b = ev.target.closest('.ep-more');
  if (!b) return;
  const ep = b.closest('section.ep'), closing = ep.dataset.open === '1';
  ep.dataset.open = closing ? '0' : '1';
  layoutEps();
  if (closing && ep.getBoundingClientRect().top < 0) ep.scrollIntoView();  // свернули длинный — назад к его началу
});
layoutEps();
// фильтр на странице темы — только в виде «По времени» (в «По сторонам» стороны и так колонками), как в общей ленте: «Все» или выбранные разделы. Первый клик по разделу оставляет
// только его, следующие добавляют или убирают; убрали последний — снова «Все»
const chips = document.querySelectorAll('.chips button.chip');
if (chips.length) {
  const ids = [...chips].map(c => c.dataset.s).filter(Boolean);
  let on = new Set(ids);
  const apply = next => {
    on = next.size ? next : new Set(ids);
    const all = on.size === ids.length;
    chips.forEach(c => c.setAttribute('aria-pressed', c.dataset.s ? !all && on.has(c.dataset.s) : all));
    document.querySelectorAll('.items li[data-s]').forEach(r => r.hidden = !on.has(r.dataset.s));
    // день без новостей выбранных разделов — без заголовка
    document.querySelectorAll('[data-view=time] .items').forEach(ul => {
      const h = ul.previousElementSibling, empty = !ul.querySelector('li[data-s]:not([hidden])');
      ul.hidden = empty;
      if (h && h.matches('h3.group')) h.hidden = empty;
    });
    window.secOn = all ? null : on;
    layoutEps();
  };
  window.resetChips = () => { if (on.size !== ids.length) apply(new Set(ids)); };
  chips.forEach(c => c.addEventListener('click', () => {
    const all = on.size === ids.length, s = c.dataset.s;
    if (!s) return apply(new Set(ids));
    if (all) return apply(new Set([s]));
    const next = new Set(on);
    next.has(s) ? next.delete(s) : next.add(s);
    apply(next);
  }));
}
</script>"""

# шапка как на главной: курс, погода и воздух — из /version.json (их пишет build.py), алфавит и поиск — на месте.
# Переводы букв, иконки погоды и уровни воздуха — как в template.html
HEADER_SCRIPT = r"""<script>
(() => {
const CFG = /*CFG*/;
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
};
store.set('lang', CFG.lang);  // главная по ссылкам из шапки откроется на языке этой страницы
const C2L = { А:"A",Б:"B",В:"V",Г:"G",Д:"D",Ђ:"Đ",Е:"E",Ж:"Ž",З:"Z",И:"I",Ј:"J",К:"K",Л:"L",Љ:"Lj",М:"M",Н:"N",Њ:"Nj",О:"O",П:"P",Р:"R",С:"S",Т:"T",Ћ:"Ć",У:"U",Ф:"F",Х:"H",Ц:"C",Ч:"Č",Џ:"Dž",Ш:"Š" };
for (const [c, l] of Object.entries(C2L)) C2L[c.toLowerCase()] = l.toLowerCase();
const L2C = {};
for (const [c, l] of Object.entries(C2L)) L2C[l] = c;
Object.assign(L2C, { LJ: "Љ", NJ: "Њ", DŽ: "Џ", Q: "Ку", W: "В", X: "Кс", Y: "И", q: "ку", w: "в", x: "кс", y: "и" });
const toLatin = s => s.replace(/[Ѐ-ӿ]/g, (ch, i) => {
  const l = C2L[ch];
  if (!l) return ch;
  if (l.length === 2 && ch === ch.toUpperCase()) {
    const next = s[i + 1] || "";
    if (next && next === next.toUpperCase() && next !== next.toLowerCase()) return l.toUpperCase();
  }
  return l;
});
const KEEP_LATIN = new Set(["Ctrl", "RSS", "EN", "RU", "SR", "BIRN", "RFE", "RL", "Lat", "Vestomat"]);
const toCyrillic = s => s.replace(/[A-Za-zČĆŽŠĐčćžšđ]+/g, w => KEEP_LATIN.has(w) ? w
  : w.replace(/Lj|LJ|lj|Nj|NJ|nj|Dž|DŽ|dž|[A-Za-zČĆŽŠĐčćžšđ]/g, ch => L2C[ch] ?? ch));
const norm = s => toLatin(s).toLowerCase().replace(/đ/g, "dj").normalize("NFD").replace(/[̀-ͯ]/g, "");

// алфавит: Original — как написали издания, Lat / Ћир — всё латиницей или кириллицей (выбор общий с главной)
let mode = CFG.lang === 'sr' ? store.get('script', 'orig') : 'orig';
const originals = new WeakMap();
const q = document.getElementById('q'), placeholder = q.placeholder;
function applyScript() {
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, { acceptNode: n =>
    n.parentElement.closest('script, style, #lang') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  for (let n; (n = walk.nextNode());) {
    if (!originals.has(n)) originals.set(n, n.nodeValue);
    const text = originals.get(n);
    // кнопки алфавита — как на главной: кириллицей «Оригинал», в остальных режимах как есть («Ћир» не латинизируем)
    if (n.parentElement.closest('#script')) { n.nodeValue = mode === 'cyr' ? toCyrillic(text) : text; continue; }
    const cyr = (text.match(/[Ѐ-ӿ]/g) || []).length;
    n.nodeValue = mode === 'lat' ? toLatin(text) : mode === 'cyr' && cyr <= text.length / 3 ? toCyrillic(text) : text;
  }
  q.placeholder = mode === 'cyr' ? toCyrillic(placeholder) : placeholder;
  document.querySelectorAll('#script button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === mode)));
  if (CFG.lang === 'sr') document.documentElement.lang = mode === 'cyr' ? 'sr-Cyrl' : 'sr-Latn';
}
window.applyScript = applyScript;  // плашки (PLATE_SCRIPT) появляются позже — их текст переписывается тем же алфавитом
document.getElementById('script')?.addEventListener('click', e => {
  const v = e.target.closest('button')?.dataset.v;
  if (v) { mode = v; store.set('script', v); applyScript(); }
});

// курс, погода, воздух и «обновлено» — свежие, из той же сборки, что и главная
const locale = CFG.lang === 'ru' ? 'ru' : 'sr-Latn-RS';
const AQI_LEVELS = [[20, "#50f0e6"], [40, "#50ccaa"], [60, "#f0e641"], [80, "#ff5050"], [100, "#960032"], [Infinity, "#7d2181"]];
function weatherIcon(code, day) {
  if (code <= 1) return day ? "☀️" : "🌙";
  if (code === 2) return day ? "⛅" : "☁️";
  if (code === 3) return "☁️";
  if (code === 45 || code === 48) return "🌫️";
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return "🌨️";
  if (code >= 95) return "⛈️";
  return "🌧️";
}
function plural(n, [one, few, many]) {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
}
const el = (tag, attrs, ...kids) => { const x = document.createElement(tag); Object.assign(x, attrs); x.append(...kids); return x; };
fetch('/version.json?t=' + Date.now(), { cache: 'no-store' }).then(r => r.json()).then(v => {
  const info = v.info || {}, parts = [];
  const num = x => new Intl.NumberFormat(locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(x);
  if (info.rates) {
    const day = new Intl.DateTimeFormat(locale, { timeZone: 'Europe/Belgrade', day: 'numeric', month: 'short' })
      .format(new Date(info.rates.date)).replace(/\.$/, '');
    const title = CFG.rateHint + ' ' + day;
    parts.push(el('span', { title }, '€ ' + num(info.rates.EUR), el('span', { className: 'unit' }, ' ' + CFG.din)));
    parts.push(el('span', { title }, '₽ ' + num(info.rates.RUB), el('span', { className: 'unit' }, ' ' + CFG.din)));
  }
  if (info.weather) {
    const t = Math.round(info.weather.temp);
    parts.push(el('span', {}, `${CFG.city} ${t > 0 ? '+' : ''}${t}° ${weatherIcon(info.weather.code, info.weather.day)}`));
  }
  if (info.air) {
    const level = AQI_LEVELS.findIndex(([max]) => info.air.aqi < max);
    const dot = el('span', { className: 'dot' });
    dot.style.background = AQI_LEVELS[level][1];
    parts.push(el('span', { title: `${CFG.airHint}: ${Math.round(info.air.aqi)}, PM2.5 ${Math.round(info.air.pm25)} µg/m³` },
      `${CFG.air}: ${CFG.aqi[level]}`, dot));
  }
  document.getElementById('info').replaceChildren(...parts.flatMap((p, i) => i ? [' · ', p] : [p]));
  if (v.generated) {
    const time = new Intl.DateTimeFormat(locale, { timeZone: 'Europe/Belgrade', hour: '2-digit', minute: '2-digit' }).format(new Date(v.generated));
    document.getElementById('updated').replaceChildren(el('b', {}, 'Vestomat'),
      ` · ${CFG.updated} ${time}` + (v.outlets ? ` · ${v.outlets} ${plural(v.outlets, CFG.outlets)}` : ''));
  }
  applyScript();
}).catch(() => {});

// поиск — по новостям этой страницы: лишние строки, дни, стороны и темы прячутся
const qInput = document.getElementById('q'), qClear = document.getElementById('q-clear');
const hit = (node, q) => node && norm(node.textContent).includes(q);
function search() {
  const q = norm(qInput.value.trim());
  qClear.hidden = !qInput.value;
  document.querySelectorAll('.wrap .items li').forEach(li => li.hidden = !!q && !hit(li, q));
  document.querySelectorAll('.wrap article.story').forEach(a => {
    const title = q && hit(a.querySelector('h2'), q);
    if (title) a.querySelectorAll('li').forEach(li => li.hidden = false);
    a.hidden = !!q && !title && !a.querySelector('li:not([hidden])');
  });
  document.querySelectorAll('.wrap details.more').forEach(d => d.open = !!q);
  document.querySelectorAll('.wrap section.col').forEach(s => s.hidden = !!q && !s.querySelector('li:not([hidden])'));
  document.querySelectorAll('.wrap h3.group').forEach(h => {
    const ul = h.nextElementSibling;
    if (ul && ul.tagName === 'UL') h.hidden = !!q && !ul.querySelector('li:not([hidden])');
  });
  window.qHit = q ? (li => hit(li, q)) : null;  // «По сюжетам» раскладывает свои новости сам (layoutEps)
  if (window.layoutEps) layoutEps();
}
qInput.addEventListener('input', search);
qInput.addEventListener('keydown', e => { if (e.key === 'Escape' && qInput.value) { qInput.value = ''; search(); } });
qClear.addEventListener('click', () => { qInput.value = ''; search(); qInput.focus(); });
if (qInput.value) search();
// ↑ — как на главной: на телефоне шапка не липнет, и кнопка появляется, когда шапка уехала за верх экрана
const toTop = document.getElementById('to-top'), header = document.querySelector('header.site');
toTop.addEventListener('click', () => scrollTo({ top: 0, behavior: 'smooth' }));
const onScroll = () => { toTop.hidden = getComputedStyle(header).position === 'sticky' || header.getBoundingClientRect().bottom > 0; };
addEventListener('scroll', onScroll, { passive: true });
addEventListener('resize', onScroll);
onScroll();
applyScript();
})();
</script>"""


# тексты страниц тем на двух языках: /sr/plot/ — сербский (заголовки новостей оригинальные), /ru/plot/ — русский
# (заголовок сюжета — тот же, что на /plot/, новости — в переводе, как на /ru/)
READER = {
    "sr": {
        "dir": SR_PAGE_DIR, "html_lang": "sr-Latn", "home": "/", "news": SR_NEWS, "outlets": SR_OUTLETS,
        "title": "Teme dana", "lead": "O čemu piše najviše medija", "site": "Srbija: vesti",
        "views": ("Mediji", "Sve vesti", "Teme"), "lang_names": ("Srpski", "Русский"), "search": "Pretraga", "to_top": "Na vrh",
        "info": {"din": "din", "city": "Beograd", "air": "vazduh", "rateHint": "Srednji kurs NBS za",
                 "airHint": "Evropski indeks kvaliteta vazduha", "updated": "ažurirano", "outlets": SR_OUTLETS,
                 "aqi": ["dobar", "prihvatljiv", "umeren", "loš", "veoma loš", "izuzetno loš"]}, "updated": "ažurirano", "at": "u",
        "since": "od", "all": "Sve", "page": "Stranica teme", "wrote": "Pisali su:", "silent": "nisu pisali",
        "others": "Ostali mediji:", "nth": "tema dana", "last": "poslednja", "by_time": "Po vremenu",
        "by_side": "Po stranama", "by_ep": "Po događajima", "more": "Još", "less": "Skupi", "prev": "Prethodna tema", "next": "Sledeća tema",
        "none": "Još nema tema.", "min": "min", "h": "h",
        "days": ["ponedeljak", "utorak", "sreda", "četvrtak", "petak", "subota", "nedelja"],
        "months": ["januara", "februara", "marta", "aprila", "maja", "juna", "jula", "avgusta", "septembra",
                   "oktobra", "novembra", "decembra"],
    },
    "ru": {
        "dir": ROOT / "site" / "ru" / "plot", "html_lang": "ru", "home": "/ru/", "news": NEWS, "outlets": OUTLETS,
        "title": "Темы дня", "lead": "О чём пишет больше всего изданий", "site": "Сербия: новости",
        "views": ("Издания", "Лента", "Темы"), "lang_names": ("Srpski", "Русский"), "search": "Поиск", "to_top": "Наверх",
        "info": {"din": "дин", "city": "Белград", "air": "воздух", "rateHint": "Средний курс Народного банка Сербии на",
                 "airHint": "Европейский индекс качества воздуха", "updated": "обновлено", "outlets": OUTLETS,
                 "aqi": ["хороший", "приемлемый", "умеренный", "плохой", "очень плохой", "крайне плохой"]}, "updated": "обновлено", "at": "в",
        "since": "с", "all": "Все", "page": "Страница темы", "wrote": "Писали:", "silent": "не писали",
        "others": "Остальные издания:", "nth": "тема дня", "last": "последняя", "by_time": "По времени",
        "by_side": "По сторонам", "by_ep": "По сюжетам", "more": "Ещё", "less": "Свернуть", "prev": "Предыдущая тема", "next": "Следующая тема",
        "none": "Тем пока нет.", "min": "мин", "h": "ч",
        "days": ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"],
        "months": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
                   "ноября", "декабря"],
    },
}
THEMES_FILE = ROOT / "site" / "themes.json"


def _when(state: dict, u: str) -> str:
    return state["items"][u].get("published", state["items"][u]["seen"])


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-9)


def clusters(vectors: dict, urls: list[str]) -> list[list[str]]:
    """Группы похожих новостей (k-means по векторам, примерно EPISODE_SIZE в группе, от EPISODES_MIN до EPISODES_MAX);
    маленькие уходят в ближайшие. Новости без вектора — в самую большую."""
    good = [u for u in urls if u in vectors]
    if len(good) < EPISODES_MIN:
        return []
    v = _unit(np.stack([vectors[u] for u in good]).astype(np.float32))
    k = min(EPISODES_MAX, max(EPISODES_MIN, round(len(good) / EPISODE_SIZE)))
    rng = np.random.default_rng(0)  # одни и те же группы для одной и той же темы
    centers = [v[int(rng.integers(len(good)))]]
    for _ in range(k - 1):  # k-means++: следующий центр — подальше от уже выбранных
        far = np.clip(1 - (v @ np.stack(centers).T).max(axis=1), 0, None) ** 2
        centers.append(v[int(rng.choice(len(good), p=far / far.sum()))] if far.sum() else v[int(rng.integers(len(good)))])
    c = np.stack(centers)
    alive = np.ones(k, bool)
    for _ in range(30):
        label = np.where(alive, v @ c.T, -9).argmax(axis=1)
        sizes = np.bincount(label, minlength=k)
        small = alive & (sizes < max(5, EPISODE_SMALL * len(good)))
        if small.any() and alive.sum() > EPISODES_MIN:  # самую маленькую — в соседние, и считаем заново
            alive[int(np.where(small, sizes, 10 ** 9).argmin())] = False
            continue
        new = _unit(np.stack([v[label == j].mean(axis=0) if alive[j] and sizes[j] else c[j] for j in range(k)]))
        if np.allclose(new, c):
            break
        c = new
    label = np.where(alive, v @ c.T, -9).argmax(axis=1)
    groups = [[good[i] for i in np.where(label == j)[0]] for j in range(k) if (label == j).any()]
    max(groups, key=len).extend(u for u in urls if u not in vectors)
    return groups


def typical_of(vectors: dict, urls: list[str], n: int) -> list[str]:
    """n новостей, больше всех похожих на остальные (из не больше 400 — равномерно)."""
    pool = [u for u in urls[::max(len(urls) // 400, 1)] if u in vectors]
    if len(pool) <= n:
        return pool
    v = _unit(np.stack([vectors[u] for u in pool]).astype(np.float32))
    return [pool[i] for i in np.argsort(-(v @ v.mean(axis=0)))[:n]]


def sample_of(state: dict, vectors: dict, urls: list[str]) -> list[str]:
    """Что модель видит об эпизоде: половина — самые типичные новости, половина — взятые равномерно по времени, чтобы
    название было про всю группу, а не про её середину. Копии — одной строкой."""
    by_time = sorted(urls, key=lambda u: _when(state, u))
    spread = by_time[::max(len(by_time) // (EPISODE_SHOWN // 2), 1)]
    texts = [stories.text_of(state["items"][u]) for u in typical_of(vectors, urls, EPISODE_SHOWN * 2) + spread]
    core, rest = list(dict.fromkeys(texts[:EPISODE_SHOWN * 2]))[:EPISODE_SHOWN // 2], texts[EPISODE_SHOWN * 2:]
    return list(dict.fromkeys(core + rest))[:EPISODE_SHOWN]


def name_episodes(state: dict, vectors: dict, ask, sids: list[str]) -> int:
    """Эпизоды больших тем страницы (EPISODES_FROM): тем, у которых их ещё нет или которые выросли в EPISODES_REDO
    раза, — группы заново и названия от модели, по запросу на тему. Возвращает, скольких спросили."""
    store = state.setdefault("episodes", {})
    asked = 0
    for sid in sids:
        urls = state["stories"][sid]["urls"]
        if len(urls) < EPISODES_FROM or sid in store and len(urls) < store[sid]["size"] * EPISODES_REDO:
            continue
        groups = sorted(clusters(vectors, urls), key=lambda g: sorted(_when(state, u) for u in g)[len(g) // 2])
        if not groups:
            continue
        content = json.dumps({"theme": state.get("names", {}).get(sid, ""), "episodes": [
            {"id": f"E{k}", "count": len(g), "from": min(_when(state, u) for u in g)[:10],
             "to": max(_when(state, u) for u in g)[:10],
             "news": sample_of(state, vectors, g)}
            for k, g in enumerate(groups)]}, ensure_ascii=False)
        answer, _ = ask(EPISODE_PROMPT, content, EPISODE_SCHEMA)
        asked += 1
        named = {x["id"]: x for x in answer["episodes"]}
        eps = []
        for k, g in enumerate(groups):
            x = named.get(f"E{k}", {})
            head = state["items"][typical_of(vectors, g, 1)[0]] if typical_of(vectors, g, 1) else state["items"][g[0]]
            eps.append({"ns": sorted(state["items"][u]["n"] for u in g),
                        "ru": stories.clean_name(x.get("ru", "")) or head["title"],
                        "sr": stories.clean_name(x.get("sr", "")) or head.get("orig") or head["title"]})
        store[sid] = {"size": len(urls), "eps": eps}
    return asked


def episodes(state: dict, vectors: dict, sid: str) -> list[tuple[dict, list[str]]]:
    """Эпизоды темы для страницы: [(эпизод с ru и sr, его новости)], самые горячие сверху — как темы на главной
    (stories.heat): каждое издание даёт очко за свою последнюю новость в эпизоде, очко вдвое тает за 12 часов. Так
    наверху то, о чём много изданий пишут сейчас, а одна запоздалая новость старый эпизод не поднимает. По середине
    времени эпизода порядок выглядел случайным: почти у всех эпизодов s9278 — одни и те же 16.11–22.11. Новость, пришедшая после
    того, как эпизоды составили, — в эпизод с самыми похожими новостями. Эпизодов нет — []."""
    stored = state.get("episodes", {}).get(sid)
    urls = state["stories"][sid]["urls"]
    if not stored or len(urls) < EPISODES_FROM:
        return []
    of = {n: k for k, ep in enumerate(stored["eps"]) for n in ep["ns"]}
    members = [[] for _ in stored["eps"]]
    late = []
    for u in urls:
        k = of.get(state["items"][u]["n"])
        (members[k] if k is not None else late).append(u)
    centers = [_unit(np.stack([vectors[u] for u in m if u in vectors]).astype(np.float32).mean(axis=0))
               if any(u in vectors for u in m) else None for m in members]
    for u in late:
        sims = [float(vectors[u] @ c) if c is not None and u in vectors else -9 for c in centers]
        members[int(np.argmax(sims)) if max(sims) > -9 else max(range(len(members)), key=lambda k: len(members[k]))].append(u)
    out = [(ep, sorted(m, key=lambda u: _when(state, u))) for ep, m in zip(stored["eps"], members) if m]
    now = datetime.fromisoformat(state.get("built") or datetime.now(timezone.utc).isoformat())

    def hotness(members: list[str]) -> float:
        latest = {}
        for u in members:
            latest[state["items"][u]["source"]] = max(latest.get(state["items"][u]["source"], ""), _when(state, u))
        return sum(0.5 ** ((now - datetime.fromisoformat(t)) / stories.HEAT_HALF_LIFE) for t in latest.values())
    return sorted(out, key=lambda em: (hotness(em[1]), _when(state, em[1][-1])), reverse=True)


def write_sr_page(state: dict, vectors: dict, archive: dict) -> None:
    """Темы для читателя: /sr/plot/ и /ru/plot/ (те же сюжеты, порядок и новости, что на /plot/, без служебного) и
    site/themes.json — для вида «Teme / Темы» на главной. Ошибка здесь не роняет сюжеты."""
    try:
        _write_reader(state, vectors, archive)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print("Сюжеты: страницы тем не обновились", file=sys.stderr)


def _write_reader(state: dict, vectors: dict, archive: dict) -> None:
    """Оформление — как у главной (template.html): Nunito, те же цвета и строки новостей «время · издание заголовок».
    Главная /sr/plot/: сюжет — номер, заголовок, какие стороны писали, SR_FIRST свежих новостей и ссылка на страницу
    сюжета sX/: там все новости — по времени (по дням) или по сторонам (разделам). Состав и порядок — как на /plot/."""
    e = html.escape
    config = json.loads(SOURCES_FILE.read_text("utf-8"))
    now = state.get("built") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    # порядок сторон — SR_SPECTRUM, один на все сюжеты и все дни; за ними остальные разделы, как в sources.json
    core = [s["id"] for s in config["sections"] if s["id"] in SR_SPECTRUM]
    core.sort(key=SR_SPECTRUM.index)
    camp_order = core + [s["id"] for s in config["sections"] if s["id"] not in core]
    shown, _, _ = page_stories(state, now)
    sr_heads = state.get("sr_heads", {})

    def local(iso: str, fmt: str) -> str:
        return datetime.fromisoformat(iso).astimezone(LOCAL_TZ).strftime(fmt)

    def orig(u: str) -> str:  # старше архива и без orig — остаётся только перевод
        it = state["items"][u]
        return it.get("orig") or archive.get(u, {}).get("title") or it["title"]

    def when(u: str) -> str:
        return state["items"][u].get("published", state["items"][u]["seen"])

    def dot(section: str) -> str:
        return f'<span class="dot" style="background:var(--c-{e(section)})"></span>'

    stories_data = []
    plate_css, plate_js = plate_parts()
    try:
        texts = json.loads(ARTICLES_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        texts = {}
    for lang, L in READER.items():
        camp_names = {s["id"]: s["title"] if lang == "sr" else s.get("title_ru", s["title"]) for s in config["sections"]}

        def news_title(u: str) -> str:
            return orig(u) if lang == "sr" else state["items"][u]["title"]

        # разметка строки — как у главной (li.item > .row): нажатие раскрывает под ней ту же плашку (PLATE_SCRIPT)
        def row(u: str) -> str:
            it = state["items"][u]
            return (f'<li class="item" data-s="{e(it["section"])}"><div class="row"><time class="time" datetime="{e(when(u))}">'
                    f'{local(when(u), "%H:%M")}</time><a class="head" href="{e(u)}" target="_blank" rel="noopener" draggable="false">'
                    f'<span class="src">{dot(it["section"])}{e(it["source"])}</span>{e(news_title(u))}</a></div></li>')

        def plate(page: str) -> str:
            """Скрипт плашки с данными новостей этой страницы: оригинал заголовка и подводки — из архива, перевод (на
            русской), tx — можно ли просить описание от ИИ. Новости старше архива — без подводки: в плашке только кнопки"""
            items = {}
            for u in dict.fromkeys(html.unescape(m) for m in re.findall(r'<a class="head" href="([^"]+)"', page)):
                it, a = state["items"].get(u, {}), archive.get(u, {})
                ru = a.get("ru") or None
                d = {"title": a.get("title") or orig(u), "lang": a.get("lang") or "sr", "src": it.get("source") or a.get("source", "")}
                if a.get("summary"):
                    d["summary"] = a["summary"]
                if tx := texts.get(u, {}).get("tx"):
                    d["tx"] = tx
                if lang == "ru" and ru:
                    d["ru"] = ru
                elif tx and ru:  # по-сербски перевод не показывается, но описание просят только у переведённых
                    d["ru"] = {"title": ru.get("title", "")}
                items[u] = d
            data = json.dumps({"lang": lang, "items": items}, ensure_ascii=False).replace("</", "<\\/")
            return PLATE_SCRIPT.replace("/*CODE*/", plate_js).replace("/*PLATE*/", data)

        def day_name(iso: str) -> str:  # без «danas / juče»: страница статична, а читают её и назавтра
            d = datetime.fromisoformat(iso).astimezone(LOCAL_TZ).date()
            return f"{L['days'][d.weekday()].capitalize()}, {d.day} {L['months'][d.month - 1]}" if lang == "ru" else \
                f"{L['days'][d.weekday()].capitalize()}, {d.day}. {L['months'][d.month - 1]}"

        def head(title: str, here: str) -> str:
            """Шапка — как на главной: иконка и название сайта, язык, вид («Темы» выбран). here — путь этой страницы
            внутри /{lang}/plot/, переключатель языка ведёт на ту же страницу другого языка"""
            home = L["home"]
            views = "".join(f'<a href="{home}#{hash_}"{" aria-current=\"page\"" if hash_ == "teme" else ""}>{e(name)}</a>'
                            for hash_, name in zip(("mediji", "lenta", "teme"), L["views"]))
            langs = "".join(f'<a href="/{code}/plot/{here}"{" aria-current=\"page\"" if code == lang else ""}>'
                            f'<span class="full">{e(name)}</span><span class="short">{code.upper()}</span></a>'
                            for code, name in zip(("sr", "ru"), L["lang_names"]))
            # алфавит — только по-сербски, как на главной; по-русски он невидим, но держит место — кнопки там же
            hide = ' style="visibility:hidden" aria-hidden="true"' if lang == "ru" else ""
            scripts = (f'<div class="seg" id="script" title="Srpski tekst: kao u originalu, latinicom ili ćirilicom"{hide}>'
                       '<button data-v="orig">Original</button><button data-v="lat">Lat</button>'
                       '<button data-v="cyr">Ћир</button></div>')
            site = (f'<div class="topline" id="info"></div><header class="site"><div class="bar"><div class="brand">'
                    f'<a class="name" href="{home}"><img class="logo" src="/icon.svg" width="28" height="28" alt="Vestomat">'
                    f'{e(L["site"])}</a><span class="updated" id="updated"><b>Vestomat</b> · {L["updated"]} '
                    f'{local(now, "%H:%M")}</span></div><div class="controls">{scripts}<div class="seg" id="lang">{langs}</div>'
                    f'<div class="controls-row"><div class="seg" id="view">{views}</div><span class="search-box">'
                    f'<input class="search" id="q" type="search" autocomplete="off" placeholder="{L["search"]}">'
                    f'<button class="search-clear" id="q-clear" type="button" hidden>×</button></span></div></div></div></header>'
                    f'<button class="to-top" id="to-top" type="button" hidden title="{L["to_top"]}" aria-label="{L["to_top"]}">↑</button>')
            # в поиске — только хабы /sr/plot/ и /ru/plot/: адрес вечный, а страница темы пропадает (404), когда тема уходит
            robots = "" if not here else '<meta name="robots" content="noindex,nofollow">'
            return SR_HEAD.format(title=e(title), style=plate_css + SR_STYLE, lang=L["html_lang"], site=site, robots=robots)

        script = (SR_SCRIPT.replace("MIN", L["min"]).replace("HOURS", L["h"])
                  + HEADER_SCRIPT.replace("/*CFG*/", json.dumps({"lang": lang, **L["info"]}, ensure_ascii=False)))
        cards, pages = [], {}
        for k, sid in enumerate(shown, 1):
            story = state["stories"][sid]
            urls = story["urls"]
            items = [state["items"][u] for u in urls]
            ru_head = headline_of(state, vectors, urls)
            title = ru_head if lang == "ru" else sr_heads.get(ru_head) or orig(headline_url(state, vectors, urls))
            # стороны — только писали или нет, без чисел. Полоса долей всегда «побеждала» сторону, где больше изданий,
            # а дроби (10/11) занижали: еженедельники (Radar) вне новостного цикла, колонки слияние иногда не берёт.
            # Не писавшая основная сторона — приглушённо «nisu pisali». Остальные разделы — списком названий
            per_camp = Counter(c for c, _ in {(it["section"], it["source"]) for it in items})
            camps = sorted(per_camp.items(), key=lambda cn: camp_order.index(cn[0]) if cn[0] in camp_order else 99)

            def side(c: str) -> str:
                name = e(camp_names.get(c, c))
                return (f'<span>{dot(c)}{name}</span>' if per_camp.get(c) else
                        f'<span class="none">{dot(c)}{name}: {L["silent"]}</span>')

            others = [e(camp_names.get(c, c)) for c, _ in camps if c not in core]
            # стороны парами, пара не разрывается: строка переносится 2 + 2, а не одна сторона на новую строку
            sides = [side(c) for c in core]
            pairs = "".join(f'<span class="pair">{"".join(sides[i:i + 2])}</span>' for i in range(0, len(sides), 2))
            balance = (f'<p class="cov"><span class="what">{L["wrote"]}</span><span class="sides">{pairs}</span>'
                       + (f'<span class="rest">{L["others"]} {" · ".join(others)}</span>' if others else "") + '</p>')
            news = sorted(urls, key=lambda u: (when(u), state["items"][u]["n"]), reverse=True)
            eps = episodes(state, vectors, sid)
            outlets = len({it["source"] for it in items})
            meta = (f'{count(len(urls), *L["news"])} · {count(outlets, *L["outlets"])} · '
                    f'{L["since"]} {local(story["created"], "%d.%m.")}')
            more = (f'<a class="all" href="{sid}/" target="_blank">{L["all"]} {count(len(urls), *L["news"])} →</a>'
                    if len(news) > SR_FIRST else f'<a class="all" href="{sid}/" target="_blank">{L["page"]} →</a>')
            preview = news[:SR_FIRST]  # самые свежие
            cards.append(
                f'<article class="story" id="{sid}"><div class="lhs"><h2><span class="rank">{k}</span><a href="{sid}/" '
                f'target="_blank">{e(title)}</a></h2><div class="indent"><p class="meta">{meta}</p>{balance}</div></div>'
                f'<div class="rhs indent"><ul class="items">{"".join(row(u) for u in preview)}</ul>{more}</div></article>')
            if lang == "sr":  # для главной: обоих языков сразу, новости — свежие SR_FIRST
                stories_data.append({
                    "id": sid, "news": len(urls), "outlets": outlets, "created": story["created"],
                    "title": {"sr": title, "ru": ru_head}, "wrote": [c for c in core if per_camp.get(c)],
                    "others": [c for c, _ in camps if c not in core],
                    "items": [{"url": u, "source": state["items"][u]["source"], "section": state["items"][u]["section"],
                               "date": when(u), "sr": orig(u), "ru": state["items"][u]["title"]} for u in preview]})

            # страница сюжета: по времени (по дням) — первой вкладкой; по сторонам — в порядке camp_order, свежие сверху
            def camp_block(c: str, n: int) -> str:  # SR_CAMP_FIRST свежих, остальные стороны — раскрываются на месте
                mine = [u for u in news if state["items"][u]["section"] == c]
                rest = mine[SR_CAMP_FIRST:]
                more = (f'<details class="more"><summary>{L["more"]} {count(len(rest), *L["news"])}</summary>'
                        f'<ul class="items">{"".join(row(u) for u in rest)}</ul></details>' if rest else "")
                return (f'<section class="col"><h3 class="group">{dot(c)}{e(camp_names.get(c, c))} <span class="n">'
                        f'{count(n, *L["outlets"])} · {count(len(mine), *L["news"])}</span></h3>'
                        f'<ul class="items">{"".join(row(u) for u in mine[:SR_CAMP_FIRST])}</ul>{more}</section>')

            # не писавшая основная сторона — на своём месте приглушённым заголовком «не писали», как кнопка в фильтре
            def silent_block(c: str) -> str:
                return (f'<section class="col"><h3 class="group none">{dot(c)}{e(camp_names.get(c, c))}: '
                        f'{L["silent"]}</h3></section>')

            blocks = [(c, camp_block(c, n)) for c, n in camps] + [(c, silent_block(c)) for c in core if not per_camp.get(c)]
            blocks.sort(key=lambda cb: camp_order.index(cb[0]) if cb[0] in camp_order else 99)
            by_camp = "".join(b for _, b in blocks)
            # фильтр по разделам заменяет на странице «Писали»: разделы, что писали, в порядке сторон, а не писавшая
            # основная сторона — приглушённой некликабельной кнопкой «не писали». Один раздел — фильтровать нечего,
            # тогда остаётся «Писали»
            def chip(c: str) -> str:
                name = e(camp_names.get(c, c))
                if per_camp.get(c):
                    return f'<button class="chip" data-s="{e(c)}" aria-pressed="false">{dot(c)}{name}</button>'
                return f'<span class="chip none">{dot(c)}{name}: {L["silent"]}</span>'
            order = [c for c in camp_order if per_camp.get(c) or c in core] + [c for c, _ in camps if c not in camp_order]
            chips = ("" if len(camps) < 2 else
                     f'<nav class="chips" data-view="{"eps time" if eps else "time"}"><button class="chip" aria-pressed="true">{L["all"]}</button>'
                     + "".join(chip(c) for c in order) + '</nav>')
            def by_days(urls_: list[str]) -> str:  # новости по дням, свежие сверху
                out, current = [], None
                for u in urls_:
                    d = day_name(when(u))
                    if d != current:
                        out.append(("</ul>" if current else "") + f'<h3 class="group">{e(d)}</h3><ul class="items">')
                        current = d
                    out.append(row(u))
                return "".join(out) + "</ul>"

            by_day = [by_days(news)]
            # большая тема — по эпизодам: список свёрнутых эпизодов, внутри — по дням. Этот вид — первым

            theme_days = sorted({local(when(u), "%Y-%m-%d") for u in urls})

            def spark(members: list[str]) -> str:  # полоска по дням темы: высота — сколько новостей сюжета в этот день
                per_day = Counter(local(when(u), "%Y-%m-%d") for u in members)
                top = max(per_day.values())
                title = ", ".join(f"{d[8:]}.{d[5:7]}.: {per_day[d]}" for d in theme_days)
                return (f'<span class="spark" title="{title}">' + "".join(
                    f'<i{"" if per_day[d] else " class=z"} style="height:{max(2, round(16 * per_day[d] / top))}px"></i>'
                    for d in theme_days) + '</span>')

            def episode(ep: dict, members: list[str]) -> str:
                first, last = local(when(members[0]), "%d.%m."), local(when(members[-1]), "%d.%m.")
                span = first if first == last else f"{first}–{last}"
                return (f'<section class="ep" data-open="0"><h3 class="ep-t">{e(ep[lang])}</h3>'
                        f'<div class="ep-line">{spark(members)}<span>{span} · <span class="k">'
                        f'{count(len(members), *L["news"])}</span></span></div>'
                        f'{by_days(sorted(members, key=lambda u: (when(u), state["items"][u]["n"]), reverse=True))}'
                        f'<button class="ep-more" type="button"></button></section>')
            by_ep = "".join(episode(ep, m) for ep, m in eps)
            prev = f'<a href="../{shown[k - 2]}/">← {L["prev"]}</a>' if k > 1 else "<span></span>"
            after = f'<a href="../{shown[k]}/">{L["next"]} →</a>' if k < len(shown) else "<span></span>"
            pages[sid] = f"""{head(f"{title} — Vestomat", f"{sid}/")}
<a class="back" href="{L["home"]}#teme">← {L["title"]}</a>
<h1>{e(title)}</h1>
<p class="meta">{k}. {L["nth"]} · {meta} · {L["last"]} {local(story["updated"], "%d.%m.")} {L["at"]} {local(story["updated"], "%H:%M")}</p>
{"" if chips else balance}
<div class="seg" role="group">{f'<button data-show="eps" aria-pressed="true">{L["by_ep"]}</button>' if eps else ""}<button data-show="time" aria-pressed="{"false" if eps else "true"}">{L["by_time"]}</button><button data-show="camps" aria-pressed="false">{L["by_side"]}</button></div>
{chips}
{f'<div data-view="eps" class="narrow" data-forms="{"|".join(L["news"])}" data-more="{L["more"]}" data-less="{L["less"]}">{by_ep}</div>' if eps else ""}
<div data-view="time" class="narrow"{" hidden" if eps else ""}>{"".join(by_day)}</div>
<div data-view="camps" class="cols" hidden>{by_camp}</div>
<nav class="nav">{prev}{after}</nav>
</div>{script}</body></html>"""

        page = f"""{head(f"{L['title']} — Vestomat", "")}
<h1>{L["title"]}</h1>
<p class="lead">{L["lead"]} · {L["updated"]} {local(now, "%d.%m.")} {L["at"]} {local(now, "%H:%M")}</p>
{"".join(cards) or f"<p>{L['none']}</p>"}
</div>{script}</body></html>"""
        L["dir"].mkdir(parents=True, exist_ok=True)
        with_plate = lambda text: text.replace("</body>", plate(text) + "</body>")  # noqa: E731
        (L["dir"] / "index.html").write_text(with_plate(page), "utf-8")
        for sid, text in pages.items():
            (L["dir"] / sid).mkdir(exist_ok=True)
            (L["dir"] / sid / "index.html").write_text(with_plate(text), "utf-8")
    THEMES_FILE.write_text(json.dumps({"built": now, "stories": stories_data}, ensure_ascii=False), "utf-8")


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
