"""Суть новости одной фразой — подготовка при сборке.

Подводка из ленты часто не говорит, что случилось (IKEA: «объяснила, почему была эвакуация» — а почему, только в статье).
Фразу по полному тексту пишет Haiku, но не при сборке, а когда читатель раскрыл новость (на компьютере — задержался
на подсказке): читателей мало, писать фразы для всех новостей — платить за то, что никто не откроет.

Здесь, при сборке, для новых новостей с переводом: скачиваем страницу, вырезаем текст статьи (article_text.py) и собираем
готовый вопрос модели — правила, глоссарий для этой статьи, заголовок, подводку, текст. Вопросы одной сборки уходят
в worker (POST /texts) одной пачкой: KV на бесплатном тарифе даёт ~1000 записей в день, а сборок 96. Тексты статей
лежат только в KV worker'а и на сайт не попадают (авторское право). В data.json у новости — только номер пачки ("tx"):
по нему worker находит вопрос, а сайт понимает, что для новости можно попросить фразу.

Проба 25.11 на 30 статьях: медиана 157 знаков, максимум 190, ~$0.0006 и ~1,5 с на фразу.
Остальное — в worker (worker/src/index.js, essence): вызов модели, проверка и одна правка, кэш фраз, дневной лимит расходов."""
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import translate as T

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "cache" / "articles.json"
KEEP_DAYS = 5          # чуть дольше, чем новость держится в ленте
TEXT_CAP = 4000        # суть почти всегда в начале, а вход — главная часть цены
MAX_PER_BUILD = 150    # первая сборка после выкатки догоняет всю ленту за несколько заходов
TRIES = 2              # не скачалась или не прошла проверку — ещё одна попытка в следующей сборке
WORKERS = 8
FETCH_TIMEOUT = 12
DEADLINE = 120         # секунд на всё: сборка не должна ждать медленные сайты
SKIP_HOSTS = ("news.google.com", "trends.google.com")  # ссылки-переадресации: страницы статьи за ними нет
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

# правила имён, терминов и оформления — те же, что у перевода заголовков
RULES = T.SYSTEM_PROMPT[T.SYSTEM_PROMPT.index("Смысл, а не калька:"):T.SYSTEM_PROMPT.index("- Пометки (FOTO)")].strip()

SYSTEM = f"""Ты редактор русскоязычного сайта новостей Сербии. Читатель видит заголовок и подводку из ленты, а подводка часто не говорит, что случилось. По полному тексту статьи напиши суть новости одной короткой фразой.

Главное ограничение — длина: 12–25 слов, не больше 190 знаков. Одна мысль, одно предложение. Лучше выбросить подробность, чем превысить длину. Не перечисляй: максимум одна причина или один итог.

Что писать:
- что случилось: кто что сделал, решил, заявил; если помещается — почему или чем кончилось;
- то, чего нет в заголовке, а не пересказ заголовка;
- только факты из статьи; чужие утверждения — «по словам…», «как утверждает…»;
- мнение, колонка — «Автор считает, что…»;
- анонс, отключение, перекрытие — что, где, когда;
- без «В статье говорится», без названия издания, без дат, если они не главное.

Примеры хорошей длины:
- Заголовок «IKEA объяснила, почему была эвакуация» → «Из-за технической неисправности сработала сигнализация, посетителей вывели; через 10 минут магазин снова открылся» (108 знаков)
- Заголовок «Министерство отвергло обвинения в давлении» → «Соцработники жаловались, что министерство торопит центры с выплатами; министерство говорит, что лишь напомнило о сроках» (119 знаков)
- Заголовок «Директор гимназии подала в отставку» → «Директор ушла после того, как родители обвинили её в попытке незаконно собрать данные учеников для голосования за рубежом» (122 знака)

Всё по-русски, кириллицей: сербские имена и названия — русскими буквами (Ђ→Дж, Ћ→Ч, Џ→Дж, Љ→Ль, Њ→Нь, Ј→Й). Латиницей — только то, что латиницей в оригинале и так принято (аббревиатуры партий, бренды).

Текст статьи вырезан со страницы автоматически, в нём могут быть куски других новостей — опирайся на то, что относится к заголовку. Ответ НЕТ (одно слово в поле essence) — только если текст явно про другое событие, чем заголовок, или в нём нет ничего, кроме заголовка. Если статья хоть частично про заголовок — пиши фразу: колонки, заявления, интервью, полемика — это тоже новости.

Как писать имена и термины:
{RULES}"""


def user_message(item: dict, text: str) -> str:
    hints = {e["sr"]: T.glossary_hint(e) for e in T.glossary_for({"title": item["title"], "summary": (item.get("summary") or "") + " " + text[:3000]})}
    hints.update(T.caps_surnames(item))
    glossary = ("Глоссарий для этой статьи:\n" + "\n".join(f"- {k} — {v}" for k, v in hints.items()) + "\n\n") if hints else ""
    return (f"{glossary}Заголовок (оригинал): {item['title']}\nЗаголовок на сайте: {item['ru']['title']}\n"
            f"Подводка из ленты: {item.get('summary') or '(нет)'}\n\nТекст статьи:\n{text}")


def cap(text: str, limit: int = TEXT_CAP) -> str:
    """Обрезка по границе абзаца (или предложения), не посреди слова."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("\n", ". "):
        pos = cut.rfind(sep)
        if pos > limit * 0.6:
            return cut[:pos + 1].strip()
    return cut.rsplit(" ", 1)[0]


def fetch_page(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "sr,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read(3_000_000)


def article(item: dict) -> str | None:
    """Текст статьи или None: не скачалась, не та статья, слишком короткая."""
    from article_text import extract
    raw = fetch_page(item["url"])
    rec = extract(raw, item["title"], item.get("summary") or "")
    return cap(rec["text"]) if rec["ok"] else None


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def attach(results: list[dict]) -> None:
    """Новостям, для которых есть текст, ставит item["tx"]; новые тексты скачивает и отправляет в worker."""
    state = load_state()
    now = datetime.now(timezone.utc)
    items = [i for r in results for i in r["items"] if i.get("ru") and urlparse(i["url"]).netloc not in SKIP_HOSTS]
    todo = [i for i in items if not state.get(i["url"], {}).get("tx") and state.get(i["url"], {}).get("fail", 0) < TRIES]
    todo.sort(key=lambda i: i.get("date") or "", reverse=True)
    todo = todo[:MAX_PER_BUILD]

    try:
        import article_text  # noqa: F401 — lxml и trafilatura: не поставились — без новых текстов, попытки не тратим
    except ImportError as exc:
        print(f"Суть: новые тексты пропущены: {exc}", file=sys.stderr)
        todo = []
    batch, failed, t0 = {}, [], time.time()
    if todo and os.environ.get("REPORTS_URL"):
        pool = ThreadPoolExecutor(WORKERS)
        futures = {pool.submit(article, i): i for i in todo}
        try:
            for f in as_completed(futures, timeout=DEADLINE):
                i = futures[f]
                try:
                    text = f.result()
                except Exception:  # noqa: BLE001 — сайт не ответил, битая страница: следующая попытка в другой сборке
                    text = None
                if text:
                    batch[i["url"]] = user_message(i, text)
                else:
                    failed.append(i["url"])
        except TimeoutError:
            print(f"Суть: не успели за {DEADLINE} с, остальное — в следующей сборке", file=sys.stderr)
        pool.shutdown(wait=False, cancel_futures=True)

    seen = now.isoformat(timespec="seconds")
    if batch:
        tx = now.strftime("%y%m%d%H%M%S")
        if T.reports_api({"id": tx, "system": SYSTEM, "items": batch}, "texts"):
            for url in batch:
                state[url] = {"seen": seen, "tx": tx}
        else:
            batch = {}  # worker не принял — попробуем в следующей сборке, попытку не засчитываем
    for url in failed:
        old = state.get(url, {})
        state[url] = {"seen": seen, "fail": old.get("fail", 0) + 1}
    print(f"Суть: текстов {len(batch)}, не вышло {len(failed)}, за {time.time() - t0:.0f} с", file=sys.stderr)

    current = {i["url"] for i in items}
    for i in items:
        if (tx := state.get(i["url"], {}).get("tx")):
            i["tx"] = tx
            state[i["url"]]["seen"] = seen
    cutoff = (now - timedelta(days=KEEP_DAYS)).isoformat()
    state = {u: s for u, s in state.items() if u in current or s["seen"] >= cutoff}
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), "utf-8")


def safe_attach(results: list[dict]) -> None:
    """Ошибка подготовки текстов не роняет сборку: сайт просто остаётся без фраз у новых новостей."""
    try:
        attach(results)
    except Exception as exc:  # noqa: BLE001
        print(f"Суть пропущена: {type(exc).__name__}: {exc}"[:300], file=sys.stderr)
