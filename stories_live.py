"""Сюжеты в живой сборке — теневой режим: build.py раскладывает новости архива по сюжетам (stories.py) и пишет
скрытую страницу site/plot/ (vestomat.rs/plot/) — посмотреть, как это было бы на сайте. На главную страницу сюжеты пока не идут.

Состояние — cache/stories.json, векторы новостей живых сюжетов — cache/story-vectors.npz; оба в кэше GitHub Actions
вместе с архивом. Пропал кэш — начинаем заново с новостей за последние BOOTSTRAP часов, а не со всего архива.
Расход по дням копится в состоянии и виден на странице.

    .venv/bin/python stories_live.py [архив.json]   — локально: сборка сюжетов на архиве (по умолчанию
                                                      cache/archive.json) и страница site/plot/
"""
import html
import json
import os
import sys
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

import stories

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "cache" / "stories.json"
VECTORS_FILE = ROOT / "cache" / "story-vectors.npz"
PAGE_DIR = ROOT / "site" / "plot"
BOOTSTRAP = timedelta(hours=12)
# новость без перевода ждёт одну сборку (перевод мог опоздать), потом идёт по оригинальному заголовку
WAIT_TRANSLATION = timedelta(minutes=20)
# сюжет, который не обновлялся дольше, убираем из состояния вместе с его новостями: архив их уже забыл
# (ARCHIVE_DAYS в build.py — 4 дня), так что заново раскладывать их не придётся
FORGET = timedelta(days=5)
SPENT_DAYS = 14
PAGE_WINDOW = timedelta(hours=24)  # на странице — видимые сюжеты, обновлявшиеся за это время
LOCAL_TZ = ZoneInfo("Europe/Belgrade")


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
    # «непонятно» (unclear) — новости без сюжета: забываем по времени
    state["items"] = {u: it for u, it in state["items"].items() if it["story"] or it["seen"] >= cutoff}
    state["checked"] = {k: v for k, v in state.get("checked", {}).items()
                        if all(s in state["stories"] for s in k.split("|"))}
    state.pop("merged", None)  # кто в кого склеился — только для разбора прогонов
    for u in set(vectors) - set(state["items"]):
        del vectors[u]


def ask_with_spending(state: dict, day: str):
    """ask для stories.update и stories.merge: запрос к модели, расход — в state["spent"][день]."""
    import anthropic

    client = anthropic.Anthropic()
    spent = state.setdefault("spent", {})

    def ask(system: str, content: str, schema: dict, effort: str = stories.EFFORT,
            model: str = stories.MODEL) -> tuple[dict, dict]:
        answer, usage = stories.ask_model(client, system, content, schema, effort, model)
        total = spent.setdefault(day, {"calls": 0, "in": 0, "out": 0, "usd": 0.0})
        total["calls"] += 1
        for k in ("in", "out", "usd"):
            total[k] = round(total[k] + usage[k], 6)
        return answer, usage

    return ask


def run(archive: dict, sections: dict) -> None:
    """Одна сборка: новые новости архива — в сюжеты, раз в час склейка, потом страница. sections — {id: название}."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state, vectors = load_state()
    if state["items"] and state.get("version", 0) < 1:
        # 12.11: склейка стала брать и пары «невидимый + видимый». Обрывки, которые появились раньше, она бы
        # не проверила — их сюжеты с прошлой склейки не росли. Один раз проверяем все живые пары
        state["merged_at"] = ""
    state["version"] = 1
    fresh_start = not state["items"]
    if fresh_start:  # что старше начала, не раскладываем и потом: иначе следующая сборка возьмёт все LIVE часов
        state["started"] = (datetime.fromisoformat(now) - BOOTSTRAP).isoformat()
    since = max(state.get("started", ""), (datetime.fromisoformat(now) - stories.LIVE).isoformat())
    wait = (datetime.fromisoformat(now) - WAIT_TRANSLATION).isoformat()
    items = [(u, a) for u, a in archive.items()
             if u not in state["items"] and stories.eligible(a) and a["seen"] >= since
             and (a.get("ru") or a.get("what") or a["seen"] < wait)]
    items.sort(key=lambda x: x[1]["seen"])
    print(f"Сюжеты: новых новостей {len(items)}{' (начинаем заново)' if fresh_start else ''}", file=sys.stderr)

    # векторы всех новостей в памяти: для раскладки нужны последние новости живых сюжетов, а для страницы — все
    need = [u for u, _ in items] + [u for u in state["items"] if u not in vectors]
    texts = {u: stories.text_of(archive.get(u) or state["items"][u]) for u in need}
    if need:
        for u, v in zip(need, stories.embed([texts[u] for u in need])):
            vectors[u] = v

    day = datetime.fromisoformat(now).astimezone(LOCAL_TZ).date().isoformat()
    ask = ask_with_spending(state, day)
    # по сборкам, в которых новости впервые появились (seen), как в прогонах на снимках: при старте с нуля так
    # время сюжетов настоящее, а не время этой сборки. Новость, ждавшая перевода, идёт со временем прошлой сборки
    builds = {}
    for url, item in items:
        builds.setdefault(max(item["seen"], state.get("built", "")), []).append((url, item))
    decided = merged = 0
    for seen, group in sorted(builds.items()):
        decided += stories.update(state, group, seen, vectors, ask)["decided"]
        if stories.merge_due(state, seen):
            merged += stories.merge(state, seen, vectors, ask)["merged"]
    forget_old(state, vectors, now)
    state["spent"] = {d: s for d, s in sorted(state.get("spent", {}).items())[-SPENT_DAYS:]}
    state["built"] = now
    save_state(state, vectors)
    print(f"Сюжеты: решено {decided}, склеено {merged}, сюжетов {len(state['stories'])}, "
          f"за сегодня ≈ ${state['spent'].get(day, {}).get('usd', 0):.3f}", file=sys.stderr)
    write_page(state, vectors, sections)


def write_page(state: dict, vectors: dict, sections: dict, error: str = "") -> None:
    """site/plot/index.html — статичная страница без скриптов: видимые сюжеты за PAGE_WINDOW, крупные сверху."""
    e = html.escape
    now = state.get("built") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    cutoff = (datetime.fromisoformat(now) - PAGE_WINDOW).isoformat()
    shown = [sid for sid, s in state["stories"].items() if s["updated"] >= cutoff and stories.visible(state, sid)]

    def sources(sid):
        return {state["items"][u]["source"] for u in state["stories"][sid]["urls"]}

    shown.sort(key=lambda sid: (len(sources(sid)), state["stories"][sid]["updated"]), reverse=True)

    def local_time(iso: str) -> str:
        return datetime.fromisoformat(iso).astimezone(LOCAL_TZ).strftime("%d.%m %H:%M")

    def headline(urls: list[str]) -> str:
        """Пока у сюжетов нет своих названий — «что произошло» (what, ровный пересказ без кликбейта из перевода)
        самой типичной новости: больше всех похожей на остальные новости сюжета. Без what — заголовок."""
        with_what = [u for u in urls if state["items"][u].get("what") and u in vectors]
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

    def item_row(u: str, it: dict, extra: str) -> str:
        what = it.get("what", "")
        return (f'<li><span class="meta">{local_time(it["seen"])} · {e(it["source"])} · '
                f'{e(sections.get(it["section"], it["section"]))}'
                f'{" · " + e(it["verdict"]) if it["verdict"] in ("same", "recheck") else ""}{extra}</span><br>'
                f'<a href="{e(u)}" rel="noopener" target="_blank">{e(it["title"])}</a>'
                f'{f"<br><span class=what>{e(what)}</span>" if what and what != it["title"] else ""}</li>')

    cards = []
    for sid in shown:
        story = state["stories"][sid]
        urls = story["urls"]
        items = [state["items"][u] for u in urls]
        camps = Counter(sections.get(it["section"], it["section"]) for it in items)
        near = closest(urls, urls)
        rows = "".join(item_row(u, it, sim_mark(near.get(u, (None,))[0]))
                       for u, it in sorted(zip(urls, items), key=lambda pair: pair[1]["seen"], reverse=True))  # свежие сверху
        cards.append(
            f'<details id="{sid}"><summary><b>{e(headline(urls))}</b><br><span class="meta">{len(urls)} новостей · '
            f'{len(sources(sid))} изданий · {e(", ".join(f"{c} {n}" for c, n in camps.most_common()))} · '
            f'{local_time(story["created"])} — {local_time(story["updated"])} · {sid}</span></summary>'
            f'<ul>{rows}</ul></details>')

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
        return f' · ближе всего <a href="#{target}">{target}</a>{sim_mark(sim)}'

    # verdict: alone — похожих не было, модель не спрашивали; new — модель решила, что это новое событие
    def verdict(u: str) -> str:  # same и recheck item_row уже пишет
        v = state["items"][u]["verdict"]
        return "" if v in ("same", "recheck") else f" · {e(v)}"

    lonely = "".join(item_row(u, state["items"][u], f' · {state["items"][u]["story"]}{verdict(u)}{nearest(u)}')
                     for u in hidden)

    spent = "".join(f"<tr><td>{d}</td><td>{s['calls']}</td><td>${s['usd']:.3f}</td></tr>"
                    for d, s in sorted(state.get("spent", {}).items(), reverse=True))
    total = len(state["stories"])
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow">
<title>Сюжеты — теневой режим</title>
<style>body{{font:15px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#222;background:#fff}}
summary{{cursor:pointer;padding:6px 0}}details{{border-bottom:1px solid #eee}}ul{{margin:4px 0 12px;padding-left:20px}}
li{{margin:6px 0}}.meta{{color:#888;font-size:13px}}a{{color:#1a5fb4}}table{{border-collapse:collapse;font-size:13px}}
td{{padding:2px 14px 2px 0}}.what{{color:#777;font-size:13px}}
.sim.mid{{color:#b7791f}}.sim.low{{color:#c0392b;font-weight:600}}.err{{background:#fde8d8;color:#a33;padding:8px 12px}}
@media (prefers-color-scheme:dark){{body{{background:#161616;color:#ddd}}details{{border-color:#333}}a{{color:#78aeed}}}}</style>
</head><body>
<h1>Сюжеты — теневой режим</h1>
<p class="meta">Сборка {e(local_time(now))} · видимых сюжетов (от двух изданий) за сутки: {len(shown)} · всего в памяти: {total}.
Страница не индексируется и на сайте нигде не упомянута.</p>
{f'<p class="err">Сюжеты в этой сборке не обновились: {e(error)}</p>' if error else ""}
<p class="meta">Цифра у новости — сходство с самой похожей другой новостью сюжета (у одиночных — с новостью видимого сюжета).
Модель видит новость с сюжетом, только если сходство не ниже {stories.CANDIDATE_SIM}; <span class="sim mid">жёлтые</span> — ниже
{stories.RECHECK_SIM}, такое добавление перепроверяет Haiku; <span class="sim low">красные</span> — ниже {stories.CANDIDATE_SIM}.
Серым под заголовком — «что произошло» (what): по нему считается сходство.</p>
{"".join(cards) or "<p>Пока пусто.</p>"}
<h2>Не попали в видимые сюжеты</h2>
<details><summary>{len(hidden)} новостей за сутки в сюжетах из одного издания — самые похожие на видимый сюжет сверху</summary>
<ul>{lonely}</ul></details>
<h2>Расход</h2><table><tr><td>день</td><td>запросов</td><td>≈ $</td></tr>{spent}</table>
</body></html>"""
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    (PAGE_DIR / "index.html").write_text(page, "utf-8")


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
