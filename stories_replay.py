"""Прогон сюжетов на снимке архива — как будто сборки шли эти дни, и сравнение с эталоном (stories_gold.py).

    .venv/bin/python stories_replay.py run <снимок archive-*.json> <с ГГГГ-ММ-ДД> <по ГГГГ-ММ-ДД> [метка]
    .venv/bin/python stories_replay.py score <replay-*.json> <gold-ГГГГ-ММ-ДД.json>

run: новости идут по времени seen, одна сборка — одно значение seen (сборка ставит его всем новым новостям разом),
каждая сборка — stories.update. Первые дни — разогрев: к дню эталона уже есть живые сюжеты. Векторы кэшируются
в datasets/vectors-<модель>.npz, ответы модели — в datasets/replay-cache.json по хэшу запроса: повторный прогон
без изменений бесплатный. Итог — datasets/replay-<по>[-метка].json.

score: на новостях дня эталона — точность и полнота по парам, дробление крупных событий, чужие новости в сюжетах.
Отчёт — тот же файл с .html.
"""
import collections
import hashlib
import html
import json
import sys
from datetime import datetime
from itertools import combinations
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

import stories

LOCAL_TZ = ZoneInfo("Europe/Belgrade")
CAMPS = {"independent": "критичные", "investigations": "критичные", "state": "государственные",
         "mainstream": "крупные порталы", "progov": "близкие к SNS", "economy": "экономика",
         "foreign": "мир о Сербии", "foreign-sr": "иностранные", "regional": "регионы"}


def local_day(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(LOCAL_TZ).date().isoformat()


def load_vectors(path: Path, archive: dict, urls: list[str]) -> dict:
    cache = {}
    if path.exists():
        z = np.load(path)
        cache = dict(zip(json.loads(str(z["urls"])), z["vectors"]))
    # вектор зависит от текста: если what появился или поменялся, старый не годится
    texts = {u: stories.text_of(archive[u]) for u in urls}
    keys = json.loads(str(np.load(path)["texts"])) if path.exists() else {}
    missing = [u for u in urls if u not in cache or keys.get(u) != texts[u]]
    if missing:
        print(f"векторы: считаем {len(missing)}…", file=sys.stderr)
        for u, v in zip(missing, stories.embed([texts[u] for u in missing])):
            cache[u] = v
            keys[u] = texts[u]
        np.savez(path, urls=np.array(json.dumps(list(cache))), vectors=np.stack(list(cache.values())),
                 texts=np.array(json.dumps(keys, ensure_ascii=False)))
    return cache


def cached_ask(cache_path: Path):
    """ask для stories.update: ответ из кэша по хэшу запроса, иначе запрос к модели."""
    import anthropic

    client = anthropic.Anthropic()
    cache = json.loads(cache_path.read_text("utf-8")) if cache_path.exists() else {}
    # usd — потрачено в этом прогоне, full_usd — сколько стоил бы прогон без кэша (столько же стоил бы день в проде)
    spent = {"in": 0, "out": 0, "usd": 0.0, "full_usd": 0.0, "calls": 0, "cached": 0}

    def ask(system: str, content: str, schema: dict, effort: str = stories.EFFORT,
            model: str = stories.MODEL) -> tuple[dict, dict]:
        key = hashlib.sha256(json.dumps([model, effort, system, content, schema],
                                        ensure_ascii=False).encode()).hexdigest()
        if key in cache:
            spent["cached"] += 1
            spent["full_usd"] += cache[key]["usage"].get("usd", 0)
            return cache[key]["answer"], cache[key]["usage"]
        answer, usage = stories.ask_model(client, system, content, schema, effort, model)
        cache[key] = {"answer": answer, "usage": usage}
        spent["in"] += usage["in"]
        spent["out"] += usage["out"]
        spent["usd"] += usage["usd"]
        spent["full_usd"] += usage["usd"]
        spent["calls"] += 1
        if spent["calls"] % 10 == 0:
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
        return answer, usage

    def save():
        cache_path.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")

    return ask, spent, save


def run(snapshot: Path, first: str, last: str, tag: str = "") -> Path:
    archive = json.loads(snapshot.read_text("utf-8"))
    urls = [u for u, a in archive.items() if first <= local_day(a["seen"]) <= last and stories.eligible(a)]
    urls.sort(key=lambda u: archive[u]["seen"])
    vectors = load_vectors(snapshot.parent / f"vectors-{stories.EMBED_MODEL.split('/')[-1]}.npz", archive, urls)
    builds = collections.defaultdict(list)
    for u in urls:
        builds[archive[u]["seen"]].append((u, archive[u]))
    print(f"{len(urls)} новостей, {len(builds)} сборок, {first} — {last}", file=sys.stderr)

    ask, spent, save = cached_ask(snapshot.parent / "replay-cache.json")
    state = stories.new_state()
    try:
        merges, asked = 0, collections.Counter()
        for n, (now, items) in enumerate(sorted(builds.items())):
            stories.update(state, items, now, vectors, ask)
            if stories.merge_due(state, now):
                result = stories.merge(state, now, vectors, ask)
                merges += result["merged"]
                asked.update(result["asked"])
            if n % 20 == 0:
                usd = spent["usd"]
                print(f"  {now[:16]}  сборка {n}/{len(builds)}, сюжетов {len(state['stories'])}, склеено {merges}, "
                      f"запросов {spent['calls']} (+{spent['cached']} из кэша), ≈ ${usd:.2f}", file=sys.stderr)
    finally:
        save()
    usd = spent["usd"]
    print(f"готово: запросов {spent['calls']}, из кэша {spent['cached']}, токены {spent['in']}/{spent['out']}, "
          f"≈ ${usd:.2f} (без кэша ${spent['full_usd']:.2f}); склеено {merges}, пар к моделям {dict(asked)}",
          file=sys.stderr)
    out = snapshot.parent / f"replay-{last}{'-' + tag if tag else ''}.json"
    params = {k: getattr(stories, k) for k in ("MODEL", "EFFORT", "EMBED_MODEL", "CANDIDATES", "CANDIDATE_SIM",
                                                "STORY_ITEMS", "SHOWN_ITEMS", "BATCH", "MERGE_SIM", "MERGE_BATCH",
                                                "MERGE_MODEL", "MERGE_EFFORT", "MERGE_SHOWN", "MERGE_FILTER_EFFORT")}
    params["MERGE_EVERY"] = str(stories.MERGE_EVERY)
    params["MERGE_SURE_SIM"] = stories.MERGE_SURE_SIM
    out.write_text(json.dumps({"snapshot": snapshot.name, "first": first, "last": last, "params": params,
                               "spent": {**spent, "usd": round(usd, 4), "full_usd": round(spent["full_usd"], 4),
                                         "merged": merges, "asked": dict(asked)}, "state": state}, ensure_ascii=False), "utf-8")
    print(f"→ {out}", file=sys.stderr)
    return out


def pairs(groups) -> set:
    return {tuple(sorted(p)) for g in groups for p in combinations(g, 2)}


def score(replay_path: Path, gold_path: Path) -> Path:
    replay = json.loads(replay_path.read_text("utf-8"))
    gold = json.loads(gold_path.read_text("utf-8"))
    state = replay["state"]
    day = set(i["url"] for i in gold["items"])
    by_url = {i["url"]: i for i in gold["items"]}
    gold_of = {u: n for n, e in enumerate(gold["events"]) for u in e["urls"]}
    ours_of = {u: it["story"] for u, it in state["items"].items() if u in day and it["story"]}
    ours = collections.defaultdict(list)
    for u, sid in ours_of.items():
        ours[sid].append(u)
    ours = {sid: us for sid, us in ours.items() if len(us) >= 2}

    gold_pairs, our_pairs = pairs(e["urls"] for e in gold["events"]), pairs(ours.values())
    hit = gold_pairs & our_pairs
    precision = len(hit) / len(our_pairs) if our_pairs else 0
    recall = len(hit) / len(gold_pairs) if gold_pairs else 0

    # крупные события эталона (3+ изданий): на сколько наших сюжетов разошлись и какая доля в самом большом куске
    big = [n for n, e in enumerate(gold["events"]) if len({by_url[u]["source"] for u in e["urls"]}) >= 3]
    pieces, largest = [], []
    for n in big:
        counts = collections.Counter(ours_of.get(u) or f"-{u}" for u in gold["events"][n]["urls"])
        pieces.append(len(counts))
        largest.append(counts.most_common(1)[0][1] / len(gold["events"][n]["urls"]))
    # чужие новости: в нашем сюжете всё, что не из главного события эталона (одиночки эталона — тоже чужие)
    mixed = []
    for sid, us in ours.items():
        labels = collections.Counter(gold_of.get(u, f"-{u}") for u in us)
        main, count = labels.most_common(1)[0]
        if count < len(us):
            mixed.append((sid, main, [u for u in us if gold_of.get(u, f"-{u}") != main]))
    stranger = sum(len(x[2]) for x in mixed)
    verdicts = collections.Counter(state["items"][u]["verdict"] for u in day if u in state["items"])
    missing = len(day - set(state["items"]))

    m = {
        "pair_precision": round(precision, 3), "pair_recall": round(recall, 3),
        "our_stories_2plus": len(ours), "gold_stories": len(gold["events"]),
        "big_gold_events": len(big), "big_pieces_avg": round(sum(pieces) / len(pieces), 2) if pieces else 0,
        "big_largest_share": round(sum(largest) / len(largest), 3) if largest else 0,
        "mixed_stories": len(mixed), "stranger_items": stranger,
        "verdicts": dict(verdicts), "not_in_replay": missing, "spent": replay["spent"],
    }
    print(json.dumps(m, ensure_ascii=False, indent=1))

    e = html.escape

    def row(u: str, mark: str = "") -> str:
        i = by_url[u]
        return (f'<li>{mark}<span class="meta">{i["time"]} · {e(i["source"])} · {CAMPS.get(i["section"], i["section"])}'
                f' · {state["items"].get(u, {}).get("verdict", "—")}</span><br>{e(i["what"] or i["title"])}</li>')

    split_html = []
    for n, p in sorted(zip(big, pieces), key=lambda x: -x[1]):
        if p == 1:
            continue
        groups = collections.defaultdict(list)
        for u in gold["events"][n]["urls"]:
            groups[ours_of.get(u) or "одиночка"].append(u)
        body = "".join(f"<p class='meta'>{e(g)} — {len(us)}</p><ul>{''.join(row(u) for u in us)}</ul>"
                       for g, us in sorted(groups.items(), key=lambda x: -len(x[1])))
        split_html.append(f"<details><summary>{e(gold['events'][n]['title'])} — {p} кусков</summary>{body}</details>")
    mixed_html = []
    for sid, main, wrong in sorted(mixed, key=lambda x: -len(x[2])):
        title = gold["events"][main]["title"] if isinstance(main, int) else "одиночка эталона"
        body = f"<ul>{''.join(row(u, '<b class=bad>чужая</b> ') for u in wrong)}</ul>"
        mixed_html.append(f"<details><summary>{e(sid)}: основа — {e(title)}; чужих {len(wrong)}</summary>{body}</details>")

    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Прогон сюжетов {gold["day"]}</title>
<style>body{{font:15px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#222}}
summary{{cursor:pointer;padding:4px 0}}details{{border-bottom:1px solid #eee}}ul{{margin:4px 0 10px;padding-left:20px}}
li{{margin:4px 0}}.meta{{color:#888;font-size:13px}}.bad{{background:#fde8d8;color:#a33;font-size:12px;padding:0 4px}}
table{{border-collapse:collapse}}td{{padding:2px 12px 2px 0}}</style></head><body>
<h1>Прогон {e(replay_path.name)} против эталона {gold["day"]}</h1>
<table>{"".join(f"<tr><td>{e(k)}</td><td>{e(json.dumps(v, ensure_ascii=False))}</td></tr>" for k, v in m.items())}
<tr><td>params</td><td>{e(json.dumps(replay["params"], ensure_ascii=False))}</td></tr></table>
<h2>Сюжеты с чужими новостями — {len(mixed)}</h2>{"".join(mixed_html)}
<h2>Раздробленные крупные события — {sum(1 for p in pieces if p > 1)} из {len(big)}</h2>{"".join(split_html)}
</body></html>"""
    out = replay_path.with_suffix(".html")
    out.write_text(page, "utf-8")
    print(f"→ {out}", file=sys.stderr)
    return out


if __name__ == "__main__":
    if len(sys.argv) in (5, 6) and sys.argv[1] == "run":
        run(Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5] if len(sys.argv) == 6 else "")
    elif len(sys.argv) == 4 and sys.argv[1] == "score":
        score(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        sys.exit(__doc__)
