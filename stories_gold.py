"""Эталон сюжетов за день: Opus раскладывает все события дня разом, отчёт — для ручной проверки.
С эталоном потом сравнивается потоковая схема сюжетов (заметка Obsidian «Сюжеты — план реализации», шаг 1).

    .venv/bin/python stories_gold.py label <снимок archive-*.json> <ГГГГ-ММ-ДД>  — запрос к модели (~$1–2), эталон и отчёт
    .venv/bin/python stories_gold.py report <gold-ГГГГ-ММ-ДД.json>               — только отчёт, например после правок эталона

Эталон кладётся рядом со снимком: gold-ГГГГ-ММ-ДД.json и .html. В него попадают новости, впервые увиденные в этот день
по Белграду, кроме Reddit и Trends, с kind=event или без kind (русскоязычные ленты). Сюжет из одной новости
в эталон не пишется: всё, чего нет ни в одном сюжете и не в «непонятно», — одиночки.
"""
import collections
import html
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MODEL = "claude-opus-5-5"
EFFORT = "high"
PRICE_PER_MTOK = (4.0, 20.0)  # вход / выход, $
LOCAL_TZ = ZoneInfo("Europe/Belgrade")
SKIP_SECTIONS = {"reddit", "trends"}
CAMPS = {"independent": "критичные", "investigations": "критичные", "state": "государственные",
         "mainstream": "крупные порталы", "progov": "близкие к SNS", "economy": "экономика",
         "foreign": "мир о Сербии", "foreign-sr": "иностранные", "regional": "регионы"}

SYSTEM_PROMPT = """Ты редактор агрегатора сербских новостей. Тебе дан список новостей за один день от разных изданий. Разложи их по сюжетам.

Сюжет — это одно конкретное событие, о котором написали одно или несколько изданий. Сюжет узкий: не тема, а событие.
- Один сюжет: разные издания пишут об одном и том же событии; развитие того же события в тот же день (новые подробности, задержание по тому же делу, итог того же заседания); прямые реакции и заявления именно по этому событию; одно заседание, решение или документ с несколькими пунктами (Совет REM на одном заседании наказал несколько телеканалов — один сюжет, а не по сюжету на канал); одно интервью с заявлениями на разные темы (Шошкич на РТС о курсе динара и об «Алта банке» — один сюжет); одни выборы — общие итоги, результаты отдельных списков и советов, реакции на них (выборы в национальные советы всех меньшинств в один день — один сюжет); подготовка, повестка и сами переговоры (делегация вылетела в Абу-Даби, Лавров о первой теме, анонс переговоров — один сюжет).
- Разные сюжеты: два похожих события с разными участниками (арестовали двух разных чиновников — два сюжета); разные выборы (парламентские и выборы в национальные советы меньшинств — два сюжета); общая тема без общего события («формирование правительства», «Сербия и ЕС», «Вучич о разном»); заявление политика на другую тему в тот же день; анонс события и само событие, если между ними нет общей новости.
- Если сомневаешься, одно ли это событие, — раздели. Лишнее разделение исправить проще, чем чужую новость в сюжете.

Каждая строка входа: id, издание, время (по Белграду), заголовок, что произошло (нейтральная фраза; бывает пустой).
Заголовок бывает кликбейтом — опирайся на смысл, а не на слова.

Ответ:
- events — сюжеты, в которых две новости и больше: title — короткое название в форме «кто что сделал» («Скупщина Воеводины приняла ребаланс бюджета»), ids — id новостей. Новость — максимум в одном сюжете.
- unclear — id новостей, по которым нельзя понять, о каком событии речь (кликбейт без факта, «ЭТО теперь запрещено!»).
- Новости-одиночки, о которых больше никто не написал, никуда не включай."""

SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "ids": {"type": "array", "items": {"type": "integer"}}},
                "required": ["title", "ids"],
                "additionalProperties": False,
            },
        },
        "unclear": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["events", "unclear"],
    "additionalProperties": False,
}


def local_time(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(LOCAL_TZ)


def day_items(archive: dict, day: str) -> list[dict]:
    """События дня в порядке появления: url и всё, что нужно для запроса и отчёта."""
    items = []
    for url, a in archive.items():
        if a["section"] in SKIP_SECTIONS or a.get("kind") not in ("event", None):
            continue
        seen = local_time(a["seen"])
        if seen.date().isoformat() != day:
            continue
        ru = a.get("ru") or {}
        items.append({"url": url, "source": a["source"], "section": a["section"], "time": seen.strftime("%H:%M"),
                      "title": ru.get("title") or a["title"], "what": a.get("what", ""), "lead": a.get("lead")})
    return sorted(items, key=lambda i: i["time"])


def label(snapshot: Path, day: str) -> Path:
    import anthropic

    items = day_items(json.loads(snapshot.read_text("utf-8")), day)
    if not items:
        sys.exit(f"в {snapshot.name} нет событий за {day}")
    lines = [f"{n}\t{i['source']}\t{i['time']}\t{i['title']}\t{i['what']}" for n, i in enumerate(items)]
    print(f"{len(items)} событий за {day}, запрос к {MODEL}…", file=sys.stderr)

    client = anthropic.Anthropic()
    with client.messages.stream(
        model=MODEL,
        max_tokens=128000,
        system=SYSTEM_PROMPT,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": "\n".join(lines)}],
    ) as stream:
        response = stream.get_final_message()
    usage = response.usage
    usd = (usage.input_tokens * PRICE_PER_MTOK[0] + usage.output_tokens * PRICE_PER_MTOK[1]) / 1e6
    print(f"токены: вход {usage.input_tokens}, выход {usage.output_tokens}, ≈ ${usd:.2f}", file=sys.stderr)
    if response.stop_reason != "end_turn":
        sys.exit(f"stop_reason={response.stop_reason}")
    result = json.loads(next(b.text for b in response.content if b.type == "text"))

    # модель могла положить новость в два сюжета или выдумать id: оставляем первое упоминание, чужое выкидываем
    taken, problems = set(), collections.Counter()
    events = []
    for event in result["events"]:
        ids = []
        for n in event["ids"]:
            if not 0 <= n < len(items):
                problems["несуществующий id"] += 1
            elif n in taken:
                problems["новость в двух сюжетах"] += 1
            else:
                taken.add(n)
                ids.append(n)
        if len(ids) >= 2:
            events.append({"title": event["title"], "urls": [items[n]["url"] for n in ids]})
        elif ids:
            problems["сюжет из одной новости"] += 1
            taken.discard(ids[0])
    unclear = [items[n]["url"] for n in dict.fromkeys(result["unclear"]) if 0 <= n < len(items) and n not in taken]
    for what, count in problems.items():
        print(f"поправлено: {what} — {count}", file=sys.stderr)

    out = snapshot.parent / f"gold-{day}.json"
    gold = {"day": day, "snapshot": snapshot.name, "model": MODEL, "effort": EFFORT,
            "labeled": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
            "usage": {"in": usage.input_tokens, "out": usage.output_tokens, "usd": round(usd, 4)},
            "events": events, "unclear": unclear, "items": items}
    out.write_text(json.dumps(gold, ensure_ascii=False, indent=1), "utf-8")
    print(f"→ {out}", file=sys.stderr)
    return out


def report(gold_path: Path) -> Path:
    gold = json.loads(gold_path.read_text("utf-8"))
    by_url = {i["url"]: i for i in gold["items"]}
    events = [[by_url[u] for u in e["urls"]] for e in gold["events"]]
    order = sorted(range(len(events)), key=lambda n: (-len({CAMPS.get(i["section"]) for i in events[n]}),
                                                       -len({i["source"] for i in events[n]}), -len(events[n])))
    in_events = sum(len(e) for e in events)
    big = [n for n in order if len({i["source"] for i in events[n]}) >= 3]
    e = html.escape

    def row(i: dict) -> str:
        lead = f' <span class="bad">{i["lead"]}</span>' if i.get("lead") in ("bad", "none", "title") else ""
        what = f'<div class="what">{e(i["what"])}</div>' if i["what"] and i["what"] != i["title"] else ""
        return (f'<li><span class="meta">{i["time"]} · {e(i["source"])} · {CAMPS.get(i["section"], i["section"])}</span>{lead}'
                f'<br><a href="{e(i["url"])}">{e(i["title"])}</a>{what}</li>')

    def story(n: int) -> str:
        its = events[n]
        camps = sorted({CAMPS.get(i["section"], i["section"]) for i in its})
        sources = {i["source"] for i in its}
        head = (f'<b>#{n}</b> {e(gold["events"][n]["title"])} <span class="meta">— {len(its)} новостей, '
                f'{len(sources)} изданий; {", ".join(camps)}</span>')
        body = "<ul>" + "".join(row(i) for i in its) + "</ul>"
        return f'<details{" open" if n in big[:15] else ""}><summary>{head}</summary>{body}</details>'

    rest = [n for n in order if n not in big]
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Эталон сюжетов {gold["day"]}</title>
<style>body{{font:15px/1.45 system-ui,sans-serif;max-width:900px;margin:24px auto;padding:0 16px;color:#222}}
summary{{cursor:pointer;padding:4px 0}}details{{border-bottom:1px solid #eee;padding:2px 0}}ul{{margin:4px 0 10px;padding-left:20px}}
li{{margin:5px 0}}a{{color:#1a4f8b;text-decoration:none}}.meta{{color:#888;font-size:13px}}.what{{color:#555;font-size:14px}}
.bad{{background:#fde8d8;color:#a33;font-size:12px;padding:0 4px;border-radius:3px}}h2{{margin-top:32px}}</style></head><body>
<h1>Эталон сюжетов за {gold["day"]}</h1>
<p>{gold["model"]}, effort {gold["effort"]}, ${gold["usage"]["usd"]}. Событий за день: {len(gold["items"])}.
В сюжетах: {in_events} в {len(events)} сюжетах ({len(big)} — из 3+ изданий). Непонятно: {len(gold["unclear"])}.
Одиночки: {len(gold["items"]) - in_events - len(gold["unclear"])}.</p>
<p>Номер сюжета (#) — для правок: «#12 убрать новость такую-то», «#5 и #9 — одно событие».</p>
<h2>Сюжеты из 3+ изданий — {len(big)}</h2>{"".join(story(n) for n in big)}
<h2>Остальные сюжеты — {len(rest)}</h2>{"".join(story(n) for n in rest)}
<h2>Непонятно — {len(gold["unclear"])}</h2><ul>{"".join(row(by_url[u]) for u in gold["unclear"])}</ul>
</body></html>"""
    out = gold_path.with_suffix(".html")
    out.write_text(page, "utf-8")
    print(f"→ {out}", file=sys.stderr)
    return out


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "label":
        report(label(Path(sys.argv[2]), sys.argv[3]))
    elif len(sys.argv) == 3 and sys.argv[1] == "report":
        report(Path(sys.argv[2]))
    else:
        sys.exit(__doc__)
