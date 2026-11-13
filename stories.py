"""Сюжеты: подборки новостей разных изданий об одном событии. Векторы находят похожие сюжеты, модель решает.

На каждой сборке новые события (kind=event или без kind — русскоязычные ленты) раскладываются так:
1. вектор по «что произошло» (what), без него — по заголовку; fastembed, локально;
2. кандидаты — до CANDIDATES живых сюжетов (последняя новость не старше LIVE), сходство — с лучшей новостью сюжета,
   а не с его центром, чтобы сюжет не уплывал. Одиночка — тоже сюжет, просто из одной новости;
3. новости без кандидатов и без похожих соседей по сборке сразу становятся новыми сюжетами, остальные решает
   модель, пачками по BATCH: join (в сюжет S…), same (то же событие, что новая N…), new, unclear (тоже свой
   сюжет: склейка ещё может приложить новость к видимому);
4. раз в MERGE_EVERY — склейка (merge): похожие пары живых сюжетов модель проверяет на «одну историю» — событие
   или дело с развитием и реакциями; склеенные сюжеты остаются частями.
Видимый сюжет — от двух изданий. Названий пока нет. Раскладку проверяем прогоном на снимке архива против эталона
(stories_replay.py, stories_gold.py), в живой сборке она идёт в теневом режиме (stories_live.py).

Три уровня: сюжет → часть → новость. Часть — сюжет, каким он был до склейки: у новости story — сюжет,
part — id сюжета, в котором она появилась; склейка сливает сюжеты, а части сохраняет, чтобы тему потом можно было
показать по событиям и разделить обратно. Новость, добавленная в сюжет (join), идёт в часть самой похожей новости.

Состояние — один словарь, JSON: stories (id → новости и время), items (адрес → решение и то, что нужно для запроса).
Векторы хранятся отдельно: {адрес: вектор}.
"""
import json
from datetime import datetime, timedelta

import numpy as np

import translate

# поднять при изменении логики раскладки или склейки (правила, промпты и пороги отслеживаются и так):
# живая страница (stories_live.py) разложит все свои новости заново
# 2 — при раскладке модель видит и новость сюжета, самую похожую на новую; 3 — участники сравниваются латиницей;
# 4 — «непонятная» новость — свой сюжет, а не без сюжета; 5 — отдельные события выделяются в свои сюжеты
LAYOUT_VERSION = 5
MODEL = "claude-haiku-5-5"
EFFORT = "low"
# e5-large против MiniLM на эталоне 09–10.11: нужная новость первой среди похожих в 96–97% случаев против 91–93%;
# на трудных парах (кандидатах для модели) при потере 5% одинаковых отсеивает 75–79% разных против 29–33%.
# Не хуже reranker bge-reranker-v2-m3 и в разы быстрее. e5 ждёт приставку «query: » перед текстом
EMBED_MODEL = "intfloat/multilingual-e5-large"
EMBED_PREFIX = "query: "
LIVE = timedelta(hours=48)
CANDIDATES = 8
# пороги — под e5 (у неё сходства сжаты к 0.8–0.95). На эталоне 09–10.11 у 0.855 полнота пар 0.80 при вдвое
# меньшем числе разных пар выше порога, чем у MiniLM с прежним порогом 0.5 (полнота там была 0.72)
CANDIDATE_SIM = 0.855  # ниже — даже не спрашиваем модель
STORY_ITEMS = 30  # с каким числом последних новостей сюжета сравниваем
SHOWN_ITEMS = 3  # сколько новостей сюжета видит модель: первая и последние
BATCH = 25
SKIP_SECTIONS = {"reddit", "trends"}

# что считать одним сюжетом — общее для раскладки и склейки (и то же, что в промпте эталона stories_gold.py)
RULES = """Сюжет — одна история: событие или одно дело, о котором пишут разные издания, вместе с его развитием, последствиями и реакциями. Сюжет — не тема: общее ведомство, компания, партия или политик ещё не делают историю одной.
- Один сюжет: разные издания об одном событии; его развитие и последствия (подробности, обыски, новые задержания по тому же делу, суд, отставки, меры, выплаты пострадавшим — крах банка и выплаты его вкладчикам — один сюжет); реакции и заявления именно по этому событию; одно событие, рассказанное с разных сторон или с разной подробностью (сводка одной стороны и репортаж другой, в целом по стране и в одном городе); шаги разных людей в одном органе или одной общине по одному поводу (отставки и назначения после смены власти); одно заседание, решение или документ с несколькими пунктами; одно выступление, пресс-конференция или интервью с заявлениями на разные темы; один саммит или конференция; одни выборы — итоги, результаты списков, реакции; подготовка, анонс и само событие.
- Разные сюжеты: разные действия одного ведомства, компании или партии (прокуратура задержала одного и ответила на жалобу по другому делу; партия в разные дни представила разные части программы); выступления одного политика в разные дни по разным поводам; встречи одного участника с разными собеседниками (послами, делегациями, партиями), если это не одна общая встреча и не один визит; однотипные события без общего (две аварии, два ареста по разным делам, работы на двух разных дорогах, отключения в разных городах); разные выборы (парламентские и выборы в национальные советы меньшинств); общая тема без общего события («переговоры по Украине», «Сербия и ЕС», «война с Ираном»).
- Обзор нескольких событий (подборка, сравнение) относится к сюжету любого из них.
- Похожие слова ещё не значат одну историю."""

SYSTEM_PROMPT = f"""Ты раскладываешь свежие новости о Сербии по сюжетам. {RULES}
Если сомневаешься — new: лишний сюжет потом склеят, а чужая новость в сюжете видна читателю.

Вход — JSON:
- stories: живые сюжеты-кандидаты, id вида S…; news — несколько новостей сюжета (что произошло), outlets — сколько изданий о нём пишут, updated — время последней новости (UTC); from — если сюжет выделен как отдельное событие из другого кандидата: S… того сюжета;
- news: новые новости, id вида N…: издание, время, заголовок, что произошло (бывает пустым), candidates — сюжеты, похожие по словам.
Заголовок бывает кликбейтом — опирайся на смысл.

Для каждой новой новости верни verdict и target:
- join — о той же истории, что сюжет target (S… из её candidates); обзор, подходящий к нескольким candidates, — к тому, у кого больше outlets, при равенстве — с более поздним updated; новость об отдельном событии — к его сюжету (с from), а не к сюжету, из которого его выделили;
- same — подходящего сюжета нет, но это то же событие, что другая новая новость target (N…);
- new — новое событие, target пустой;
- unclear — нельзя понять, о каком событии речь (кликбейт без факта), target пустой."""

SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["join", "same", "new", "unclear"]},
                    "target": {"type": "string"},
                },
                "required": ["id", "verdict", "target"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}


RECHECK_PROMPT = f"""Ты проверяешь, правильно ли новости разложены по сюжетам агрегатора новостей о Сербии. {RULES}

Вход — JSON: stories — сюжеты, id вида S…, у каждого несколько новостей (что произошло); news — новости, id вида N…, каждая уже добавлена в сюжет story.
Для каждой новости ответь keep: true, если она о той же истории, что сюжет (то же событие или дело, его развитие, последствия и реакции), и false, если это другое событие — пусть даже однотипное, на ту же тему или с теми же участниками."""

RECHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "news": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "keep": {"type": "boolean"}},
                "required": ["id", "keep"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["news"],
    "additionalProperties": False,
}
# join со сходством с сюжетом ниже порога Haiku перепроверяет на medium. Офлайн на 09–10.11: из 210 таких добавлений
# снял 9 из 19 чужих и 6 из 191 своих; Sonnet снимал больше чужих, но и своих столько же, а все добавления подряд —
# полнота падает сильнее, чем растёт точность
RECHECK_SIM = 0.9
RECHECK_EFFORT = "medium"

MERGE_PROMPT = f"""Ты проверяешь сюжеты агрегатора новостей о Сербии на дубли. {RULES}

Вход — JSON: stories — сюжеты, id вида S…, у каждого несколько новостей (время UTC · издание: что произошло); pairs — пары сюжетов, похожих по словам, id вида P…
Для каждой пары сначала в event назови общее событие обоих сюжетов одной фразой: кто что сделал, по какому поводу. Общий участник, партия, ведомство или тема — ещё не общее событие. Назвать общее событие не можешь — event пустой и same: false.
same: true, если оба сюжета — про одну историю: одно событие или одно дело, его развитие, последствия и реакции. Заявления, прозвучавшие на одном выступлении, пресс-конференции или в одном интервью, — true, даже если темы разные: смотри на время и участника.
false — если общего события нет: тот же участник, ведомство или тема, но другой повод; однотипные, но не связанные события. Если сомневаешься — false."""

# event в ответе склейки — общее событие пары, которое модель называет до same: 13.11 одиночки про «Студенческий список»
# (дестабилизация, Шешель, Курти, энергетика) склеились с консультациями в Скупщине — общим был только участник.
# Сходство их не отличает: у неверных 0.87–0.91, у верных столько же. Событие пишется в журнал склеек и на страницу
MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "event": {"type": "string"}, "same": {"type": "boolean"}},
                "required": ["id", "event", "same"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pairs"],
    "additionalProperties": False,
}
MERGE_EVERY = timedelta(hours=1)
MERGE_SIM = 0.865  # пары сюжетов с лучшей парой новостей не ниже — к модели (у MiniLM было 0.55)
# одиночка (сюжет из одного издания) с видимым — с тем же порогом, что при раскладке: новость, пришедшая раньше своего
# сюжета, проверяется так же, как пришедшая после. 12.11 ремонт JANAF (0.864) остался одиночкой рядом с сюжетом о нём
MERGE_FRAGMENT_SIM = CANDIDATE_SIM
MERGE_BATCH = 20  # пар в одном запросе
# склейку решает Sonnet. Фильтра Haiku перед ним больше нет: с крупными сюжетами (12.11) Haiku стал строже Sonnet —
# из 139 пар, которые Sonnet склеил из 357 прежних отказов, пропустил бы только 78
MERGE_MODEL = "claude-sonnet-5-5"
MERGE_EFFORT = "medium"
MERGE_SHOWN = 5  # сколько новостей каждого сюжета видит модель при склейке
MERGE_SURE_SIM = 0.9  # пары похожее этого идут к модели и без общих участников (у MiniLM было 0.7)
PRICES = {MODEL: (0.10, 0.50), MERGE_MODEL: (2.0, 10.0)}  # вход / выход за 1M токенов, $


# отдельные события внутри больших сюжетов — в свои сюжеты (13.11: из 8 предложений за день верны все 8)
LINES_PROMPT = """Ты проверяешь большие сюжеты агрегатора новостей о Сербии. Сюжет собирает одно событие или одно дело вместе с его развитием, последствиями и реакциями. Разрастаясь, он может вобрать отдельное событие: у него свой повод и о нём пишут как о самостоятельном, даже если участники те же.
Вход — JSON: news — новости одного сюжета по времени: n — номер, t — время UTC, source — издание, what — что произошло.
Верни lines — отдельные события внутри сюжета: name — 1–3 слова по-русски, why — одной фразой, чем это отдельное событие, news — номера n всех его новостей. Подробности, реакции, заявления и следствия без своего повода — не отдельное событие. Основное событие сюжета в lines не входит. Если такого нет или сомневаешься — пустой lines."""
LINES_SCHEMA = {
    "type": "object",
    "properties": {
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "why": {"type": "string"},
                               "news": {"type": "array", "items": {"type": "integer"}}},
                "required": ["name", "why", "news"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["lines"],
    "additionalProperties": False,
}
LINES_MIN = 15  # сюжет проверяем, когда в нём столько новостей,
# и снова — когда в нём прибавилось столько новостей (до 13.11 — когда вырос в 1.5 раза: 156 новостей s3868
# ждали проверки до 234). Когда проверять — не правило раскладки, в отпечаток не входит (stories_live.fingerprint)
LINES_STEP = 15
LINES_NEWS, LINES_OUTLETS = 3, 2  # отдельное событие — не меньше стольких новостей и изданий; и столько же остаётся
LINES_EFFORT = "medium"


def eligible(item: dict) -> bool:
    """Идёт ли новость в сюжеты: события (и новости без kind — русскоязычные до 13.11), без Reddit и Trends."""
    return item["section"] not in SKIP_SECTIONS and item.get("kind") in ("event", None)


def text_of(item: dict) -> str:
    ru = item.get("ru") or {}
    return item.get("what") or ru.get("title") or item["title"]


def embed(texts: list[str]) -> np.ndarray:
    """Векторы единичной длины. Модель скачивается при первом вызове (~2 ГБ) и дальше берётся из кэша."""
    from fastembed import TextEmbedding

    global _embedder
    if "_embedder" not in globals():
        _embedder = TextEmbedding(EMBED_MODEL)
    vectors = np.array(list(_embedder.embed([EMBED_PREFIX + t for t in texts], batch_size=64)), dtype=np.float32)
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def new_state() -> dict:
    return {"stories": {}, "items": {}, "next_id": 0}


def ask_model(client, system: str, content: str, schema: dict, effort: str = EFFORT,
              model: str = MODEL) -> tuple[dict, dict]:
    """Запрос к модели: (ответ по схеме, {"in", "out", "write", "read", "usd"} — токены, из них записанные в кэш
    и прочитанные из кэша, и оценка в долларах). Системный промпт — в кэше, как у перевода."""
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=translate.cached(system),
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason in ("max_tokens", "refusal"):
        raise RuntimeError(f"stop_reason={response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    used = translate.tokens(response.usage)
    return json.loads(text), {**used, "usd": translate.usd(used, PRICES[model])}


def visible(state: dict, sid: str) -> bool:
    return len({state["items"][u]["source"] for u in state["stories"][sid]["urls"]}) >= 2


def _start(state: dict, now: str) -> str:
    sid = f"s{state['next_id']}"
    state["next_id"] += 1
    state["stories"][sid] = {"created": now, "updated": now, "urls": []}
    return sid


def _add(state: dict, sid: str, url: str, item: dict, verdict: str, now: str, part: str | None = None,
         sim: float | None = None) -> None:
    """part — часть сюжета (событие); по умолчанию — сам сюжет: новость его открыла. sim — сходство с сюжетом (join).
    Новость уже была (перепроверка сняла её из сюжета) — история сохраняется, шаг пишется с from."""
    ru = item.get("ru") or {}
    prev = state["items"].get(url)
    n = (prev or {}).get("n")
    if n is None:  # короткий номер новости — для команд боту (stories_live.py): «/move 1234 s471»
        n = state.get("next_n", 1)
        state["next_n"] = n + 1
    state["items"][url] = {"n": n, "story": sid, "part": part or sid, "verdict": verdict, "seen": now, "source": item["source"],
                           "section": item["section"], "title": ru.get("title") or item["title"],
                           "what": item.get("what", ""), "entities": item.get("entities") or [],
                           "history": (prev or {}).get("history", [])}
    step = {"t": now, "why": verdict, "story": sid, "part": part or sid}
    if sim is not None:
        step["sim"] = round(sim, 3)
    if prev:
        step["from"] = prev["story"]
    state["items"][url]["history"].append(step)
    if sid:
        state["stories"][sid]["urls"].append(url)
        state["stories"][sid]["updated"] = now


def _live(state: dict, now: str) -> tuple[list[str], list[str]]:
    """Живые сюжеты: (адреса их последних новостей, id сюжета для каждого адреса)."""
    cutoff = (datetime.fromisoformat(now) - LIVE).isoformat()
    urls, owners = [], []
    for sid, story in state["stories"].items():
        if story["updated"] >= cutoff:
            for url in story["urls"][-STORY_ITEMS:]:
                urls.append(url)
                owners.append(sid)
    return urls, owners


def _decide(state: dict, batch: list[tuple[str, dict]], now: str, vectors: dict, ask) -> dict:
    """Одна пачка новых новостей: кандидаты, запрос к модели, решения в состояние. Возвращает токены."""
    live_urls, owners = _live(state, now)
    new_vecs = np.stack([vectors[url] for url, _ in batch])
    candidates = [{} for _ in batch]  # {id сюжета: лучшее сходство с его новостями}
    closest = {}  # id сюжета → его новости, самые похожие на новые новости, для которых он кандидат
    if live_urls:
        sims = new_vecs @ np.stack([vectors[u] for u in live_urls]).T
        for n in range(len(batch)):
            for k in np.argsort(-sims[n]):
                if sims[n, k] < CANDIDATE_SIM or len(candidates[n]) == CANDIDATES:
                    break
                if owners[k] not in candidates[n]:
                    candidates[n][owners[k]] = float(sims[n, k])
                    closest.setdefault(owners[k], []).append(live_urls[k])
    # соседи по сборке: если две новые новости похожи, обе идут к модели — вдруг это новое событие
    near = new_vecs @ new_vecs.T
    np.fill_diagonal(near, 0)
    asked = [n for n in range(len(batch)) if candidates[n] or near[n].max(initial=0) >= CANDIDATE_SIM]
    for n in range(len(batch)):
        if n not in asked:
            url, item = batch[n]
            _add(state, _start(state, now), url, item, "alone", now)
    if not asked:
        return {"in": 0, "out": 0}

    aliases = {}  # S0, S1… → id сюжета: короткие id модель не путает
    for n in asked:
        for sid in candidates[n]:
            aliases.setdefault(sid, f"S{len(aliases)}")
    stories = []
    spun = state.get("spun", {})  # выделенный сюжет → из какого (find_lines)
    for sid, alias in aliases.items():
        stories.append({"id": alias, "news": _shown(state, sid, vectors, extra=closest.get(sid, [])),
                        "outlets": len({state["items"][u]["source"] for u in state["stories"][sid]["urls"]}),
                        "updated": state["stories"][sid]["updated"][11:16]}
                       | ({"from": aliases[spun[sid]]} if spun.get(sid) in aliases else {}))
    news = []
    for n in asked:
        url, item = batch[n]
        ru = item.get("ru") or {}
        news.append({"id": f"N{n}", "source": item["source"], "time": now[11:16],
                     "title": ru.get("title") or item["title"], "what": item.get("what", ""),
                     "candidates": [aliases[s] for s in candidates[n]]})
    content = json.dumps({"stories": stories, "news": news}, ensure_ascii=False)
    answer, usage = ask(SYSTEM_PROMPT, content, SCHEMA)

    by_alias = {alias: sid for sid, alias in aliases.items()}
    verdicts = {d["id"]: d for d in answer["decisions"]}
    weak = []  # (n, alias сюжета): join со слабым сходством — на перепроверку
    for n in asked:  # по порядку: «same» ссылается на более раннюю новость, у которой сюжет уже есть
        url, item = batch[n]
        d = verdicts.get(f"N{n}", {"verdict": "new", "target": ""})
        verdict, target = d["verdict"], d["target"]
        part = sim = None
        if verdict == "join" and target in by_alias:  # можно и чужого кандидата из той же пачки: модель видела его
            sid = by_alias[target]
            part = _nearest_part(state, sid, vectors[url], vectors)
            sim = candidates[n].get(sid)
            if candidates[n].get(sid, 0) < RECHECK_SIM:
                weak.append((n, target))
        elif verdict == "same" and target.startswith("N") and target[1:].isdigit() \
                and batch[int(target[1:])][0] in state["items"] and state["items"][batch[int(target[1:])][0]]["story"]:
            sid = state["items"][batch[int(target[1:])][0]]["story"]
            part = state["items"][batch[int(target[1:])][0]]["part"]
        else:  # и unclear: без сюжета до неё не дошла бы склейка, а 12.11 «непонятным» стал внятный анонс матча
            verdict = "new" if verdict in ("join", "same", "new") else verdict
            sid = _start(state, now)
        _add(state, sid, url, item, verdict, now, part, sim)
    if weak:
        usage = {k: usage.get(k, 0) + v for k, v in _recheck(state, batch, weak, stories, now, ask).items()}
    return usage


def _nearest_part(state: dict, sid: str, vector: np.ndarray, vectors: dict) -> str:
    """Часть сюжета, в которой самая похожая на новость новость (среди последних STORY_ITEMS)."""
    urls = [u for u in state["stories"][sid]["urls"][-STORY_ITEMS:] if u in vectors]
    if not urls:
        return sid
    nearest = state["items"][urls[int((np.stack([vectors[u] for u in urls]) @ vector).argmax())]]
    return nearest.get("part") or nearest["story"]


def _recheck(state: dict, batch: list[tuple[str, dict]], weak: list[tuple[int, str]], stories: list[dict],
             now: str, ask) -> dict:
    """Перепроверка join со слабым сходством: сюжет — те же новости, что модель видела при раскладке. Снятая новость
    уходит в свой сюжет (verdict recheck); новости пачки, легшие к ней через same, остаются где были."""
    shown = {s["id"]: s for s in stories}
    content = json.dumps({"stories": [shown[a] for a in dict.fromkeys(a for _, a in weak)],
                          "news": [{"id": f"N{n}", "story": a, "title": state["items"][batch[n][0]]["title"],
                                    "what": batch[n][1].get("what", "")} for n, a in weak]}, ensure_ascii=False)
    answer, usage = ask(RECHECK_PROMPT, content, RECHECK_SCHEMA, RECHECK_EFFORT, MODEL)
    drop = {d["id"] for d in answer["news"] if not d["keep"]}
    for n, _ in weak:
        if f"N{n}" in drop:
            url, item = batch[n]
            state["stories"][state["items"][url]["story"]]["urls"].remove(url)
            _add(state, _start(state, now), url, item, "recheck", now)
    return usage


def _shown(state: dict, sid: str, vectors: dict, count: int = SHOWN_ITEMS, detail: bool = False,
           extra: list[str] = ()) -> list[str]:
    """Что модель видит о сюжете: первая новость и самые типичные — похожие на остальные новости сюжета больше всех,
    по порядку появления, без повторов текста. Не последние: с последними одна чужая новость, попав в сюжет,
    становится его лицом и тянет за собой следующие (10.11 так в сюжет решения REM легли 19 новостей
    про обыск у Вучичевича). detail — с временем (UTC) и изданием: склейке без времени «удар по Киеву» и «удар
    по Украине» в ту же ночь казались разными событиями (12.11). extra — ещё новости сюжета: при раскладке — самые похожие
    на новую новость. Типичные показывают главную тему, и поворот того же события на неё не похож: 12.11 призыв
    Брнабич в Дублине быстрее сформировать институты лёг отдельно от сюжета о саммите, хотя такие же новости в нём были."""
    urls = state["stories"][sid]["urls"][-STORY_ITEMS:]
    first = state["stories"][sid]["urls"][0]
    rest = [u for u in urls if u != first]
    if len(rest) > count - 1:
        v = np.stack([vectors[u] for u in urls])
        typical = (v @ v.T).mean(axis=1)[[urls.index(u) for u in rest]]
        keep = set(np.argsort(-typical)[:count - 1])
        rest = [u for n, u in enumerate(rest) if n in keep]
    order = state["stories"][sid]["urls"]
    chosen = sorted(set([first] + rest + list(extra)), key=order.index)
    items = [state["items"][u] for u in chosen]
    if detail:
        return list(dict.fromkeys(f"{it['seen'][8:10]}.{it['seen'][5:7]} {it['seen'][11:16]} · {it['source']}: {text_of(it)}"
                                  for it in items))
    return list(dict.fromkeys(text_of(it) for it in items))


# участники приходят и по-русски, и по-сербски, кириллицей и латиницей: сравниваем латиницей без диакритики —
# «ЯНАФ» и «JANAF», «Шошкич» и «Šoškić» совпадут (12.11 ремонт ЯНАФ не склеился с сюжетом о JANAF)
TRANSLIT = str.maketrans({**dict(zip("абвгдезиклмнопрстуфхцчшыэ", "abvgdeziklmnoprstufhccsye")),
                          "ё": "e", "ж": "z", "й": "j", "щ": "s", "ъ": "", "ь": "", "ю": "ju", "я": "ja",
                          "ђ": "dj", "ј": "j", "љ": "lj", "њ": "nj", "ћ": "c", "џ": "dz",
                          "č": "c", "ć": "c", "š": "s", "ž": "z", "đ": "dj"})


def _keys(entities: list[str]) -> set[str]:
    """Участники для сравнения: имя целиком и последнее слово («Деян Шошкич» и «Шошкич» совпадут), латиницей."""
    keys = set()
    for e in entities:
        e = e.lower().strip().translate(TRANSLIT)
        keys.add(e)
        last = e.replace("«", " ").replace("»", " ").split()[-1:] if e else []
        if last and len(last[0]) >= 4:
            keys.add(last[0])
    return keys


def _share_entities(state: dict, a: str, b: str) -> bool:
    """Есть ли у сюжетов общий участник. У кого участников нет совсем (русскоязычные ленты) — не отсеиваем."""
    ka, kb = ({k for u in state["stories"][s]["urls"] for k in _keys(state["items"][u].get("entities") or [])}
              for s in (a, b))
    return not ka or not kb or bool(ka & kb)


def _same_pairs(state: dict, pairs: list[tuple[str, str]], vectors: dict, model: str, effort: str, ask,
                total: dict) -> dict[int, str]:
    """Номера пар, которые модель считает одним событием, → общее событие, которое она назвала."""
    same = {}
    for start in range(0, len(pairs), MERGE_BATCH):
        chunk = pairs[start:start + MERGE_BATCH]
        aliases = {}
        for a, b in chunk:
            for s in (a, b):
                aliases.setdefault(s, f"S{len(aliases)}")
        content = json.dumps({"stories": [{"id": al, "news": _shown(state, s, vectors, MERGE_SHOWN, detail=True)} for s, al in aliases.items()],
                              "pairs": [{"id": f"P{n}", "a": aliases[a], "b": aliases[b]}
                                        for n, (a, b) in enumerate(chunk)]}, ensure_ascii=False)
        answer, usage = ask(MERGE_PROMPT, content, MERGE_SCHEMA, effort, model)
        for k in ("in", "out", "usd"):
            total[k] += usage.get(k, 0)
        total["asked"][model] = total["asked"].get(model, 0) + len(chunk)
        same |= {start + int(p["id"][1:]): p["event"].strip() for p in answer["pairs"]
                 if p["same"] and p["event"].strip() and p["id"][1:].isdigit()}
    return same


def merge(state: dict, now: str, vectors: dict, ask) -> dict:
    """Склейка дублей — сюжетов, на которые распалось одно событие. Пара идёт к модели, только если:
    хотя бы один сюжет видимый (два невидимых обрывка читателю не мешают, а невидимый рядом с видимым — новость,
    которая должна была лечь в видимый: пришла раньше него или модель ошиблась), хотя бы один вырос с прошлой склейки, лучшая пара
    новостей похожа не меньше MERGE_SIM (одиночка с видимым — MERGE_FRAGMENT_SIM), у сюжетов есть общий участник (entities; не нужен, если сходство
    не ниже MERGE_SURE_SIM), пару ещё не признавали разными — или с тех пор один из сюжетов вырос вдвое — и её части
    не разделяли руками (split, unmerge). Решает Sonnet. Каждая склейка — в журнал state["merges"] с частями, которые
    принёс склеенный сюжет: по нему unmerge её отменяет.
    Возвращает {"in", "out", "usd", "asked": {модель: пар}, "merged"}."""
    total = {"in": 0, "out": 0, "usd": 0.0, "asked": {}, "merged": 0}
    stories, checked = state["stories"], state.setdefault("checked", {})
    last = state.get("merged_at") or ""
    again = set(state.pop("merge_again", []))  # склеенные в прошлый проход: их пары с соседями ещё не проверены
    live_urls, owners = _live(state, now)
    shown = {sid for sid in set(owners) if visible(state, sid)}
    state["merged_at"] = now
    if len(live_urls) < 2:
        return total
    sims = np.stack([vectors[u] for u in live_urls])
    sims = sims @ sims.T
    rows, cols = np.nonzero(np.triu(sims, 1) >= min(MERGE_SIM, MERGE_FRAGMENT_SIM))
    candidates = {}
    for r, c in zip(rows, cols):
        a, b = sorted((owners[r], owners[c]), key=lambda s: int(s[1:]))
        both = a in shown and b in shown
        if a != b and (a in shown or b in shown) and sims[r, c] >= (MERGE_SIM if both else MERGE_FRAGMENT_SIM):
            candidates[(a, b)] = max(candidates.get((a, b), 0), sims[r, c])
    todo = []
    for (a, b), sim in sorted(candidates.items()):
        grew = stories[a]["updated"] >= last or stories[b]["updated"] >= last or a in again or b in again
        if not grew or _apart(state, a, b):
            continue
        old = checked.get(f"{a}|{b}")
        if old and len(stories[a]["urls"]) < 2 * old[0] and len(stories[b]["urls"]) < 2 * old[1]:
            continue
        if sim >= MERGE_SURE_SIM or _share_entities(state, a, b):
            todo.append((a, b))

    same = _same_pairs(state, todo, vectors, MERGE_MODEL, MERGE_EFFORT, ask, total)
    # за проход сюжет склеивается не больше одного раза: цепочки a ← b ← c на первом прогоне стянули аварию,
    # границу и ДТП в один сюжет на 450 новостей. Остальные пары с ним проверим в следующий раз, уже со склеенным.
    # Невидимые обрывки цепочку не строят: видимый сюжет за проход принимает их сколько угодно
    used, merges = set(), []
    for n, (a, b) in enumerate(todo):
        if n in same and a not in used and b not in used:
            if a in shown and b in shown:
                used.update((a, b))
                merges.append((a, b, same[n]))
            else:  # обрывок — в видимый, а не наоборот
                gone = a if a not in shown else b
                used.add(gone)
                merges.append((b if gone == a else a, gone, same[n]))
    for a, b in todo:
        if not {(a, b), (b, a)} & {m[:2] for m in merges} and a not in used and b not in used:
            checked[f"{a}|{b}"] = [len(stories[a]["urls"]), len(stories[b]["urls"])]

    moved = {}  # склеенный сюжет → куда ушёл: видимый мог принять обрывки и сам уйти в другой видимый
    for keep, gone, event in merges:
        while keep in moved:
            keep = moved[keep]
        moved[gone] = keep
        state.setdefault("merges", []).append(
            {"t": now, "keep": keep, "gone": gone, "parts": sorted({state["items"][u]["part"] for u in stories[gone]["urls"]}),
             "event": event})
        for u in stories[gone]["urls"]:
            it = state["items"][u]
            it["story"] = keep
            it.setdefault("history", []).append({"t": now, "why": "merge", "story": keep, "part": it["part"], "from": gone,
                                                    "event": event})
        stories[keep]["urls"] = sorted(stories[keep]["urls"] + stories[gone]["urls"],
                                       key=lambda u: state["items"][u]["seen"])
        stories[keep]["updated"] = max(stories[keep]["updated"], stories[gone]["updated"])
        state.setdefault("merged", {})[gone] = keep
        del stories[gone]
        total["merged"] += 1
    # склеенный сюжет вырос, но updated у него — время новостей, а не склейки: без пометки его пары, отложенные
    # из-за «не больше одного раза за проход», ждали бы новой новости
    grown = set()
    for keep in moved.values():
        while keep in moved:
            keep = moved[keep]
        grown.add(keep)
    state["merge_again"] = sorted(grown)
    return total


def find_lines(state: dict, now: str, ask) -> dict:
    """Отдельные события внутри больших видимых сюжетов (Sonnet) — в свои сюжеты (_spin_off); что нашли — в
    state["lines"][сюжет]: {"size", "t", "found"}. Сюжет проверяем, когда в нём LINES_MIN новостей, и снова — когда
    в нём прибавилось LINES_STEP. Модели не верим на слово: событие — не меньше LINES_NEWS новостей и LINES_OUTLETS изданий,
    и в сюжете остаётся не меньше LINES_NEWS. Возвращает {"in", "out", "usd", "checked", "found", "spun": [(сюжет, имя)]}."""
    total = {"in": 0, "out": 0, "usd": 0.0, "checked": 0, "found": 0, "spun": []}
    lines = state.setdefault("lines", {})
    for sid, story in list(state["stories"].items()):  # выделенные сюжеты добавляются по ходу
        size = len(story["urls"])
        if size < LINES_MIN or size < lines.get(sid, {}).get("size", 0) + LINES_STEP or not visible(state, sid):
            continue
        by_n = {state["items"][u]["n"]: u for u in story["urls"]}
        items = {n: state["items"][u] for n, u in by_n.items()}
        content = json.dumps({"news": [{"n": n, "t": it["seen"][5:16], "source": it["source"],
                                        "what": it.get("what") or it["title"]} for n, it in items.items()]},
                             ensure_ascii=False)
        answer, usage = ask(LINES_PROMPT, content, LINES_SCHEMA, LINES_EFFORT, MERGE_MODEL)
        for k in ("in", "out", "usd"):
            total[k] += usage.get(k, 0)
        found, taken = [], set()
        for line in answer["lines"]:
            news = sorted({n for n in line["news"] if n in items} - taken)  # новость — только в одно событие
            if (len(news) >= LINES_NEWS and len({items[n]["source"] for n in news}) >= LINES_OUTLETS
                    and size - len(taken) - len(news) >= LINES_NEWS and line["name"].strip()):
                taken |= set(news)
                found.append({"name": clean_name(line["name"]), "why": line["why"].strip(), "news": news})
        for line in found:
            line["to"] = _spin_off(state, sid, [by_n[n] for n in line["news"]], now)
            total["spun"].append((line["to"], line["name"]))
        lines[sid] = {"size": len(story["urls"]), "t": now, "found": found}
        total["checked"] += 1
        total["found"] += len(found)
    return total


def clean_name(name: str) -> str:
    """Имя без кавычек вокруг и точки в конце. Кавычки названия внутри остаются: «Разрешение на «Югославию»»."""
    name = name.strip().rstrip(".").strip()
    while len(name) > 1 and name[0] + name[-1] in ("«»", '""', "''") and not set(name[0] + name[-1]) & set(name[1:-1]):
        name = name[1:-1].strip()
    return name


def _spin_off(state: dict, parent: str, urls: list[str], now: str) -> str:
    """Новости отдельного события — из сюжета parent в новый сюжет. Связь — state["spun"][новый] = parent; склейка
    их обратно не соединит (state["apart"]) — ни с ним, ни с событиями, выделенными из него раньше: 13.11 «Отвод
    прокурора» склеился с «Разрешением на «Югославию»», выделенным из того же сюжета. Возвращает новый сюжет."""
    sid = None
    for u in sorted(urls, key=lambda u: state["items"][u]["seen"]):
        sid = move(state, u, sid, now, part=sid, why="line")
    siblings = {p for c, was in state.get("spun", {}).items() if was == parent and c in state["stories"]
                for p in _parts(state, c)}
    state.setdefault("apart", []).append([[sid], sorted(_parts(state, parent) | siblings)])
    state.setdefault("spun", {})[sid] = parent
    return sid


def _parts(state: dict, sid: str) -> set[str]:
    return {state["items"][u]["part"] for u in state["stories"][sid]["urls"]}


def _apart(state: dict, a: str, b: str) -> bool:
    """Разделены ли сюжеты руками: в state["apart"] — пары списков частей, которые склейка не соединяет."""
    pa, pb = _parts(state, a), _parts(state, b)
    return any(pa & set(x) and pb & set(y) or pa & set(y) and pb & set(x) for x, y in state.get("apart", []))


def move(state: dict, url: str, sid: str | None, now: str, part: str | None = None, why: str = "move") -> str:
    """Перенос новости без модели: в сюжет sid или в новый (None). Часть part; по умолчанию в новом сюжете — своя
    (новое событие), в существующем — прежняя (событие новости не меняется). Опустевший сюжет удаляется.
    Возвращает сюжет."""
    it, stories = state["items"][url], state["stories"]
    old = it["story"]
    if old:
        stories[old]["urls"].remove(url)
        if not stories[old]["urls"]:
            del stories[old]
    if sid is None:
        sid = _start(state, it["seen"])
        part = part or sid
    elif sid not in stories:
        stories[sid] = {"created": it["seen"], "updated": it["seen"], "urls": []}
    story = stories[sid]
    story["urls"] = sorted(story["urls"] + [url], key=lambda u: state["items"][u]["seen"])
    story["created"] = min(story["created"], it["seen"])
    story["updated"] = max(story["updated"], it["seen"])
    it["story"], it["part"] = sid, part or it["part"]
    it.setdefault("history", []).append({"t": now, "why": why, "story": sid, "part": it["part"], "from": old})
    return sid


def split(state: dict, parts: list[str], now: str, sid: str | None = None, why: str = "split") -> str:
    """Части parts — в отдельный сюжет sid (по умолчанию — id первой части, если свободен). Склейка их с тем, откуда
    они ушли, обратно не соединит (state["apart"]). Возвращает сюжет."""
    urls = sorted((u for u, it in state["items"].items() if it["story"] and it["part"] in parts),
                  key=lambda u: state["items"][u]["seen"])
    if not urls:
        raise ValueError(f"нет новостей в частях {parts}")
    left = {state["items"][u]["story"] for u in urls}
    sid = sid or (parts[0] if parts[0] not in state["stories"] else _start(state, now))  # части не меняются
    for u in urls:
        sid = move(state, u, sid, now, why=why)
    for old in left:
        if old in state["stories"] and old != sid:
            state.setdefault("apart", []).append([sorted(set(parts)), sorted(_parts(state, old))])
    return sid


def unmerge(state: dict, gone: str, now: str) -> str:
    """Отмена склейки: части, которые принёс сюжет gone (последняя запись журнала), — снова сюжет gone."""
    record = next(m for m in reversed(state.get("merges", [])) if m["gone"] == gone)
    return split(state, record["parts"], now, gone if gone not in state["stories"] else None, why="unmerge")


def merge_due(state: dict, now: str) -> bool:
    last = state.get("merged_at")
    return not last or datetime.fromisoformat(now) - datetime.fromisoformat(last) >= MERGE_EVERY


def update(state: dict, items: list[tuple[str, dict]], now: str, vectors: dict, ask) -> dict:
    """Раскладывает новости одной сборки: items — [(адрес, новость из архива)], now — время сборки (ISO),
    vectors — {адрес: вектор} для новых и для новостей живых сюжетов, ask(system, content, schema) → (ответ, токены).
    Новости, уже решённые раньше, и не-события пропускаются. Возвращает {"in", "out", "decided"}."""
    todo = [(url, item) for url, item in items if url not in state["items"] and eligible(item)]
    total = {"in": 0, "out": 0, "decided": len(todo)}
    for start in range(0, len(todo), BATCH):
        usage = _decide(state, todo[start:start + BATCH], now, vectors, ask)
        total["in"] += usage["in"]
        total["out"] += usage["out"]
    return total
