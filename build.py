#!/usr/bin/env python3
"""Собирает RSS-ленты из sources.json и генерирует статичную страницу site/index.html.

По мотивам infomate.club (https://infomate.club, код: https://github.com/vas3k/infomate.club),
проекта Вастрик.Клуба (https://vas3k.club).

Сбор лент — только стандартная библиотека Python 3.10+. Перевод на русский (translate.py)
нужен пакет anthropic и ключ ANTHROPIC_API_KEY; без них сайт собирается без перевода.
Запуск: .venv/bin/python build.py  (или python3 build.py — тогда без перевода)
"""

import collections
import html
import json
import re
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from translate import SKELETON, WORDS_RE, attach_translations, reports_api

ROOT = Path(__file__).parent
SOURCES = ROOT / "sources.json"
TEMPLATE = ROOT / "template.html"
OUT_DIR = ROOT / "site"
DATA_FILE = OUT_DIR / "data.json"
STATIC_DIR = ROOT / "static"  # картинка-превью, robots.txt, файлы подтверждения для поисковиков
ICONS_DIR = STATIC_DIR / "icons"  # иконки изданий, скачивает icons.py
REGIONS_FILE = ROOT / "regions.json"  # контуры областей для карточек региональных групп, пишет regions.py
# все новости за последние дни — для будущей ежедневной сводки: в data.json у быстрых изданий только последние
# 30 новостей, это несколько часов. Хранится в кэше GitHub Actions вместе с кэшем переводов, в git не попадает
ARCHIVE_FILE = ROOT / "cache" / "archive.json"
# 4 суток, а не 3: снимок раз в 3 дня (snapshot.py) когда бы ни сняли, захватывает 3 предыдущих календарных дня
# целиком, а соседние снимки перекрываются на сутки — опоздание запуска не оставляет дыр
ARCHIVE_DAYS = 4
# только для архива (сюжеты, разбор): в data.json не кладём — он встроен в страницу, и каждый килобайт читатель качает
ARCHIVE_ONLY = ("categories", "author", "what", "entities", "place", "topic")
# пометки срочного в заголовке: HITNO, UŽIVO, BREAKING — в архиве поле «marker», для Live
MARKER_RE = re.compile(r"(?<!\w)(HITNO|ХИТНО|UŽIVO|UZIVO|УЖИВО|BREAKING)(?!\w)", re.IGNORECASE)

SITE_URL = "https://vestomat.rs"
CARD_LIMIT = 8  # сколько новостей издания видно сразу — как в template.html
# у каждого языка свой адрес: так поисковики находят и сербскую, и русскую версию
PAGES = {
    "sr": {
        "path": "",
        "html_lang": "sr-Latn",
        "locale": "sr_RS",
        "h1": "Srbija: vesti",
        "title": "Srbija: vesti iz svih medija — Vestomat",
        "description": "Vesti iz Srbije na jednom mestu: mediji kritični prema SNS-u i bliski SNS-u, državni i regionalni "
                       "mediji, strani mediji o Srbiji. Naslovi svih izvora, ažurirano tokom celog dana.",
        "og_image": "og.png",  # картинки-превью рисует og.py
    },
    "ru": {
        "path": "ru/",
        "html_lang": "ru",
        "locale": "ru_RU",
        "h1": "Сербия: новости",
        "title": "Новости Сербии на русском — Vestomat",
        "description": "Новости Сербии на русском языке в одном месте: СМИ, критичные к SNS и близкие к нему, "
                       "государственные и региональные медиа, зарубежная пресса о Сербии. Заголовки переводит ИИ, "
                       "лента обновляется в течение всего дня.",
        "og_image": "og-ru.png",
    },
}

ITEMS_PER_SOURCE = 30
# самая свежая новость ленты старше, чем в прошлой сборке, на столько — значит, сайт отдал старую ленту из кэша
STALE_FEED = timedelta(hours=3)
# сторож лент: лента не открывается или молчит дольше суток — пишем в Telegram (через worker/). Медленным изданиям
# (CINS, BIRN — раз в несколько дней) сутки тишины — норма: им ждём SILENT_GAPS обычных промежутков между новостями
SILENT_AFTER = timedelta(days=1)
SILENT_GAPS = 4
# часть изданий (Cenzolovka, Mašina, Istinomer) по выходным и праздникам не пишет. Какие — сторож узнаёт сам:
# у каждой ленты в data.json копятся дни, когда у неё были новости («days», за DAYS_KEPT дней). Не писала
# в большинство прошедших выходных — её выходные в тишину не засчитываем
DAYS_KEPT = 35
# государственные праздники Сербии (нерабочие дни) с постоянной датой; православная Пасха — в days_off
HOLIDAYS = {(1, 1), (1, 2), (1, 7), (2, 15), (2, 16), (5, 1), (5, 2), (11, 11)}
# если такой праздник выпал на воскресенье, выходной переносится на понедельник (кроме Рождества)
HOLIDAYS_MOVED = HOLIDAYS - {(1, 7)}
SUMMARY_CHARS = 420
# подводка короче — это рубрика вроде «Iza Ekrana 137» у Radar, а не текст
SUMMARY_MIN = 20
# длиннее и без точки в конце — значит, лента обрезала текст: N1 и Nova режут на 150 знаках посреди слова,
# WordPress — на 55 словах посреди фразы
SUMMARY_CUT = 140
SENTENCE_END = tuple('.!?…"»“”\')')
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


def feed_author(text: str) -> str:
    """<dc:creator> или <author>: «redakcija@site.rs (Ime Prezime)» → «Ime Prezime», голый адрес почты — пусто."""
    text = clean_text(text)
    m = re.search(r"\(([^()]+)\)\s*$", text)
    if m:
        text = m.group(1).strip()
    return "" if "@" in text else text[:100]


def child_text(el, *names) -> str:
    for child in el:
        # у Владе в description неэкранированный HTML (<p>…</p>): текст лежит во вложенных элементах
        text = "".join(child.itertext()).strip()
        if local(child.tag) in names and text:
            return text
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
# WordPress-хвосты вида «The post ... appeared first on ...» (или «first appeared on»), «[…]», «[...] Source», «… Read more →»,
# подпись Reddit «submitted by /u/... [comments]»; их бывает несколько подряд, поэтому снимаем по одному
TAIL_RE = re.compile(r"(The post .*? (appeared first|first appeared) on .*|\[(…|&hellip;|\.\.\.)\]|(?<=\])\s*Source|Read more\s*→?|Continue reading.*"
                     r"|Opširnije.*|Pročitajte više.*|submitted by\s+/u/.*)$", re.I)


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
    while (stripped := TAIL_RE.sub("", text).strip()) != text:
        text = stripped
    return text


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:—-– ") + "…"


def tidy_summary(text: str) -> str:
    """Подводка без рубрик и обрывков: обрезанную лентой — по последнее целое слово и с многоточием."""
    if len(text) < SUMMARY_MIN:
        return ""
    if len(text) > SUMMARY_CHARS:
        return shorten(text, SUMMARY_CHARS)
    # смайлик в конце (Reddit) — не обрыв, просто так пишут
    if len(text) >= SUMMARY_CUT and not text.endswith(SENTENCE_END) and (text[-1].isalnum() or text[-1] in ",;:—-–"):
        return text.rsplit(" ", 1)[0].rstrip(",.;:—-– ") + "…"
    return text


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
            "summary": tidy_summary(summary),
            "date": date.isoformat() if date else None,
            # рубрики и автор — для «exclude_category» и архива (по ним видно колонки и агентские тексты),
            # в data.json не попадают (см. ARCHIVE_ONLY)
            "categories": [c for c in (clean_text(c.text or c.get("term") or "") for c in el if local(c.tag) == "category") if c],
            "author": feed_author(child_text(el, "creator", "author")),
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


class PageLinks(HTMLParser):
    """Ссылки страницы: текст ссылки (или title/alt картинки в ней) → адрес."""

    def __init__(self):
        super().__init__()
        self.links, self.href, self.text = [], None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.href, self.text = attrs.get("href"), []
        elif tag == "img" and self.href:
            self.text.append(attrs.get("title") or attrs.get("alt") or "")

    def handle_data(self, data):
        if self.href:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((" ".join(self.text), self.href))
            self.href = None


def title_key(title: str) -> str:
    return " ".join(WORDS_RE.findall(title.lower()))


def real_links(page: str, site: str) -> dict[str, str]:
    """Заголовок → адрес материала по странице сайта со списком свежих новостей («links» в sources.json —
    такие страницы по порядку, от свежих к старым).
    Google News иногда ведёт не на материал, а на его копию с тем же заголовком: у 021.rs — на страницу
    комментариев. Берём только ссылки на сам сайт и без якоря (#commlist — это ссылка на комментарии)."""
    parser = PageLinks()
    parser.feed(fetch(page).decode("utf-8", "replace"))
    host = urlparse(site).netloc
    links = [(text, urljoin(page, href)) for text, href in parser.links]  # у RTV ссылки относительные: /sr_lat/…
    return {title_key(text): href for text, href in links
            if urlparse(href).netloc == host and href.startswith("https://") and "#" not in href and title_key(text)}


# Страницы RTV, кроме статей, Google называет по <title> («Srem - JMU Radio-televizija Vojvodine»)
# или по шапке рубрики («Vojvodina | Novi Sad», «Sport - sve vesti»)
SECTION_PAGE_RE = re.compile(r"^(Kategorija|Rubrika|Autor|Tag|Arhiva)\b|\d{1,2}\.\d{1,2}\.\d{4}\.?$|^[\w ]{1,20} vesti$"
                             r"|^[\w ]{1,20} \| [\w ]{1,20}$|\bsve vesti$|Radio-televizija Vojvodine$", re.I)

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


def parse_wp_json(data: bytes) -> list[dict]:
    """Сайт на WordPress с выключенной RSS (BIRN Srbija): те же записи отдаёт его API /wp-json/wp/v2/posts."""
    items = []
    for post in json.loads(data):
        title = clean_text(post.get("title", {}).get("rendered", ""))
        summary = clean_text(post.get("excerpt", {}).get("rendered", ""))
        date = parse_date((post.get("date_gmt") or "") + "+00:00") if post.get("date_gmt") else None
        if not title or not post.get("link"):
            continue
        items.append({
            "title": title,
            "url": post["link"],
            "summary": tidy_summary("" if summary == title else summary),
            "date": date.isoformat() if date else None,
            "categories": [],
        })
    return items


# ---------- шапка: курс, погода, воздух ----------

# средний курс Народного банка Сербии (зеркало kurs.resenje.org), погода и качество воздуха — Open-Meteo; всё без ключей.
# Open-Meteo — расчёт модели: обложной дождь он, бывает, показывает как «пасмурно». Поэтому дождь, снег, туман и облачность
# берём из наблюдений метеостанции аэропорта (METAR, обновляется раз в полчаса), а модель — запасной вариант
RATE_URL = "https://kurs.resenje.org/api/v1/currencies/{}/rates/today"
CITY = (44.82, 20.46)  # Белград
WEATHER_URL = f"https://api.open-meteo.com/v1/forecast?latitude={CITY[0]}&longitude={CITY[1]}&current=temperature_2m,weather_code,is_day&timezone=Europe/Belgrade"
AIR_URL = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={CITY[0]}&longitude={CITY[1]}&current=european_aqi,pm2_5"
METAR_URL = "https://aviationweather.gov/api/data/metar?ids=LYBE&format=json"  # аэропорт Никола Тесла
METAR_MAX_AGE = 2 * 3600


def observed_code() -> int | None:
    """Погода по наблюдениям аэропорта в кодах WMO, как у Open-Meteo; None — сводки нет или она устарела."""
    reports = json.loads(fetch(METAR_URL))
    if not reports or datetime.now(timezone.utc).timestamp() - reports[0]["obsTime"] > METAR_MAX_AGE:
        return None
    # явления «поблизости» (VCSH, VCTS) — не над станцией, их пропускаем
    weather = " ".join(w for w in (reports[0].get("wxString") or "").split() if not w.startswith("VC"))
    for codes, wmo in (("TS", 95), ("SN SG", 71), ("RA DZ PL GR GS", 61), ("FG", 45)):
        if any(code in weather for code in codes.split()):
            return wmo
    return {"OVC": 3, "OVX": 3, "BKN": 3, "SCT": 2, "FEW": 1}.get(reports[0].get("cover"), 0)


def header_info() -> dict:
    """Что не ответило, в шапке просто не показываем — сборку это не ломает."""
    def rates():
        eur, rub = (json.loads(fetch(RATE_URL.format(code))) for code in ("eur", "rub"))
        return {"EUR": eur["exchange_middle"], "RUB": rub["exchange_middle"], "date": eur["date"]}

    def weather():
        now = json.loads(fetch(WEATHER_URL))["current"]
        try:
            code = observed_code()
        except Exception as exc:  # noqa: BLE001 — тогда по модели
            print(f"Шапка: METAR не получен — {type(exc).__name__}: {exc}"[:200], file=sys.stderr)
            code = None
        return {"temp": now["temperature_2m"], "code": now["weather_code"] if code is None else code, "day": now["is_day"]}

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


def update_archive(results: list[dict], fresh: set[str]) -> dict:
    """Дописывает новости сборки в архив и убирает старше ARCHIVE_DAYS. Возвращает архив.
    seen — когда сборка впервые увидела новость: у части лент нет дат или они врут, а сводке нужно окно по времени.
    last_seen — когда новость в последний раз была в ленте (fresh — ленты, скачанные в эту сборку): пропала раньше,
    чем её вытеснили бы новые, — возможно, удалили. titles — прежние заголовки, если издание его меняло:
    [{"title", "until"}]. Остальное — оценки модели при переводе (см. translate.attach_translations) и поля ленты."""
    try:
        archive = json.loads(ARCHIVE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        archive = {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for src in results:
        for item in src["items"]:
            old = archive.get(item["url"], {})
            entry = {
                "title": item["title"], "summary": item.get("summary", ""), "ru": item.get("ru") or old.get("ru"),
                "lead": item.get("lead") or old.get("lead"), "kind": item.get("kind") or old.get("kind"),
                "date": item.get("date"), "seen": old.get("seen") or now,
                "last_seen": now if src["feed"] in fresh else old.get("last_seen") or now,
                "source": src["name"], "section": src["section"], "lang": item.get("lang", src["lang"]),
            }
            for field in ARCHIVE_ONLY:
                if value := item.get(field) or old.get(field):
                    entry[field] = value
            # издание снимает «HITNO» через час-другой, а для Live важно, что новость была срочной, — метку не теряем
            if m := MARKER_RE.search(item["title"]):
                entry["marker"] = m.group(1).upper().replace("UZIVO", "UŽIVO").replace("ХИТНО", "HITNO").replace("УЖИВО", "UŽIVO")
            elif old.get("marker"):
                entry["marker"] = old["marker"]
            titles = old.get("titles", [])
            # один адрес бывает в двух лентах с разными заголовками — правкой считаем только смену в той же ленте
            if old.get("source") == src["name"] and old["title"] != item["title"]:
                titles = [*titles, {"title": old["title"], "until": now}]
            if titles:
                entry["titles"] = titles
            archive[item["url"]] = entry
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ARCHIVE_DAYS)).isoformat()
    archive = {url: a for url, a in archive.items() if max(a["seen"], a["date"] or "") >= cutoff}
    ARCHIVE_FILE.parent.mkdir(exist_ok=True)
    ARCHIVE_FILE.write_text(json.dumps(archive, ensure_ascii=False), "utf-8")
    return archive


def orthodox_easter(year: int) -> date:
    """Православная Пасха по григорианскому календарю (алгоритм Меёса для юлианского + 13 дней в 1900–2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return date(year, month, day + 1) + timedelta(days=13)


def days_off(year: int) -> set[date]:
    """Нерабочие дни Сербии, кроме суббот и воскресений: праздники с переносом и православная Пасха (пт—пн)."""
    easter = orthodox_easter(year)
    off = {date(year, m, d) for m, d in HOLIDAYS} | {easter + timedelta(days=k) for k in (-2, -1, 0, 1)}
    # праздник в воскресенье — выходным становится ближайший рабочий день после него (1 января в воскресенье:
    # 2 января и так праздник, выходной — 3-го)
    for holiday in sorted(off):
        if holiday.weekday() == 6 and (holiday.month, holiday.day) in HOLIDAYS_MOVED:
            moved = holiday + timedelta(days=1)
            while moved in off or moved.weekday() >= 5:
                moved += timedelta(days=1)
            off.add(moved)
    return off


def is_day_off(day: date) -> bool:
    """Суббота, воскресенье или праздник."""
    return day.weekday() >= 5 or day in days_off(day.year)


def track_days(results: list[dict], previous: dict, archive: dict) -> None:
    """Дни (по Белграду), когда у ленты были новости, — в "days"; "tracked" — с какого дня следим.
    Берём из архива: в нём все новости за ARCHIVE_DAYS, а в ленте у быстрых изданий только последние часы."""
    seen = collections.defaultdict(set)
    first = {}
    for a in archive.values():
        day = datetime.fromisoformat(a["seen"]).astimezone(LOCAL_TZ).date().isoformat()
        first[a["source"]] = min(first.get(a["source"], day), day)
        seen[a["source"]].add(datetime.fromisoformat(a["date"] or a["seen"]).astimezone(LOCAL_TZ).date().isoformat())
    cutoff = (datetime.now(LOCAL_TZ).date() - timedelta(days=DAYS_KEPT)).isoformat()
    for r in results:
        prev = previous.get(r["feed"], {})
        r["tracked"] = prev.get("tracked") or first.get(r["name"])
        if not r["tracked"]:
            continue
        r["days"] = sorted(d for d in set(prev.get("days", [])) | seen[r["name"]] if d >= max(cutoff, r["tracked"]))


def rests_on_days_off(r: dict, today: date) -> bool:
    """Издание обычно не пишет в выходные и праздники: в большинство прошедших нерабочих дней новостей не было."""
    if not r.get("tracked"):
        return False
    start = max(date.fromisoformat(r["tracked"]), today - timedelta(days=DAYS_KEPT))
    off = [start + timedelta(days=k) for k in range((today - start).days) if is_day_off(start + timedelta(days=k))]
    worked = sum(1 for d in off if d.isoformat() in set(r.get("days", [])))
    return bool(off) and worked * 2 < len(off)


def working_time(start: datetime, end: datetime) -> timedelta:
    """Сколько прошло от start до end без суббот, воскресений и праздников (по Белграду)."""
    total = timedelta()
    t = start.astimezone(LOCAL_TZ)
    end = end.astimezone(LOCAL_TZ)
    while t < end:
        midnight = datetime.combine(t.date() + timedelta(days=1), datetime.min.time(), LOCAL_TZ)
        if not is_day_off(t.date()):
            total += min(midnight, end) - t
        t = midnight
    return total


def watch_feeds(jobs: list, results: list[dict]) -> tuple[list[dict], list[str]]:
    """Сторож лент: какие ленты не открываются (broken) или молчат (silent) дольше SILENT_AFTER.
    Второе в ответе — ленты, которые молчат, но у них выходной (rests_on_days_off): worker скажет «отбой», а не «ожила»."""
    now = datetime.now(timezone.utc)
    today = now.astimezone(LOCAL_TZ).date()
    problems, resting = [], []
    for (source, _), r in zip(jobs, results):
        base = {"name": r["name"], "site": r["site"], "feed": r["feed"]}
        # пока лента падает, fetched остаётся временем последнего удачного скачивания
        if r["error"] and r.get("fetched") and now - datetime.fromisoformat(r["fetched"]) > SILENT_AFTER:
            problems.append({**base, "kind": "broken", "since": r["fetched"], "detail": r["error"]})
            continue
        if source.get("ranked"):  # в топе Reddit и Google Trends свежего может не быть и у живой ленты
            continue
        dated = sorted((i for i in r["items"] if i.get("date")), key=lambda i: i["date"], reverse=True)
        if not dated:
            continue
        newest = datetime.fromisoformat(dated[0]["date"])
        gap = (newest - datetime.fromisoformat(dated[-1]["date"])) / max(len(dated) - 1, 1)
        limit = max(SILENT_AFTER, SILENT_GAPS * gap)
        if now - newest <= limit:
            continue
        if rests_on_days_off(r, today) and working_time(newest, now) <= limit:
            resting.append(r["feed"])
            continue
        problems.append({**base, "kind": "silent", "since": dated[0]["date"], "detail": dated[0]["title"],
                         "gap_hours": round(gap / timedelta(hours=1), 1)})
    return problems, resting


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
            for key in ("unmatched", "excluded"):
                if key in prev:
                    result[key] = prev[key]
        return result
    try:
        url = source["feed"]
        if source.get("nocache"):
            # Pink, Insajder, Pančevac, Espreso отдают ленту из кэша на час-два старше; с параметром в адресе — свежую
            url += ("&" if "?" in url else "?") + f"_={int(datetime.now().timestamp())}"
        data = fetch(url)
        if "trends.google.com" in source["feed"]:
            items = parse_trends(data)
        elif "/wp-json/" in source["feed"]:
            items = parse_wp_json(data)
        else:
            items = parse_feed(data)
        # «more_feeds» — ещё ленты того же издания: у Telegraf новости и политика (поддомен politika.) идут разными лентами
        for feed in source.get("more_feeds", []):
            try:
                items += parse_feed(fetch(feed))
            except Exception as exc:
                print(f"  {source['name']}: лента {feed} не открылась: {type(exc).__name__}: {exc}"[:200], file=sys.stderr)
        # «exclude» в sources.json — куски адресов разделов, которые не нужны: шоу-бизнес, реалити, анонсы эфиров
        exclude = source.get("exclude", [])
        items = [i for i in items if not any(part in i["url"] for part in exclude)]
        # «exclude_category» — то же по рубрикам ленты (<category>), у кого рубрики нет в адресе: Beta, Serbian Times
        skip = {c.lower() for c in source.get("exclude_category", [])}
        items = [i for i in items if not skip & {c.lower() for c in i.get("categories", [])}]
        # ссылка из чужой ленты попадает в href: «javascript:…» выполнился бы на нашей странице по клику
        items = [i for i in items if i["url"].startswith(("https://", "http://"))]
        if "news.google.com" in source["feed"]:
            items = tidy_google_news(items)
            if "site:" in source["feed"]:
                # лента одного сайта через Google News (у 021.rs, RTV и др. своей RSS нет): издание и так в заголовке
                # карточки, а заголовки из одного-двух слов — это страницы рубрик («Srem», «Kategorija Hronika»),
                # как и страницы авторов, архивы по дням и «Novi Sad vesti»
                items = [i for i in items if len(i["title"].split()) >= 3 and not SECTION_PAGE_RE.search(i["title"])]
                for item in items:
                    item.pop("via", None)
        if not items:
            raise ValueError("пустая лента")
        if result["lang"] == "sr":
            for item in items:
                if looks_english(item["title"]):
                    item["lang"] = "en"
        # у Владе в ленте нет дат: ставим время, когда сборка впервые увидела новость.
        # В первой сборке источника новое от старого не отличить — те остаются без даты
        prev_dates = {i["url"]: i.get("date") for i in (prev or {}).get("items", [])}
        for item in items:
            if not item["date"]:
                item["date"] = prev_dates.get(item["url"], datetime.now(timezone.utc).isoformat(timespec="seconds") if prev else None)
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
        if source.get("links"):
            # адреса, найденные прошлой сборкой, берём из её данных, а новые новости ищем по страницам списка
            # (свежие — на первой), пока не найдём все: обычно хватает одной страницы.
            # Чего не нашли на всех страницах (unmatched), не ищем снова — так и останется ссылкой Google News
            found = {title_key(i["title"]): i["url"] for i in (prev or {}).get("items", []) if "news.google.com" not in i["url"]}
            tried = set((prev or {}).get("unmatched", []))
            # выкинутые по «exclude» уже после поиска (у ссылки Google News раздела не видно) — заново не ищем
            dropped = {title_key(i["title"]) for i in result["items"]} & set((prev or {}).get("excluded", []))
            result["items"] = [i for i in result["items"] if title_key(i["title"]) not in dropped]
            for item in result["items"]:
                item["url"] = found.get(title_key(item["title"]), item["url"])
            searched = True
            for page in source["links"]:
                left = [i for i in result["items"] if "news.google.com" in i["url"] and title_key(i["title"]) not in tried]
                if not left:
                    break
                try:
                    real = real_links(page, source["site"])
                except Exception as exc:  # noqa: BLE001 — без настоящих адресов остаются ссылки Google News
                    print(f"  {source['name']}: страница со ссылками не открылась: {type(exc).__name__}: {exc}"[:200], file=sys.stderr)
                    searched = False  # не открылась — поищем в следующую сборку
                    break
                for item in left:
                    item["url"] = real.get(title_key(item["title"]), item["url"])
            result["unmatched"] = [title_key(i["title"]) for i in result["items"] if "news.google.com" in i["url"]
                                   and (searched or title_key(i["title"]) in tried)]
            dropped |= {title_key(i["title"]) for i in result["items"] if any(part in i["url"] for part in exclude)}
            result["items"] = [i for i in result["items"] if title_key(i["title"]) not in dropped]
            result["excluded"] = sorted(dropped)
        # лента откатилась назад: Insajder однажды отдал из кэша вчерашнюю — тогда оставляем прошлые новости
        # (в топах Reddit и Google Trends свежесть скачет сама по себе)
        newest = max((i["date"] for i in result["items"] if i["date"]), default=None)
        before = max((i["date"] for i in (prev or {}).get("items", []) if i.get("date")), default=None)
        if (not source.get("ranked") and newest and before
                and datetime.fromisoformat(newest) < datetime.fromisoformat(before) - STALE_FEED):
            raise ValueError(f"лента старее прошлой: самая свежая новость {newest[:16]}, в прошлой сборке {before[:16]}")
        result["fetched"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001 — одна сломанная лента не должна ронять всю сборку
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        if prev:
            result["items"] = prev.get("items", [])
            result["fetched"] = prev.get("fetched")
            result["stale"] = True
            for key in ("unmatched", "excluded"):
                if key in prev:
                    result[key] = prev[key]
    return result


def section_meta(config: dict) -> list[dict]:
    meta = []
    for sec in config["sections"]:
        entry = {k: sec.get(k, "") for k in ("id", "title", "note", "title_ru", "note_ru")}
        if "groups" in sec:
            # «groups» — несколько изданий в одной общей карточке (региональные: карточка на регион)
            names = {src["name"] for src in sec["sources"]}
            unknown = [name for group in sec["groups"] for name in group["sources"] if name not in names]
            if unknown:
                raise ValueError(f"в groups раздела {sec['id']} нет таких изданий: {unknown}")
            entry["groups"] = sec["groups"]
            # вместо буквы у карточки группы — мини-карта с её областью (regions.json, см. regions.py)
            if REGIONS_FILE.exists():
                maps = json.loads(REGIONS_FILE.read_text("utf-8"))
                entry["map"] = {"viewBox": maps["viewBox"], "country": maps["country"]}
                entry["groups"] = [{**group, "map": maps["regions"].get(group["title"])} for group in sec["groups"]]
        meta.append(entry)
    return meta


def cards_of(sec: dict, sources: list[dict]) -> list[tuple[str | dict, str | None, list[dict]]]:
    """Карточки раздела как (издание или группа, сайт, новости): издание — своя карточка, группа — одна общая,
    новости в ней вперемешку по времени. Карточка группы стоит там, где первое её издание."""
    group_of = {name: group for group in sec.get("groups", []) for name in group["sources"]}
    cards, done = [], set()
    for src in sources:
        group = group_of.get(src["name"])
        if not group:
            cards.append((src["name"], src["site"], src["items"]))
        elif group["title"] not in done:
            done.add(group["title"])
            items = [i for s in sources if s["name"] in group["sources"] for i in s["items"]]
            items.sort(key=lambda i: i["date"] or "", reverse=True)
            cards.append((group, None, items))
    return cards


def head_tags(lang: str) -> str:
    """<title>, описание, превью для соцсетей и мессенджеров, ссылки между языковыми версиями."""
    page = PAGES[lang]
    url = SITE_URL + "/" + page["path"]
    e = lambda text: html.escape(text, quote=True)
    tags = [
        f"<title>{e(page['title'])}</title>",
        f'<meta name="description" content="{e(page["description"])}">',
        f'<link rel="canonical" href="{url}">',
        *(f'<link rel="alternate" hreflang="{l}" href="{SITE_URL}/{p["path"]}">' for l, p in PAGES.items()),
        f'<link rel="alternate" hreflang="x-default" href="{SITE_URL}/">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="Vestomat">',
        f'<meta property="og:title" content="{e(page["title"])}">',
        f'<meta property="og:description" content="{e(page["description"])}">',
        f'<meta property="og:url" content="{url}">',
        f'<meta property="og:locale" content="{page["locale"]}">',
        f'<meta property="og:image" content="{SITE_URL}/{page["og_image"]}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta name="twitter:card" content="summary_large_image">',
        '<script type="application/ld+json">' + json.dumps({
            "@context": "https://schema.org", "@type": "WebSite", "name": "Vestomat",
            "url": url, "inLanguage": lang, "description": page["description"],
        }, ensure_ascii=False) + "</script>",
    ]
    return "\n".join(tags)


def prerender(data: dict, lang: str) -> str:
    """Те же разделы и колонки, что рисует скрипт, но готовым HTML: заголовки видят и роботы без JS.
    Разметка повторяет template.html, чтобы до запуска скрипта страница выглядела так же."""
    e = html.escape
    ru = lang == "ru"
    out = []
    for sec in data["sections"]:
        cards = []
        for name, site, items in cards_of(sec, [s for s in data["sources"] if s["section"] == sec["id"] and s["items"]]):
            if isinstance(name, dict):  # группа изданий
                name = (name.get("title_ru") if ru else None) or name["title"]
            rows = []
            for item in items[:CARD_LIMIT]:
                title = item["ru"]["title"] if ru and item.get("ru") else item["title"]
                rows.append(f'<li class="item"><div class="row"><span class="time"></span>'
                            f'<a class="head" href="{e(item["url"])}" target="_blank" rel="noopener">{e(title)}</a></div></li>')
            heading = (f'<a class="card-name" href="{e(site)}" target="_blank" rel="noopener">{e(name)}</a>' if site
                       else f'<span class="card-name">{e(name)}</span>')
            cards.append(
                f'<article class="card"><div class="card-head">'
                f'<span class="avatar" style="background:var(--c-{sec["id"]})">{e(name[:1])}</span>'
                f'<div>{heading}</div></div>'
                f'<ul class="items">{"".join(rows)}</ul></article>'
            )
        if not cards:
            continue
        title = (sec.get("title_ru") if ru else None) or sec["title"]
        note = (sec.get("note_ru") if ru else None) or sec.get("note", "")
        out.append(
            f'<section class="section" data-id="{e(sec["id"])}"><div class="section-head">'
            f'<h2><span class="dot" style="background:var(--c-{sec["id"]})"></span>{e(title)}</h2><p>{e(note)}</p></div>'
            f'<div class="grid">{"".join(cards)}</div></section>'
        )
    return "\n".join(out)


def strip_comments(template: str) -> str:
    """Комментарии в шаблоне — для разработки, на сайт их не выкладываем; метки вида __HEAD__ оставляем для подстановки."""
    keep_marks = lambda m: m.group(0) if re.search(r"__[A-Z_]+__", m.group(0)) else ""
    for comment in (r"<!--(?:(?!-->).)*-->", r"/\*(?:(?!\*/).)*\*/"):  # HTML; CSS и JS
        template = re.sub(rf"^[ \t]*{comment}[ \t]*\n", keep_marks, template, flags=re.S | re.M)  # строка целиком
        template = re.sub(rf"[ \t]*{comment}", keep_marks, template, flags=re.S)
    template = re.sub(r"^[ \t]*//.*\n", "", template, flags=re.M)  # JS: строка целиком
    return re.sub(r"[ \t]+// .*$", "", template, flags=re.M)  # JS: в конце строки (у адресов перед // нет пробела)


def add_icons(data: dict) -> None:
    """"icon" — своя иконка издания из static/icons; у кого её нет, страница берёт favicon у Google."""
    for src in data["sources"]:
        host = urlparse(src["site"]).hostname
        src["icon"] = next((f"/icons/{p.name}" for p in sorted(ICONS_DIR.glob(f"{host}.*"))), None)


def write_pages(data: dict) -> None:
    """index.html и ru/index.html, sitemap.xml и статика."""
    # дни ленты (track_days) нужны только сторожу: в страницу их не встраиваем
    page = {**data, "sources": [{k: v for k, v in s.items() if k not in ("days", "tracked")} for s in data["sources"]]}
    payload = json.dumps(page, ensure_ascii=False).replace("</", "<\\/")
    template = strip_comments(TEMPLATE.read_text("utf-8"))
    # по нему открытая страница узнаёт о новой сборке, не скачивая весь data.json
    (OUT_DIR / "version.json").write_text(json.dumps({"generated": data["generated"]}), "utf-8")
    for lang, page in PAGES.items():
        out = OUT_DIR / page["path"] / "index.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        # данные подставляем последними: в заголовках новостей тоже может встретиться «__HEAD__»
        text = (template.replace("__HTML_LANG__", page["html_lang"])
                .replace("<!--__HEAD__-->", head_tags(lang))
                .replace("__H1__", page["h1"])
                .replace("<!--__PRERENDER__-->", prerender(data, lang))
                .replace('/*__PAGE_LANG__*/"sr"', json.dumps(lang))
                .replace("/*__DATA__*/null", payload))
        out.write_text(text, "utf-8")

    lastmod = data["generated"]
    links = "".join(f'<xhtml:link rel="alternate" hreflang="{l}" href="{SITE_URL}/{p["path"]}"/>' for l, p in PAGES.items())
    urls = "".join(f"<url><loc>{SITE_URL}/{p['path']}</loc><lastmod>{lastmod}</lastmod><changefreq>hourly</changefreq>{links}</url>"
                   for p in PAGES.values())
    (OUT_DIR / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">'
        f"{urls}</urlset>\n", "utf-8")

    if STATIC_DIR.is_dir():
        shutil.copytree(STATIC_DIR, OUT_DIR, dirs_exist_ok=True)


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
    fresh = {r["feed"] for r in results if not r["error"] and r["feed"] not in skip}
    archive = update_archive(results, fresh)
    print(f"Архив новостей: {len(archive)} за {ARCHIVE_DAYS} дн.", file=sys.stderr)
    track_days(results, previous, archive)
    for r in results:
        for item in r["items"]:
            for field in ARCHIVE_ONLY:
                item.pop(field, None)

    data = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sections": section_meta(config),
        "sources": results,
        "info": header_info(),
    }

    add_icons(data)
    OUT_DIR.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    write_pages(data)
    # сюжеты — теневой режим: скрытая страница site/plot/, на главной их пока нет. Ошибка не роняет сборку
    try:
        import stories_live
    except ImportError as exc:
        print(f"Сюжеты пропущены: {exc}", file=sys.stderr)
    else:
        stories_live.safe_run(archive, {s["id"]: s["title_ru"] for s in config["sections"]})

    problems, resting = watch_feeds(jobs, results)
    for p in problems:
        print(f"  сторож лент: {p['name']} — {'не открывается' if p['kind'] == 'broken' else 'молчит'} с {p['since'][:16]}",
              file=sys.stderr)
    # шлём каждую сборку, даже пустой список: так worker узнаёт, что лента ожила. newest — самая свежая новость
    # каждой ленты: по ней worker отличает «снова пишет» от «отбой, у издания выходной»
    newest = {r["feed"]: max((i["date"] for i in r["items"] if i.get("date")), default=None) for r in results}
    reports_api({"feeds": [r["feed"] for r in results], "problems": problems, "resting": resting, "newest": newest}, "feeds")

    ok = sum(1 for r in results if not r["error"] and r["feed"] not in skip)
    print(f"\nГотово: {ok}/{len(results) - len(skip)} лент, {OUT_DIR / 'index.html'}", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--render-only" in sys.argv:  # пересобрать страницы из site/data.json, без лент и перевода — для правок шаблона
        data = json.loads(DATA_FILE.read_text("utf-8"))
        data["sections"] = section_meta(json.loads(SOURCES.read_text("utf-8")))  # порядок и названия разделов — свежие
        add_icons(data)
        write_pages(data)
        sys.exit(0)
    sys.exit(main())
