"""Сюжеты: подборки новостей разных изданий об одном событии. Векторы находят похожие сюжеты, модель решает.

На каждой сборке новые события (kind=event или без kind — русскоязычные ленты) раскладываются так:
1. вектор по «что произошло» (what), без него — по заголовку; fastembed, локально;
2. кандидаты — до CANDIDATES живых сюжетов (последняя новость не старше LIVE), сходство — с лучшей новостью сюжета,
   а не с его центром, чтобы сюжет не уплывал. Одиночка — тоже сюжет, просто из одной новости;
3. новости без кандидатов и без похожих соседей по сборке сразу становятся новыми сюжетами, остальные решает
   модель, пачками по BATCH: join (в сюжет S…), same (то же событие, что новая N…), new, unclear.
4. раз в MERGE_EVERY — склейка дублей (merge): похожие пары живых сюжетов модель проверяет на «одно событие».
Видимый сюжет — от двух изданий. Названий пока нет. Раскладку проверяем прогоном на снимке архива против эталона
(stories_replay.py, stories_gold.py), в живой сборке она идёт в теневом режиме (stories_live.py).

Состояние — один словарь, JSON: stories (id → новости и время), items (адрес → решение и то, что нужно для запроса).
Векторы хранятся отдельно: {адрес: вектор}.
"""
import json
from datetime import datetime, timedelta

import numpy as np

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
RULES = """Сюжет — одна история: событие, о котором пишут одно или несколько изданий, вместе с его развитием и последствиями. Сюжет шире одной новости, но уже темы.
- Один сюжет: разные издания пишут об одном и том же событии; развитие и последствия этого события (новые подробности, что нашли при обыске, кто ещё задержан по тому же делу, суд, отставки, решения и меры, принятые из-за него); прямые реакции и заявления по нему; одно заседание, решение или документ с несколькими пунктами (Совет REM на одном заседании наказал несколько телеканалов — один сюжет); одно интервью с заявлениями на разные темы; одни выборы — общие итоги, результаты отдельных списков и советов, реакции на них; подготовка, повестка и сами переговоры.
- Разные сюжеты: два похожих, но не связанных события (арестовали двух разных чиновников по разным делам — два сюжета); разные выборы (парламентские и выборы в национальные советы меньшинств — два сюжета); общая тема без общего события, когда одно не следует из другого («формирование правительства», «Сербия и ЕС»); заявление того же политика на другую тему; анонс события и само событие, если между ними нет общей новости.
- Похожие слова ещё не значат одно событие."""

SYSTEM_PROMPT = f"""Ты раскладываешь свежие новости о Сербии по сюжетам. {RULES}
Если сомневаешься — new: лишний сюжет потом склеят, а чужая новость в сюжете видна читателю.

Вход — JSON:
- stories: живые сюжеты-кандидаты, id вида S…; news — несколько новостей сюжета (что произошло);
- news: новые новости, id вида N…: издание, время, заголовок, что произошло (бывает пустым), candidates — сюжеты, похожие по словам.
Заголовок бывает кликбейтом — опирайся на смысл.

Для каждой новой новости верни verdict и target:
- join — о том же событии, что сюжет target, или о его развитии и последствиях (S… из её candidates);
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
Для каждой новости ответь keep: true, если она о той же истории, что сюжет (включая развитие события, его последствия и прямые реакции на него), и false, если это отдельное событие, не связанное с событием сюжета, — пусть даже на ту же тему или с теми же участниками."""

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

Вход — JSON: stories — сюжеты, id вида S…, у каждого несколько новостей (что произошло); pairs — пары сюжетов, похожих по словам, id вида P…
Для каждой пары ответь same: true, только если оба сюжета — про одну историю: одно и то же событие (те же участники, то же происшествие, заседание, интервью или решение) или событие и его прямое развитие и последствия. Однотипные события — не одно событие: две аварии, два ареста по разным делам, два заявления одного политика по разным поводам — false. Общая тема (выборы, «Алта банка») без общего события, когда один сюжет не следует из другого, — false. Если сомневаешься — false: дубль хуже видно, чем чужую новость в сюжете."""

MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "pairs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "same": {"type": "boolean"}},
                "required": ["id", "same"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["pairs"],
    "additionalProperties": False,
}
MERGE_EVERY = timedelta(hours=1)
MERGE_SIM = 0.865  # пары сюжетов с лучшей парой новостей не ниже — к модели (у MiniLM было 0.55)
MERGE_BATCH = 20  # пар в одном запросе
# склейку решает Sonnet: Haiku даже на effort medium склеивал однотипные события — два ареста, два суда.
# Но ошибается Haiku в одну сторону, поэтому он стоит перед Sonnet фильтром: его «разные» до Sonnet не доходят
MERGE_MODEL = "claude-sonnet-5-5"
MERGE_EFFORT = "medium"
MERGE_SHOWN = 5  # сколько новостей каждого сюжета видит модель при склейке
MERGE_SURE_SIM = 0.9  # пары похожее этого идут к модели и без общих участников (у MiniLM было 0.7)
MERGE_FILTER_EFFORT = "medium"  # Haiku перед Sonnet: на low он отбраковывал явные дубли, на medium пропускает лишнее — что и нужно
PRICES = {MODEL: (0.10, 0.50), MERGE_MODEL: (2.0, 10.0)}  # вход / выход за 1M токенов, $


def eligible(item: dict) -> bool:
    """Идёт ли новость в сюжеты: события и русскоязычные ленты (у них нет kind), без Reddit и Trends."""
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
    """Запрос к модели: (ответ по схеме, {"in", "out", "usd"} — токены и оценка в долларах)."""
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=system,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": content}],
    )
    if response.stop_reason in ("max_tokens", "refusal"):
        raise RuntimeError(f"stop_reason={response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    usage = response.usage
    price = PRICES[model]
    return json.loads(text), {"in": usage.input_tokens, "out": usage.output_tokens,
                              "usd": (usage.input_tokens * price[0] + usage.output_tokens * price[1]) / 1e6}


def visible(state: dict, sid: str) -> bool:
    return len({state["items"][u]["source"] for u in state["stories"][sid]["urls"]}) >= 2


def _start(state: dict, now: str) -> str:
    sid = f"s{state['next_id']}"
    state["next_id"] += 1
    state["stories"][sid] = {"created": now, "updated": now, "urls": []}
    return sid


def _add(state: dict, sid: str, url: str, item: dict, verdict: str, now: str) -> None:
    ru = item.get("ru") or {}
    state["items"][url] = {"story": sid, "verdict": verdict, "seen": now, "source": item["source"],
                           "section": item["section"], "title": ru.get("title") or item["title"],
                           "what": item.get("what", ""), "entities": item.get("entities") or []}
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
    if live_urls:
        sims = new_vecs @ np.stack([vectors[u] for u in live_urls]).T
        for n in range(len(batch)):
            for k in np.argsort(-sims[n]):
                if sims[n, k] < CANDIDATE_SIM or len(candidates[n]) == CANDIDATES:
                    break
                candidates[n].setdefault(owners[k], float(sims[n, k]))
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
    for sid, alias in aliases.items():
        stories.append({"id": alias, "news": _shown(state, sid, vectors)})
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
        if verdict == "join" and target in by_alias:  # можно и чужого кандидата из той же пачки: модель видела его
            sid = by_alias[target]
            if candidates[n].get(sid, 0) < RECHECK_SIM:
                weak.append((n, target))
        elif verdict == "same" and target.startswith("N") and target[1:].isdigit() \
                and batch[int(target[1:])][0] in state["items"] and state["items"][batch[int(target[1:])][0]]["story"]:
            sid = state["items"][batch[int(target[1:])][0]]["story"]
        elif verdict == "unclear":
            sid = None
        else:
            verdict = "new" if verdict in ("join", "same", "new") else verdict
            sid = _start(state, now)
        _add(state, sid, url, item, verdict, now)
    if weak:
        usage = {k: usage.get(k, 0) + v for k, v in _recheck(state, batch, weak, stories, now, ask).items()}
    return usage


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


def _shown(state: dict, sid: str, vectors: dict, count: int = SHOWN_ITEMS) -> list[str]:
    """Что модель видит о сюжете: первая новость и самые типичные — похожие на остальные новости сюжета больше всех,
    по порядку появления, без повторов текста. Не последние: с последними одна чужая новость, попав в сюжет,
    становится его лицом и тянет за собой следующие (10.11 так в сюжет решения REM легли 19 новостей
    про обыск у Вучичевича)."""
    urls = state["stories"][sid]["urls"][-STORY_ITEMS:]
    first = state["stories"][sid]["urls"][0]
    rest = [u for u in urls if u != first]
    if len(rest) > count - 1:
        v = np.stack([vectors[u] for u in urls])
        typical = (v @ v.T).mean(axis=1)[[urls.index(u) for u in rest]]
        keep = set(np.argsort(-typical)[:count - 1])
        rest = [u for n, u in enumerate(rest) if n in keep]
    return list(dict.fromkeys(text_of(state["items"][u]) for u in [first] + rest))


def _keys(entities: list[str]) -> set[str]:
    """Участники для сравнения: имя целиком и последнее слово («Деян Шошкич» и «Шошкич» совпадут)."""
    keys = set()
    for e in entities:
        e = e.lower().strip()
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
                total: dict) -> set[int]:
    """Номера пар, которые модель считает одним событием."""
    same = set()
    for start in range(0, len(pairs), MERGE_BATCH):
        chunk = pairs[start:start + MERGE_BATCH]
        aliases = {}
        for a, b in chunk:
            for s in (a, b):
                aliases.setdefault(s, f"S{len(aliases)}")
        content = json.dumps({"stories": [{"id": al, "news": _shown(state, s, vectors, MERGE_SHOWN)} for s, al in aliases.items()],
                              "pairs": [{"id": f"P{n}", "a": aliases[a], "b": aliases[b]}
                                        for n, (a, b) in enumerate(chunk)]}, ensure_ascii=False)
        answer, usage = ask(MERGE_PROMPT, content, MERGE_SCHEMA, effort, model)
        for k in ("in", "out", "usd"):
            total[k] += usage.get(k, 0)
        total["asked"][model] = total["asked"].get(model, 0) + len(chunk)
        same |= {start + int(p["id"][1:]) for p in answer["pairs"] if p["same"] and p["id"][1:].isdigit()}
    return same


def merge(state: dict, now: str, vectors: dict, ask) -> dict:
    """Склейка дублей — сюжетов, на которые распалось одно событие. Пара идёт к модели, только если:
    оба сюжета видимые (невидимые обрывки читателю не мешают), хотя бы один вырос с прошлой склейки, лучшая пара
    новостей похожа не меньше MERGE_SIM, у сюжетов есть общий участник (entities; не нужен, если сходство
    не ниже MERGE_SURE_SIM) и пару ещё не признавали разными — или с тех пор один из сюжетов вырос вдвое.
    Дальше каскад: Haiku отсеивает явно разные пары (он ошибается в сторону «одно событие»), Sonnet решает остальное.
    Возвращает {"in", "out", "usd", "asked": {модель: пар}, "merged"}."""
    total = {"in": 0, "out": 0, "usd": 0.0, "asked": {}, "merged": 0}
    stories, checked = state["stories"], state.setdefault("checked", {})
    last = state.get("merged_at") or ""
    live_urls, owners = _live(state, now)
    keep_rows = [n for n, sid in enumerate(owners) if visible(state, sid)]
    live_urls, owners = [live_urls[n] for n in keep_rows], [owners[n] for n in keep_rows]
    state["merged_at"] = now
    if len(live_urls) < 2:
        return total
    sims = np.stack([vectors[u] for u in live_urls])
    sims = sims @ sims.T
    rows, cols = np.nonzero(np.triu(sims, 1) >= MERGE_SIM)
    candidates = {}
    for r, c in zip(rows, cols):
        a, b = sorted((owners[r], owners[c]), key=lambda s: int(s[1:]))
        if a != b:
            candidates[(a, b)] = max(candidates.get((a, b), 0), sims[r, c])
    todo = []
    for (a, b), sim in sorted(candidates.items()):
        if stories[a]["updated"] < last and stories[b]["updated"] < last:
            continue
        old = checked.get(f"{a}|{b}")
        if old and len(stories[a]["urls"]) < 2 * old[0] and len(stories[b]["urls"]) < 2 * old[1]:
            continue
        if sim >= MERGE_SURE_SIM or _share_entities(state, a, b):
            todo.append((a, b))

    maybe = [todo[n] for n in sorted(_same_pairs(state, todo, vectors, MODEL, MERGE_FILTER_EFFORT, ask, total))]
    same = _same_pairs(state, maybe, vectors, MERGE_MODEL, MERGE_EFFORT, ask, total)
    # за проход сюжет склеивается не больше одного раза: цепочки a ← b ← c на первом прогоне стянули аварию,
    # границу и ДТП в один сюжет на 450 новостей. Остальные пары с ним проверим в следующий раз, уже со склеенным
    used, merges = set(), []
    for n, (a, b) in enumerate(maybe):
        if n in same and a not in used and b not in used:
            used.update((a, b))
            merges.append((a, b))
    for a, b in todo:
        if (a, b) not in merges and a not in used and b not in used:
            checked[f"{a}|{b}"] = [len(stories[a]["urls"]), len(stories[b]["urls"])]

    for keep, gone in merges:
        for u in stories[gone]["urls"]:
            state["items"][u]["story"] = keep
        stories[keep]["urls"] = sorted(stories[keep]["urls"] + stories[gone]["urls"],
                                       key=lambda u: state["items"][u]["seen"])
        stories[keep]["updated"] = max(stories[keep]["updated"], stories[gone]["updated"])
        state.setdefault("merged", {})[gone] = keep
        del stories[gone]
        total["merged"] += 1
    return total


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
