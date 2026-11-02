#!/usr/bin/env python3
"""Собирает RSS-ленты из sources.json и генерирует статичную страницу site/index.html.

По мотивам infomate.club (https://infomate.club, код: https://github.com/vas3k/infomate.club),
проекта Вастрик.Клуба (https://vas3k.club).

Сбор лент — только стандартная библиотека Python 3.10+. Перевод на русский (translate.py)
нужен пакет anthropic и ключ ANTHROPIC_API_KEY; без них сайт собирается без перевода.
Запуск: .venv/bin/python build.py  (или python3 build.py — тогда без перевода)
"""

import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from translate import SKELETON, WORDS_RE, attach_translations

ROOT = Path(__file__).parent
SOURCES = ROOT / "sources.json"
TEMPLATE = ROOT / "template.html"
OUT_DIR = ROOT / "site"
DATA_FILE = OUT_DIR / "data.json"

ITEMS_PER_SOURCE = 30
SUMMARY_CHARS = 420
TIMEOUT = 25
LOCAL_TZ = ZoneInfo("Europe/Belgrade")
# Reddit без авторизации отвечает примерно на один запрос в минуту, остальным отдаёт 429.
# Поэтому за сборку с такого хоста скачиваем столько лент, сколько указано, — те, что дольше всех
# не обновлялись, — а остальные берём из прошлой сборки. При сборке раз в 20 минут
# каждый из пяти сабреддитов обновляется примерно раз в полтора часа.
FEEDS_PER_BUILD = {"www.reddit.com": 1}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130 Safari/537.36"
)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


# ---------- parsing ----------

def local(tag: str) -> str:
    """'{namespace}title' -> 'title'"""
    return tag.rsplit("}", 1)[-1].lower()


def child_text(el, *names) -> str:
    for child in el:
        if local(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    return ""


# Alo.rs пишет даты по-сербски: «пон, 02 нов 2026 12:52:26 +0100»
SR_MONTHS = {
    "јан": "Jan", "феб": "Feb", "мар": "Mar", "апр": "Apr", "мај": "May", "јун": "Jun",
    "јул": "Jul", "авг": "Aug", "сеп": "Sep", "окт": "Oct", "нов": "Nov", "дец": "Dec",
    "maj": "May", "avg": "Aug", "okt": "Oct", "dec": "Dec",
}
SR_DAY_RE = re.compile(r"^[^\d,]+,\s*")


def parse_date(value: str):
    if not value:
        return None
    value = SR_DAY_RE.sub("", value.strip())
    parts = value.split(" ")
    if len(parts) > 1 and parts[1].lower() in SR_MONTHS:
        parts[1] = SR_MONTHS[parts[1].lower()]
        value = " ".join(parts)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)


TAG_RE = re.compile(r"<[^>]+>")
UNCLOSED_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*$")
SPACE_RE = re.compile(r"\s+")
# WordPress-хвосты вида «The post ... appeared first on ...» и «[…]», подпись Reddit «submitted by /u/... [comments]»
TAIL_RE = re.compile(r"(The post .*? appeared first on .*|\[(…|&hellip;|\.\.\.)\]|Opširnije.*|Pročitajte više.*|submitted by\s+/u/.*)$", re.I)


def clean_text(raw: str) -> str:
    if not raw:
        return ""
    # Ozon press экранирует HTML дважды («&amp;lt;img…»): снимаем экранирование, пока текст меняется
    text = raw
    while (unescaped := html.unescape(text)) != text:
        text = unescaped
    # и обрезает подводку посреди тега картинки: незакрытый хвост «<img width=… title="Tužil…» тоже убираем
    text = UNCLOSED_TAG_RE.sub(" ", TAG_RE.sub(" ", text))
    text = SPACE_RE.sub(" ", text).strip()
    return TAIL_RE.sub("", text).strip()


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:—-– ") + "…"


def parse_feed(data: bytes) -> list[dict]:
    # некоторые ленты отдают управляющие символы, которые ломают XML-парсер
    data = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", data)
    data = data.lstrip()  # Pančevac отдаёт пустые строки перед <?xml …?>
    root = ET.fromstring(data)
    items = []
    for el in root.iter():
        kind = local(el.tag)
        if kind not in ("item", "entry"):
            continue
        title = clean_text(child_text(el, "title"))
        link = ""
        for child in el:
            if local(child.tag) == "link":
                href = child.get("href")
                if href and child.get("rel", "alternate") == "alternate":
                    link = href
                    break
                if child.text and child.text.strip():
                    link = child.text.strip()
                    break
        if not link:
            guid = child_text(el, "guid", "id")
            if guid.startswith("http"):
                link = guid
        summary = clean_text(child_text(el, "description", "summary", "content", "encoded"))
        date = parse_date(child_text(el, "pubdate", "published", "updated", "date", "issued"))
        if not title or not link:
            continue
        if summary and (summary == title or title.startswith(summary)):
            summary = ""
        items.append({
            "title": title,
            "url": link,
            "summary": shorten(summary, SUMMARY_CHARS),
            "date": date.isoformat() if date else None,
        })
    return items


# Сербские сайты иногда публикуют английские материалы (N1 English и т.п.) —
# их нельзя переводить в кириллицу. Слова подобраны так, чтобы не совпадать с сербскими.
EN_WORDS = {"the", "of", "and", "is", "for", "with", "from", "at", "by", "after", "has", "have",
            "are", "was", "his", "her", "says", "said", "claims", "not", "will", "an", "its", "this",
            "that", "over", "into", "new", "what", "why", "how", "who"}
WORD_RE = re.compile(r"[a-zčćžšđ]+")


ENGLISH_SPELLING_RE = re.compile(r"[wxyq]|th|ee|oo|ea")


def looks_english(text: str) -> bool:
    text = text.lower()
    if re.search(r"[čćžšđ\u0400-\u04ff]", text):  # кириллица или сербская диакритика — точно не английский
        return False
    words = WORD_RE.findall(text)
    hits = sum(w in EN_WORDS or bool(ENGLISH_SPELLING_RE.search(w)) for w in words)
    return "the" in words or (hits >= 2 and hits >= 0.2 * len(words))


def tidy_google_news(items: list[dict]) -> list[dict]:
    """В Google News заголовок выглядит как «Заголовок - Издание», а description дублирует заголовок."""
    for item in items:
        title, sep, outlet = item["title"].rpartition(" - ")
        if sep and title:
            item["title"] = title
            item["via"] = outlet
        item["summary"] = ""
    # у материала без заголовка остаётся только « - Издание»
    return [item for item in items if not item["title"].startswith("- ")]


TRENDS_NS = "{https://trends.google.com/trending/rss}"
TRAFFIC_RE = re.compile(r"\D")


def parse_trends(data: bytes) -> list[dict]:
    """Google Trends: заголовок — поисковый запрос, ссылка и подводка — первая новость к нему.
    У всех запросов в ленте одна и та же <link>, поэтому parse_feed тут не годится."""
    items = []
    for el in ET.fromstring(data).iter("item"):
        news = el.find(TRENDS_NS + "news_item")
        query = child_text(el, "title")
        if news is None or not query:
            continue
        date = parse_date(child_text(el, "pubdate"))
        items.append({
            "title": query[:1].upper() + query[1:],
            "url": news.findtext(TRENDS_NS + "news_item_url", "").strip(),
            "summary": clean_text(news.findtext(TRENDS_NS + "news_item_title", "")),
            "date": date.isoformat() if date else None,
            "via": news.findtext(TRENDS_NS + "news_item_source", "").strip(),
            "traffic": el.findtext(TRENDS_NS + "approx_traffic", "").strip(),
        })
    # самые частые запросы сверху: «5000+» выше «1000+», при равенстве — как в ленте, свежие первыми
    items.sort(key=lambda i: int(TRAFFIC_RE.sub("", i["traffic"]) or 0), reverse=True)
    return items


# ---------- шапка: курс, погода, воздух ----------

# средний курс Народного банка Сербии (зеркало kurs.resenje.org), погода и качество воздуха — Open-Meteo; всё без ключей
RATE_URL = "https://kurs.resenje.org/api/v1/currencies/{}/rates/today"
CITY = (44.82, 20.46)  # Белград
WEATHER_URL = f"https://api.open-meteo.com/v1/forecast?latitude={CITY[0]}&longitude={CITY[1]}&current=temperature_2m,weather_code,is_day&timezone=Europe/Belgrade"
AIR_URL = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={CITY[0]}&longitude={CITY[1]}&current=european_aqi,pm2_5"


def header_info() -> dict:
    """Что не ответило, в шапке просто не показываем — сборку это не ломает."""
    def rates():
        eur, rub = (json.loads(fetch(RATE_URL.format(code))) for code in ("eur", "rub"))
        return {"EUR": eur["exchange_middle"], "RUB": rub["exchange_middle"], "date": eur["date"]}

    def weather():
        now = json.loads(fetch(WEATHER_URL))["current"]
        return {"temp": now["temperature_2m"], "code": now["weather_code"], "day": now["is_day"]}

    def air():
        now = json.loads(fetch(AIR_URL))["current"]
        return {"aqi": now["european_aqi"], "pm25": now["pm2_5"]}

    info = {}
    for key, job in (("rates", rates), ("weather", weather), ("air", air)):
        try:
            info[key] = job()
        except Exception as exc:  # noqa: BLE001
            print(f"Шапка: {key} не получено — {type(exc).__name__}: {exc}"[:200], file=sys.stderr)
    return info


# ---------- build ----------

def load_previous() -> dict:
    try:
        prev = json.loads(DATA_FILE.read_text("utf-8"))
        return {s["feed"]: s for s in prev.get("sources", [])}
    except (OSError, ValueError, KeyError):
        return {}


def postponed(jobs: list, previous: dict) -> set[str]:
    """Ленты с ограниченных хостов, которые в эту сборку не качаем (см. FEEDS_PER_BUILD)."""
    skip = set()
    for host, per_build in FEEDS_PER_BUILD.items():
        feeds = [src["feed"] for src, _ in jobs if urlparse(src["feed"]).hostname == host]
        feeds.sort(key=lambda feed: previous.get(feed, {}).get("fetched") or "")
        skip.update(feeds[per_build:])
    return skip


def collect(source: dict, section: dict, previous: dict, skip: set[str]) -> dict:
    result = {
        "name": source["name"],
        "site": source["site"],
        "feed": source["feed"],
        "note": source.get("note", ""),
        "note_ru": source.get("note_ru", ""),
        "lang": source.get("lang", "sr"),
        "section": section["id"],
        "items": [],
        "error": None,
    }
    prev = previous.get(source["feed"])
    if source["feed"] in skip:
        # очередь до этой ленты ещё не дошла: показываем прошлые новости без пометки об ошибке
        if prev:
            result["items"] = prev.get("items", [])
            result["fetched"] = prev.get("fetched")
        return result
    try:
        data = fetch(source["feed"])
        items = parse_trends(data) if "trends.google.com" in source["feed"] else parse_feed(data)
        # «exclude» в sources.json — куски адресов разделов, которые не нужны: шоу-бизнес, реалити, анонсы эфиров
        exclude = source.get("exclude", [])
        items = [i for i in items if not any(part in i["url"] for part in exclude)]
        if "news.google.com" in source["feed"]:
            items = tidy_google_news(items)
            if "site:" in source["feed"]:
                # лента одного сайта через Google News (у 021.rs, RTV и др. своей RSS нет): издание и так в заголовке
                # карточки, а заголовки из одного-двух слов — это страницы рубрик («Srem», «Kategorija Hronika»)
                items = [i for i in items if len(i["title"].split()) >= 3]
                for item in items:
                    item.pop("via", None)
        if not items:
            raise ValueError("пустая лента")
        if result["lang"] == "sr":
            for item in items:
                if looks_english(item["title"]):
                    item["lang"] = "en"
        # свежие сверху; материалы без даты в конце. «ranked» — лента уже упорядочена по популярности (топ Reddit, Google Trends)
        if not source.get("ranked"):
            items.sort(key=lambda i: i["date"] or "", reverse=True)
        # RTV и другие публикуют одну новость дважды, латиницей и кириллицей, — оставляем первую
        seen, unique = set(), []
        for item in items:
            keys = {item["url"], " ".join(WORDS_RE.findall(item["title"].lower())).translate(SKELETON)}
            if not keys & seen:
                seen |= keys
                unique.append(item)
        result["items"] = unique[:ITEMS_PER_SOURCE]
        result["fetched"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001 — одна сломанная лента не должна ронять всю сборку
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        if prev:
            result["items"] = prev.get("items", [])
            result["fetched"] = prev.get("fetched")
            result["stale"] = True
    return result


def main() -> int:
    config = json.loads(SOURCES.read_text("utf-8"))
    previous = load_previous()
    jobs = [(src, sec) for sec in config["sections"] for src in sec["sources"]]
    skip = postponed(jobs, previous)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda job: collect(job[0], job[1], previous, skip), jobs))

    for r in results:
        status = f"ОШИБКА {r['error']}" if r["error"] else "в очереди" if r["feed"] in skip else "ok"
        print(f"{len(r['items']):3d}  {r['name']:<24} {status}", file=sys.stderr)

    attach_translations(results)

    data = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sections": [{k: s.get(k, "") for k in ("id", "title", "note", "title_ru", "note_ru")} for s in config["sections"]],
        "sources": results,
        "info": header_info(),
    }

    OUT_DIR.mkdir(exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False)
    DATA_FILE.write_text(payload, "utf-8")
    page = TEMPLATE.read_text("utf-8").replace(
        "/*__DATA__*/null", payload.replace("</", "<\\/")
    )
    (OUT_DIR / "index.html").write_text(page, "utf-8")

    ok = sum(1 for r in results if not r["error"] and r["feed"] not in skip)
    print(f"\nГотово: {ok}/{len(results) - len(skip)} лент, {OUT_DIR / 'index.html'}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
