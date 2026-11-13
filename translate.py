"""Перевод заголовков и подводок на русский через Claude API.

Переводится только то, чего ещё нет в кэше (cache/translations.json), поэтому
каждая новость оплачивается один раз. Без ключа ANTHROPIC_API_KEY или без пакета
anthropic перевод просто пропускается — сайт собирается как раньше.
"""

import hashlib
import json
import os
import re
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MODEL = "claude-haiku-5-5"
# "medium" на замере 03.11.2026 оказался не лучше "low" и чуть дороже; Sonnet заметно лучше, но ~в 10 раз дороже
EFFORT = "low"
PRICE_PER_MTOK = (0.10, 0.50)  # вход / выход, $ — для оценки в логе и в статистике расходов
BATCH_SIZE = 25
# повтор для того, что не перевелось с первого раза: пачки меньше, модель думает дольше
RETRY_EFFORT = "medium"
RETRY_BATCH = 10
RETRIES = 2
WORKERS = 4
# новости, на которые читатели пожаловались кнопкой «Плохой перевод?» (worker/), переводит модель посильнее.
# Жалобы сборка берёт по адресу REPORTS_URL с паролем REPORTS_TOKEN (секреты репозитория); без них шаг пропускается
REVIEW_MODEL = "claude-sonnet-5-5"
REVIEW_EFFORT = "medium"
REVIEW_PRICE = (2.0, 10.0)
REVIEW_LIMIT = 30  # за одну сборку — на случай, если жалобы шлёт бот
# виды работы: модель и effort; расходы по ним сборка отправляет в worker/ (POST /stats), смотреть — costs.py
KINDS = {"new": (MODEL, EFFORT), "retry": (MODEL, RETRY_EFFORT), "review": (REVIEW_MODEL, REVIEW_EFFORT)}
PRICES = {MODEL: PRICE_PER_MTOK, REVIEW_MODEL: REVIEW_PRICE}
CACHE_FILE = Path(__file__).parent / "cache" / "translations.json"
# потрачено за эту сборку: вид работы → {"items", "in", "out"}; пачки переводятся в несколько потоков
USAGE: dict[str, dict] = {}
USAGE_LOCK = threading.Lock()

# при изменении промпта поменяйте версию — кэш сбросится и всё переведётся заново;
# мелкую правку можно не версионировать: она применится к новым новостям, а старые за день-два уйдут из ленты
PROMPT_VERSION = "4"
# то же для заголовков с таблоидным капсом: поправили fix_caps — смените, и заново переведутся только они
CAPS_VERSION = "1"
SYSTEM_PROMPT = """Ты переводишь новости о Сербии на русский язык для русскоязычного читателя. Пиши так, как написал бы редактор русского новостного сайта: естественным русским языком, а не слово в слово.

На вход приходит JSON-массив материалов с полями id, title и summary. Тексты на сербском (латиницей или кириллицей) или на английском. Переводи каждый текст на русский — никогда не переписывай сербский текст русскими буквами: «Ilija Srdanović o pritiscima na Novu i Radar» → «Илия Срданович о давлении на Nova и Radar», а не «Илия Срданович о притисцима на Нову и Радар».

Сербская кириллица похожа на русский алфавит, но это другой язык. Такие тексты тоже переводи на русский, а не переписывай как есть: «Миљојко Ђорђевић се изјаснио да није крив» → «Милойко Джорджевич заявил, что не признаёт вины». В переводе не должно остаться сербских букв ђ, ћ, џ, љ, њ, ј.

Смысл, а не калька:
- Переводи точно по смыслу: без пересказа, оценок, пояснений и добавлений. Не усиливай и не ослабляй утверждения: «navodno», «sumnja se», «tvrdi» — это «якобы», «подозревают», «утверждает»; оговорки сохраняй.
- Не переворачивай сравнения и отрицания: «Mrtva banka je manje opasna nego živa» → «Мёртвый банк менее опасен, чем живой» (manje — менее, меньше; više — более, больше).
- Время «pola N» — это половина N-го, число то же: pola tri — половина третьего (2:30), pola osam — половина восьмого (7:30).
- Если дословный перевод звучит не по-русски, перестрой фразу, как сказал бы русский журналист. Сербские слова, похожие на русские, часто значат другое — проверяй смысл:
  - oglasiti se (o čemu) — выступить с заявлением, высказаться (по поводу), а не «сообщить о» как о свершившемся факте: «Oglasilo se Više javno tužilaštvo o uništavanju dokumentacije u Domu zdravlja Smederevo» → «Высшая прокуратура выступила с заявлением по поводу уничтожения документов в поликлинике Смедерева»;
  - saopštenje — заявление, сообщение; izjasniti se — высказаться; najaviti — анонсировать, пообещать; pozvati — призвать (кого-то сделать что-то); zahtevati — потребовать; odbiti — отказаться, отклонить; reagovati — отреагировать;
  - vanredni izbori — досрочные выборы (не «чрезвычайные»); lokalni izbori — местные выборы; biračko mesto — избирательный участок; kontrolori na izborima — наблюдатели;
  - uhapšen — задержан; određen pritvor — взят под стражу (pritvor — содержание под стражей); krivična prijava — заявление о преступлении; optužnica — обвинительное заключение; saslušanje — допрос;
  - list, novine — газета; javnost — общественность; javno preduzeće — госпредприятие; JKP — коммунальное предприятие;
  - Dom zdravlja — поликлиника (Dom zdravlja Smederevo — поликлиника в Смедереве); opšta bolnica — больница; Klinički centar — Клинический центр; hitna pomoć — скорая помощь;
  - opština — община; gradska opština — городская община; gradonačelnik — мэр; Skupština grada — городская скупщина; pokrajina — край (AP Vojvodina — автономный край Воеводина);
  - naprednjaci — прогрессисты (сторонники СНС); vlast — власть;
  - guverner (NBS) — управляющий Национальным банком, глава Нацбанка (не «губернатор»).
- Суды и прокуратура: Osnovni sud — Основной суд, Viši sud — Высший суд, Apelacioni sud — Апелляционный суд, Vrhovni sud — Верховный суд, Ustavni sud — Конституционный суд, Privredni sud — Коммерческий суд; Osnovno javno tužilaštvo — Основная прокуратура, Više javno tužilaštvo — Высшая прокуратура, Apelaciono javno tužilaštvo — Апелляционная прокуратура, Vrhovno javno tužilaštvo — Верховная прокуратура, Tužilaštvo za organizovani kriminal — Прокуратура по организованной преступности; tužilaštvo — прокуратура, (javni) tužilac — прокурор (никогда не «обвинительство» и не «обвинитель»).
- Власть: Vlada — правительство, premijer (predsednik Vlade) — премьер-министр, predsednik — президент, Narodna skupština — Народная скупщина, poslanik — депутат, MUP — МВД, ministar unutrašnjih poslova — министр внутренних дел.

Оформление:
- Таблоиды набирают часть заголовка капсом. В переводе капса быть не должно, кроме аббревиатур (СНС, ЕС, РИК), — пиши обычным регистром, как в русских СМИ: имена, страны и названия с большой буквы, остальное со строчной. Это касается и цитат в кавычках. Выделенную капсом фразу отделяй точкой: «INSTITUCIJE MORAJU DA RADE Brnabić: Skupštinu ću sazvati» → «Институты должны работать. Брнабич: созову Скупщину»; «VUČIĆ: Ne pada mi na pamet» → «Вучич: мне и в голову не приходит»; «"NATO NEĆE NAPASTI SRBIJU" Dačić o Rusiji» → «„НАТО не нападёт на Сербию“. Дачич о России».
- Имена и названия пиши русской кириллицей, как принято в русских СМИ: Bojana Lazić — Бояна Лазич, Ljubiša — Любиша, Vučić — Вучич, Brnabić — Брнабич, Đorđević — Джорджевич, Novi Sad — Нови-Сад, Skupština — Скупщина, SNS — СНС, Studentska lista — «Студенческий список». Названия СМИ (N1, Nova, Informer) оставляй латиницей. Если ведомство в оригинале названо полностью, пиши его полное русское название, а не аббревиатуру; сербские аббревиатуры (JTOK, VJT, OJT, BIA) оставляй латиницей, только если в оригинале стоит сама аббревиатура. Не придумывай русских аббревиатур из названий («ВСП», «ВЯП»): если название из глоссария, пиши его полностью; привычные сокращения (ЕС, США, ОБСЕ, НАТО, МВД, СНС, РИК) можно.
- Названия компаний, банков, спортивных клубов, брендов и заведений — это имена: не переводи их по смыслу, а передавай русскими буквами в кавычках: Poštanska štedionica — «Поштанска штедионица» (а не «Почтовая сберегательная касса»), Crvena zvezda — «Црвена звезда», Zvezda — «Звезда» (а не «Красная звезда»), Alta banka — «Алта банка». Английские названия (Financial Times, Alta Pay Group) оставляй латиницей. Государственные органы, учреждения и партии (Narodna banka Srbije — Национальный банк Сербии, Srpska napredna stranka — Сербская прогрессивная партия) переводи, как принято в русских СМИ.
- Пометки (FOTO), (VIDEO) переводи как (фото), (видео).
- Пустой summary оставляй пустым.
- Верни перевод для каждого id из входа.

Оценка подводки (поле lead_ok) — по оригиналу, а не по переводу:
- true — заголовок вместе с подводкой отвечают на вопрос «что произошло»: кто что сделал, решил, заявил, что случилось или что и когда состоится (объявление о событии с датой и местом — тоже true);
- false — подводка сути не сообщает: зазывает к материалу («смотрите», «читайте», «узнайте»), задаёт вопрос, даёт общую фразу или оценку без факта, говорит о другом, реклама, гороскоп, рецепт;
- если подводки нет или она повторяет заголовок — оценивай один заголовок, и строже: true, только если по нему одному новость можно пересказать фразой без догадок — кто что сделал или что где случилось («MUP: u Beogradu uhapšene tri osobe zbog razbojništva»). false — если заголовок прячет главное («u gradu u Srbiji», «poznati biznismen», «ovaj uređaj»), зазывает («evo zašto», «otkriva», «pogledajte»), задаёт вопрос, или это мнение, колонка, разбор без нового события.

Тип материала (поле kind) — по оригиналу, независимо от того, хорош ли заголовок:
- event — новость о событии: что-то произошло, кто-то что-то сделал, решил, потребовал или заявил (заявление политика или ведомства — тоже event), что-то состоится. Кликбейтный заголовок, за которым событие («Horor u centru Čačka»), — тоже event;
- opinion — колонка, комментарий, мнение, разбор «что это значит», прогноз аналитика, интервью-рассуждение без новости;
- service — погода, курс, результаты лото, гороскоп, рецепты, советы, обзор первых полос газет, «как правильно пишется»;
- feature — история, культура, портреты и воспоминания, «на сегодняшний день», некрологи-очерки, праздники и памятные дни.

Разметка для группировки новостей в события — по-русски, только из того, что есть в заголовке и подводке, ничего не додумывая:
- what — только для kind=event: одно нейтральное предложение до 20 слов, что произошло — кто что сделал или что где случилось, без оценок, кликбейта и цитат: «Прокуратура Нови-Сада просит взять под стражу краевого секретаря Предрага Вулетича». Если заголовок держится на подробности, последствии или реакции, а главное событие (задержание, решение, авария) упомянуто в заголовке или подводке или названы его участники, — начни с этого события и его главного участника, потом подробность: what должен показывать, к какому событию относится новость. Если по заголовку и подводке не понять, что произошло, или kind не event — пустая строка;
- entities — до 5 главных участников: люди (имя и фамилия, если они есть в тексте), организации, партии, компании — в той же русской форме, что в переводе, в именительном падеже: ["Предраг Вулетич", "Высшая прокуратура Нови-Сада"]. Места сюда не пиши; нет участников — пустой массив;
- place — где произошло: город или край Сербии («Нови-Сад», «Воеводина», «Косово»), для событий за границей — страна; пустая строка, если событие общесербское или место не названо;
- topic — politics (власть, выборы, партии, протесты, дипломатия Сербии), crime (преступления, полиция, суды, коррупционные дела), economy (деньги, банки, цены, компании, работа), society (образование, здоровье, транспорт, город, экология, права), incident (аварии, пожары, катастрофы, гибель людей), culture, sport, world (события за границей без участия Сербии), other."""

TOPICS = ["politics", "crime", "economy", "society", "incident", "culture", "sport", "world", "other"]
# разметка для сюжетов (what, entities, place, topic) идёт в архив, а не на сайт — см. build.ARCHIVE_ONLY
MARKUP = ("kind", "what", "entities", "place", "topic")

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "lead_ok": {"type": "boolean"},
                    "kind": {"type": "string", "enum": ["event", "opinion", "service", "feature"]},
                    "what": {"type": "string"},
                    "entities": {"type": "array", "items": {"type": "string"}},
                    "place": {"type": "string"},
                    "topic": {"type": "string", "enum": TOPICS},
                },
                "required": ["id", "title", "summary", "lead_ok", "kind", "what", "entities", "place", "topic"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


# русскоязычные новости не переводим, но размечаем — те же правила, что в SYSTEM_PROMPT. Без разметки сюжеты видят
# у них один заголовок: 12–13.11 модель раскладки дважды сочла «непонятным» анонс матча Сербия — Германия из Google News
MARKUP_PROMPT = ("Ты размечаешь русскоязычные новости о Сербии. На вход приходит JSON-массив материалов с полями id, title "
                 "и summary. Для каждого id верни поля разметки.\n\n" + SYSTEM_PROMPT[SYSTEM_PROMPT.index("Тип материала (поле kind)"):])
MARKUP_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {k: v for k, v in SCHEMA["properties"]["items"]["items"]["properties"].items()
                               if k == "id" or k in MARKUP},
                "required": ["id", *MARKUP],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def text_key(item: dict) -> str:
    # ключ по тексту, а не по ссылке: если издание поправило заголовок, он переведётся заново.
    # Записи глоссария, которые встречаются в новости, тоже входят в ключ: поправили глоссарий —
    # заново переведутся только новости с этим названием
    names = "".join(f"\n{e['sr']}={glossary_hint(e)}" for e in glossary_for(item))
    caps = "caps" + CAPS_VERSION if has_caps_run(item["title"]) else ""
    raw = PROMPT_VERSION + caps + names + "\n" + item["title"] + "\n" + item.get("summary", "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# метка прямой трансляции в начале заголовка: «UŽIVO Vučić: …», «(UŽIVO) ČLANOVI REM…».
# Модель передаёт её как попало («ВЖИВУЮ», «В прямом эфире»), поэтому переводим заголовок без неё,
# а «ОНЛАЙН» ставим сами. «Uživo» с заглавной — метка, только если за ним скобка или знак: «Uživo prenos» — нет
LIVE_TAG = re.compile(r"\W*(UŽIVO|UZIVO|УЖИВО|Uživo|Уживо)(?!\w)(\W*)")


def split_live(title: str) -> tuple[str, str]:
    """«(UŽIVO) ČLANOVI REM» → («(ОНЛАЙН) », «ČLANOVI REM»); без метки — («», title)."""
    m = LIVE_TAG.match(title)
    if not m or not title[m.end():] or (m.group(1)[1:].islower() and not m.group(2).strip()):
        return "", title
    return title[:m.end()].replace(m.group(1), "ОНЛАЙН").rstrip() + " ", title[m.end():]


SERBIAN_ONLY_LETTERS = re.compile("[ђћџљњјЂЋЏЉЊЈ]")
# Сербский и русский пишем одной «скелетной» латиницей без диакритики, чтобы сравнивать слова
SKELETON = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "dj", "е": "e", "ё": "e", "ж": "z", "з": "z", "и": "i",
    "й": "j", "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m", "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "ћ": "c", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "c", "џ": "dz", "ш": "s", "щ": "s",
    "ы": "y", "э": "e", "ю": "ju", "я": "ja", "ь": "", "ъ": "", "č": "c", "ć": "c", "đ": "dj", "ž": "z", "š": "s",
})
URL_RE = re.compile(r"\S+://\S+|www\.\S+")
# модель иногда собирает имя из двух алфавитов: «Илija», «Зарićем», «Долováц»
MIXED_SCRIPT_RE = re.compile(r"[^\W\d_]*(?:[\u0400-\u04ff][A-Za-z\u00c0-\u024f]|[A-Za-z\u00c0-\u024f][\u0400-\u04ff])[^\W\d_]*")


# латинские буквы, которые выглядят как русские: «Цацилендe» с латинской «e» на конце — это опечатка, а не непереведённое имя
HOMOGLYPHS = str.maketrans("aceopxyAEOPCXTHKMB", "асеорхуАЕОРСХТНКМВ")


def fix_homoglyphs(text: str) -> str:
    """«Цацилендe» → «Цациленде»: в кириллическом слове меняем латинские двойники на русские буквы.
    Слова с другими латинскими буквами («Зарićем») не трогаем — их отбракует rejection."""
    def repl(m):
        word = m.group(0)
        latin = {c for c in word if c.isascii()}
        return word.translate(HOMOGLYPHS) if latin and latin <= set("aceopxyAEOPCXTHKMB") else word
    return MIXED_SCRIPT_RE.sub(repl, text)


def foreign_letter(char: str) -> bool:
    """Буква не из латиницы и не из кириллицы: модель иногда вставляет японскую кану — «Даスティн Хоффман»."""
    return char.isalpha() and not (char <= "\u024f" or "\u0400" <= char <= "\u04ff")
WORDS_RE = re.compile(r"[^\W\d_]+")


# служебные слова, которые есть в сербском, но не в русском: «се», «су», «када», «шта», «није»…
SERBIAN_WORDS = set("se su sa ka kod sta kada gde koji koja koje koju kojih kojima nije nisu nisam jer vec kao "
                    "pre pored izmedju zbog kroz prema tokom bio bila bilo bili biti vise nakon sada zasto koliko "
                    "dok jos uz evo ce cu ovaj ovo ova posto ali da".split())
HYPHENATED_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)+")


def skeleton_words(text: str, lowercase_only: bool = False, min_len: int = 3) -> list[str]:
    words = WORDS_RE.findall(text)
    if lowercase_only:
        # имена собственные одинаковы в обоих языках — сравниваем только обычные слова
        words = [w for w in words if w.islower()]
    return [w.lower().translate(SKELETON) for w in words if len(w) >= min_len]


# glossary.json — названия и термины, которые надо всегда переводить одинаково:
# {"names": [{"sr": "Poštanska štedionica", "ru": "Поштанска штедионица"}, ...]}.
# Необязательное поле note — пояснение для модели, когда слово многозначное: «Mađar» — и фамилия, и «венгр».
# Кандидатов ищут audit_names.py (названия, переведённые по смыслу) и audit_terms.py (учреждения, партии, термины).
GLOSSARY_FILE = Path(__file__).parent / "glossary.json"
# сербская кириллица → латиница, с диакритикой: чтобы «НИС» совпал с «NIS», а «NIŠ» — нет
SR_LATIN_UPPER = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Ђ": "Đ", "Е": "E", "Ж": "Ž", "З": "Z", "И": "I", "Ј": "J",
    "К": "K", "Л": "L", "Љ": "LJ", "М": "M", "Н": "N", "Њ": "NJ", "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T",
    "Ћ": "Ć", "У": "U", "Ф": "F", "Х": "H", "Ц": "C", "Ч": "Č", "Џ": "DŽ", "Ш": "Š",
}
SR_LATIN = str.maketrans({**SR_LATIN_UPPER, **{c.lower(): l.lower() for c, l in SR_LATIN_UPPER.items()}})


def load_glossary() -> list[dict]:
    try:
        return json.loads(GLOSSARY_FILE.read_text("utf-8"))["names"]
    except (OSError, ValueError, KeyError):
        return []


def glossary_pattern(name: str) -> re.Pattern:
    # сербские названия склоняются: «Poštanska štedionica», «Poštanskoj štedionici» — сравниваем по началу слов,
    # а окончание — не длиннее падежного, иначе «Novi Beograd» совпадёт с «novembar u Beogradu»
    # Короткие слова («i», «za», «u») пропускаем и в названии, и в тексте между словами названия
    stems = [w[:max(3, len(w) - 2)] for w in skeleton_words(name)]
    if not stems:
        # «od A do Š»: все слова короткие, сравнивать по началу нечего — ищем фразу целиком
        return re.compile(r"\b" + r"\W+".join(map(re.escape, skeleton_words(name, min_len=1))) + r"\b")
    return re.compile(r"\b" + r"\w{0,4}\W+(?:\w{1,2}\W+)*".join(map(re.escape, stems)) + r"\w{0,4}\b")


def is_abbreviation(name: str) -> bool:
    # NIS, CG, BiH — их ищем только целым словом с тем же регистром, иначе «NIS» совпадёт с «Niš» и «nisu»
    return " " not in name and len(name) <= 5 and sum(c.isupper() for c in name) >= 2


GLOSSARY = [(entry, None if is_abbreviation(entry["sr"]) else glossary_pattern(entry["sr"])) for entry in load_glossary()]


def glossary_hint(entry: dict) -> str:
    return entry["ru"] + (f" ({entry['note']})" if entry.get("note") else "")


def glossary_for(item: dict) -> list[dict]:
    raw = item["title"] + " " + item.get("summary", "")
    words = {w.translate(SR_LATIN) for w in WORDS_RE.findall(raw)}
    text = " ".join(skeleton_words(raw, min_len=1))
    return [entry for entry, pattern in GLOSSARY if (entry["sr"] in words if pattern is None else pattern.search(text))]


# фамилии на -ić капсом («MILATOVIĆ», «ŽMIRIĆEVA») модель читает плохо: «Миятович», «Нестерович».
# Переписываем их по правилам и подсказываем, как глоссарий. Окончание «-IĆE» не берём: это чаще будущее время («UNIŠTIĆE»)
CAPS_SURNAME = re.compile(r"\b([A-ZČĆŽŠĐ]{2,}IĆ)(?:A|U|EM|I|EVA|EVOG|EVE|EVU|EVIH|EVOJ|EVIM)?\b")
LATIN_TO_SR = [("DŽ", "Џ"), ("LJ", "Љ"), ("NJ", "Њ")] + [(l, c) for c, l in SR_LATIN_UPPER.items() if len(l) == 1]


def caps_surnames(item: dict) -> dict[str, str]:
    """«MILATOVIĆ UPRAVO OTKRIO…» → {"Milatović": "Милатович"}."""
    out = {}
    for m in CAPS_SURNAME.finditer(item["title"] + " " + item.get("summary", "")):
        word = m.group(1)
        cyr = word
        for lat, sr in LATIN_TO_SR:
            cyr = cyr.replace(lat, sr)
        out[word[0] + word[1:].lower()] = russify(cyr[0] + cyr[1:].lower())
    return out


def share_of_original(original: str, translated: str) -> float:
    """Доля обычных слов перевода, которые совпадают со словами оригинала (после транслитерации)."""
    # ссылки модель оставляет как есть — их слова («vesti», «drustvo») не считаем
    source = set(skeleton_words(URL_RE.sub(" ", original)))
    words = skeleton_words(URL_RE.sub(" ", translated), lowercase_only=True)
    if len(words) < 4:
        return 0.0  # слишком мало слов, чтобы судить
    return sum(w in source for w in words) / len(words)


CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")


def lost_text(original: dict, translated: dict) -> bool:
    """Модель выкинула почти весь текст: «Almost Vida Petrović Škero» вместо перевода длинного заголовка
    или пустая подводка там, где в оригинале она была."""
    return ((len(WORDS_RE.findall(original["title"])) >= 4 and not CYRILLIC_RE.search(translated["title"]))
            or (bool(original.get("summary", "").strip()) and not translated["summary"].strip()))


LATIN_WORD_RE = re.compile(r"[a-z\u00c0-\u024f]+")


def latin_words(original: dict, translated: dict) -> list[str]:
    """Обычные слова латиницей, которых нет в оригинале: модель иногда вставляет английское слово
    вместо русского — «прослушивала редакцию и following главного редактора», «плотина near Куршумлии».
    Названия (Radar, N1) пишутся с заглавной и сюда не попадают."""
    source = {w.lower() for w in WORDS_RE.findall(original["title"] + " " + original.get("summary", ""))}
    text = URL_RE.sub(" ", translated["title"] + " " + translated["summary"])
    return list(dict.fromkeys(w for w in WORDS_RE.findall(text) if LATIN_WORD_RE.fullmatch(w) and w not in source))


def rejection(original: dict, translated: dict, strict: bool = True) -> str | None:
    """Почему «перевод» на деле остался сербским текстом (None — перевод годный). Например, сербский,
    переписанный кириллицей: «о притисцима на Нову и Радар» вместо «о давлении на Nova и Radar».
    Причину видит модель при повторе и мы в Telegram, если новость так и не перевелась.
    Пороги подобраны на реальных переводах: общие для двух языков слова («после», «против», «видео»)
    хорошие переводы через них не перешагивают. strict=False — без сравнения со словами оригинала:
    короткий заголовок вроде «Средњи курс динара за евро» по-русски почти дословно тот же."""
    title, summary = translated["title"].strip(), translated["summary"]
    if not title:
        return "пустой заголовок"
    # без букв переводить нечего: запрос Google Trends «2026» так и остаётся «2026».
    # Короткий кириллический запрос без сербских букв («надзор») по-русски бывает тем же словом —
    # в последней попытке (strict=False) такой принимаем
    same_in_russian = (len(title.split()) <= 3 and not re.search("[A-Za-z]", title)
                       and not SERBIAN_ONLY_LETTERS.search(title))
    if title == original["title"].strip() and WORDS_RE.search(title) and (strict or not same_in_russian):
        return "заголовок остался как в оригинале"
    if lost_text(original, translated):
        return "в переводе пропала большая часть заголовка или подводки"
    text = URL_RE.sub(" ", title + " " + summary)
    words = WORDS_RE.findall(text)
    # сербские буквы в именах поправит russify, а в обычных словах («још», «уређивачку») — это непереведённый текст
    if found := [w for w in words if w.islower() and SERBIAN_ONLY_LETTERS.search(w)]:
        return "остались сербские слова: " + ", ".join(dict.fromkeys(found))
    if found := latin_words(original, translated):
        return "слова латиницей вместо русских: " + ", ".join(found)
    if found := MIXED_SCRIPT_RE.findall(text):
        return "слова из двух алфавитов: " + ", ".join(dict.fromkeys(found))
    if found := [c for c in text if foreign_letter(c)]:
        return "посторонние буквы: " + "".join(dict.fromkeys(found))
    # слова через дефис — названия: «Нови-Сада» — это Нови-Сад, а не сербское «сада» (сейчас)
    plain = WORDS_RE.findall(HYPHENATED_RE.sub(" ", text))
    if len(found := [w for w in plain if w.lower().translate(SKELETON) in SERBIAN_WORDS]) >= 3:
        return "остались сербские служебные слова: " + ", ".join(dict.fromkeys(found))
    if strict and (share_of_original(original["title"], title) >= 0.7
                   or share_of_original(original.get("summary", ""), summary) >= 0.6):
        return "перевод почти дословно повторяет сербский оригинал"
    return None


CAPS_WORD = re.compile(r"^[^\w]*[A-ZА-ЯЁ]+[^\w]*$")
# аббревиатуры, которые остаются капсом даже внутри таблоидной фразы
ABBREVIATIONS = {"ЕС", "ЕУ", "НАТО", "США", "ООН", "РФ", "СНС", "СПС", "РИК", "ЦИК", "БИА", "МВД", "МВФ", "ФСБ", "ЕБРР", "ВОЗ", "ФИФА", "УЕФА", "ЕП", "NATO", "EU", "USA", "UN", "SNS", "RIK", "BIA", "MUP", "N1", "REM"}
SENTENCE_START = re.compile(r"(^|[.!?…:]\s*)[«„\"(]*$")


def has_caps_run(text: str) -> bool:
    """Два слова капсом подряд: «PRIZNANJE KOSOVA BIĆE…» — такой заголовок правит fix_caps."""
    words = [re.sub(r"\W", "", w) for w in text.split()]
    return any(len(a) >= 2 and len(b) >= 2 and a.isupper() and b.isupper() for a, b in zip(words, words[1:]))


def capitalize(word: str) -> str:
    pos = next((n for n, c in enumerate(word) if c.isalpha()), None)
    return word if pos is None else word[:pos] + word[pos].upper() + word[pos + 1:]


SR_TO_LATIN = str.maketrans({"Ј": "J", "Ђ": "Đ", "Ћ": "Ć", "Џ": "DŽ", "Љ": "LJ", "Њ": "NJ"})
# только слова с «Ј»: JTOK, VJT, OJT. «ВУЧИЋ» — это имя капсом, его поправит russify
SERBIAN_ABBR = re.compile(r"\b[А-ЯЁЈЂЋЏЉЊ]*Ј[А-ЯЁЈЂЋЏЉЊ]*\b")


def fix_abbreviations(text: str) -> str:
    """«ЈТОК» → «JTOK»: сербская аббревиатура, записанная кириллицей, возвращается в латиницу."""
    def to_latin(m):
        word = m.group(0)
        if len(word) > 6:
            return word
        cyr = "АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШ"
        lat = ["A", "B", "V", "G", "D", "E", "Ž", "Z", "I", "K", "L", "M", "N", "O", "P", "R", "S", "T", "U", "F", "H", "C", "Č", "Š"]
        return word.translate(SR_TO_LATIN).translate(str.maketrans(dict(zip(cyr, lat))))
    return SERBIAN_ABBR.sub(to_latin, text)


# сербские буквы в именах → русское написание: «Бојана Лазић» → «Бояна Лазич», «Љубиша» → «Любиша»
IOTATED = {"а": "я", "у": "ю", "е": "е", "о": "ё", "и": "и"}
SR_LETTER_RE = re.compile(r"([ЉЊљњ])([аеиоуАЕИОУ]?)|([Јј])([аеиоуАЕИОУ]?)|[ЋћЂђЏџ]")
SIMPLE = {"Ћ": "Ч", "ћ": "ч", "Ђ": "Дж", "ђ": "дж", "Џ": "Дж", "џ": "дж"}


def russify(text: str) -> str:
    """Модель иногда оставляет имена в сербской кириллице, из-за одной буквы перевод отбраковывался."""
    def repl(m):
        if m.group(1):  # љ/њ: перед гласной смягчает её, иначе мягкий знак
            base = "Л" if m.group(1) in "Љљ" else "Н"
            base = base if m.group(1).isupper() else base.lower()
            vowel = m.group(2)
            if not vowel:
                return base + "ь"
            soft = IOTATED[vowel.lower()]
            return base + (soft.upper() if vowel.isupper() else soft)
        if m.group(3):  # ј: «ја» → «я», «ој» → «ой»
            vowel = m.group(4)
            if not vowel:
                return "Й" if m.group(3).isupper() else "й"
            soft = "йо" if vowel.lower() == "о" else IOTATED[vowel.lower()]  # «Јован» → «Йован»
            return soft.capitalize() if m.group(3).isupper() else soft
        return SIMPLE[m.group(0)]
    return SR_LETTER_RE.sub(repl, text)


def proper_names(texts: list[str]) -> set[str]:
    """Слова, которые модель пишет с большой буквы не в начале фразы и нигде — с маленькой, — имена и названия,
    в нижнем регистре. По ним fix_caps возвращает большую букву: «ПРИЗНАНИЕ КОСОВА» → «Признание Косова», а не «косова».
    Слова и с маленькой буквы отбрасываем: с большой они бывают в начале цитаты («На …») или в названии («Нови-Сад»)."""
    out, lower = set(), set()
    for text in texts:
        words = text.split(" ")
        for n, word in enumerate(words):
            core = re.sub(r"^\W+|\W+$", "", word)
            if core[:1].islower():
                lower.add(core)
            elif (core[:1].isupper() and not CAPS_WORD.match(word)
                    and n and not SENTENCE_START.search(" ".join(words[:n]) + " ")):
                out.add(core.lower())
    return out - lower


def fix_caps(text: str, names: set[str] = frozenset()) -> str:
    """Подстраховка к промпту: таблоидный капс из двух и больше слов подряд → обычный регистр.
    Одиночные слова капсом не трогаем — это обычно аббревиатуры (СНС, РИК, ЕС).
    names — имена и названия из proper_names: их пишем с большой буквы."""
    words = text.split(" ")
    i = 0
    while i < len(words):
        j = i
        while j < len(words) and CAPS_WORD.match(words[j]):
            j += 1
        if sum(len(re.sub(r"\W", "", w)) >= 2 for w in words[i:j]) >= 2:
            for k in range(i, j):
                if re.sub(r"\W", "", words[k]) in ABBREVIATIONS:
                    continue
                words[k] = words[k].lower()
                if re.sub(r"^\W+|\W+$", "", words[k]) in names:
                    words[k] = capitalize(words[k])
                if words[k][:1] in "«„\"":  # слово в кавычках — чаще всего название
                    words[k] = capitalize(words[k])
            if i == 0 or SENTENCE_START.search(" ".join(words[:i]) + " "):
                words[i] = capitalize(words[i])
        i = max(j, i + 1)
    return " ".join(words)


def lead_state(item: dict, lead_ok: bool | None) -> str | None:
    """Понятно ли по новости, что произошло, — для сводки и сюжетов (оценка модели при переводе):
    "good" / "bad" — подводка отвечает или не отвечает на «что произошло»;
    без подводки (пустая или повторяет заголовок): "title" — заголовок сам говорит, что произошло, "none" — нет
    или оценки нет (русскоязычная новость). Модель судит заголовок без подводки с 07.11.2026, до этого такие — "none".
    None — оценки нет: новость с подводкой, переведённая до появления оценки (04.11.2026).
    Кому нужен лид из текста статьи — "bad" и "none"."""
    summary = WORDS_RE.findall(item.get("summary", "").lower())
    if not summary or summary == WORDS_RE.findall(item["title"].lower()):
        return "title" if lead_ok else "none"
    return None if lead_ok is None else "good" if lead_ok else "bad"


def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def translate_batch(client, batch: list[tuple[str, dict]], kind: str = "new", strict: bool = True,
                                    rejected: dict | None = None) -> dict:
    """Переводы, прошедшие проверку. rejected — что отбраковали в прошлых попытках: ключ → {"reason", "attempt", "attempt_summary"}
    (причина и отбракованный перевод); причины уходят модели подсказкой, сюда же пишутся отбракованные в этой."""
    model, effort = KINDS[kind]
    # модели даём короткие номера, а не хэши: длинные id она иногда переписывает с ошибкой
    payload = [{"id": str(n), "title": item["title"], "summary": item.get("summary", "")} for n, (_, item) in enumerate(batch)]
    names = {sr: ru for _, item in batch for sr, ru in caps_surnames(item).items()}
    names.update({entry["sr"]: glossary_hint(entry) for _, item in batch for entry in glossary_for(item)})
    content = json.dumps(payload, ensure_ascii=False)
    hints = [f"id {n}: {rejected[key]['reason']}" for n, (key, _) in enumerate(batch) if key in (rejected or {})]
    if hints:
        content = ("Прошлый перевод этих материалов не годится — исправь:\n" + "\n".join(hints) + "\n\n" + content)
    if names:
        content = ("Названия из глоссария — пиши их именно так, склоняя по-русски, если нужно:\n"
                   + "\n".join(f"{sr} → {ru}" for sr, ru in names.items()) + "\n\n" + content)
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    # считаем до проверок: за оборванный или негодный ответ тоже платим
    spend(kind, len(batch), response.usage)
    if response.stop_reason in ("max_tokens", "refusal"):
        raise RuntimeError(f"stop_reason={response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    by_number = {str(n): pair for n, pair in enumerate(batch)}
    out = {}
    items = json.loads(text)["items"]
    # имена берём со всей пачки: «Сербии» в капсовом заголовке найдётся в подводке этой или соседней новости
    names = proper_names([russify(t[k]) for t in items for k in ("title", "summary", "what")])
    for t in items:
        key, item = by_number.get(t["id"], (None, None))
        if not item:
            continue
        for k in ("title", "summary", "what", "place"):
            t[k] = fix_homoglyphs(t[k])
        t["entities"] = [fix_homoglyphs(e) for e in t["entities"]]
        # проверяем до russify: сербские буквы в «переводе» — тоже признак, что модель не перевела
        reason = rejection(item, t, strict)
        if reason is None:
            out[key] = {k: fix_caps(russify(fix_abbreviations(t[k])), names) for k in ("title", "summary")}
            out[key]["lead_ok"] = t["lead_ok"]
            out[key]["kind"] = t["kind"]
            out[key]["what"] = fix_caps(russify(t["what"].strip()), names) if t["kind"] == "event" else ""
            out[key]["entities"] = list(dict.fromkeys(russify(e.strip()) for e in t["entities"] if e.strip()))[:5]
            out[key]["place"] = russify(t["place"].strip())
            out[key]["topic"] = t["topic"]
        elif rejected is not None:
            rejected[key] = {"reason": reason, "attempt": t["title"], "attempt_summary": t["summary"]}
    return out


def markup_batch(client, batch: list[tuple[str, dict]]) -> dict:
    """Разметка (MARKUP) русскоязычных новостей: ключ → {kind, what, entities, place, topic}."""
    payload = [{"id": str(n), "title": item["title"], "summary": item.get("summary", "")} for n, (_, item) in enumerate(batch)]
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=MARKUP_PROMPT,
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": MARKUP_SCHEMA}},
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    spend("new", len(batch), response.usage)  # в статистике расходов — вместе с новыми переводами: та же модель
    if response.stop_reason in ("max_tokens", "refusal"):
        raise RuntimeError(f"stop_reason={response.stop_reason}")
    by_number = {str(n): key for n, (key, _) in enumerate(batch)}
    out = {}
    for t in json.loads(next(b.text for b in response.content if b.type == "text"))["items"]:
        if t["id"] in by_number:
            out[by_number[t["id"]]] = {
                "kind": t["kind"], "what": t["what"].strip() if t["kind"] == "event" else "",
                "entities": list(dict.fromkeys(e.strip() for e in t["entities"] if e.strip()))[:5],
                "place": t["place"].strip(), "topic": t["topic"]}
    return out


def spend(kind: str, items: int, usage) -> None:
    with USAGE_LOCK:
        total = USAGE.setdefault(kind, {"items": 0, "in": 0, "out": 0})
        total["items"] += items
        total["in"] += usage.input_tokens
        total["out"] += usage.output_tokens


def cost(*kinds: str) -> tuple[int, int, float]:
    """Токены на входе, на выходе и примерная цена в $ за эти виды работы в текущей сборке."""
    tokens_in = tokens_out = usd = 0
    for kind in kinds:
        total, price = USAGE.get(kind, {}), PRICES[KINDS[kind][0]]
        tokens_in += total.get("in", 0)
        tokens_out += total.get("out", 0)
        usd += total.get("in", 0) / 1e6 * price[0] + total.get("out", 0) / 1e6 * price[1]
    return tokens_in, tokens_out, usd


def connect():
    """Модуль anthropic и клиент — или (None, None), если переводить нечем."""
    try:
        import anthropic
    except ImportError:
        print("Перевод пропущен: не установлен пакет anthropic (pip install -r requirements.txt)", file=sys.stderr)
        return None, None
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("Перевод пропущен: не задан ANTHROPIC_API_KEY", file=sys.stderr)
        return None, None
    return anthropic, anthropic.Anthropic()


def reports_api(data: dict | None = None, path: str = "reports") -> dict | None:
    """Приёмник жалоб (worker/): без data — список жалоб, ждущих итога, с data — отправка итогов.
    path="stats" — туда же отправляем расходы сборки, path="untranslated" — новости, которые не перевелись,
    path="feeds" — сторож лент: какие ленты не открываются или молчат, path="stories" — итоги команд сюжетам из бота."""
    url, token = os.environ.get("REPORTS_URL"), os.environ.get("REPORTS_TOKEN")
    if not (url and token):
        return None
    request = urllib.request.Request(
        url.rsplit("/", 1)[0] + "/" + path, data=None if data is None else json.dumps(data).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "vestomat-build"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except Exception as exc:  # noqa: BLE001 — без жалоб сборка всё равно должна пройти
        print(f"Приёмник жалоб не ответил: {type(exc).__name__}: {exc}"[:200], file=sys.stderr)
        return None


def reported_urls() -> set[str]:
    """Адреса новостей, на перевод которых пожаловались читатели за последнюю неделю."""
    answer = reports_api()
    return {report["url"] for report in answer["reports"]} if answer else set()


def report_results(reported: set[str], copies: dict, cache: dict, before: dict) -> None:
    """Итоги по жалобам — Worker отметит ими сообщения в Telegram (статусы описаны в worker/src/index.js)."""
    key_of = {item["url"]: key for key, items in copies.items() for item in items}
    results = []
    for url in reported:
        key = key_of.get(url)
        if key is None:
            status = "gone"
        elif cache.get(key, {}).get("by") != REVIEW_MODEL:
            if key not in before:
                continue  # до неё не дошла очередь или нечем переводить — попробуем в следующую сборку
            status = "error"
        else:
            old = before.get(key, cache[key])
            changed = any(old.get(k) != cache[key].get(k) for k in ("title", "summary"))
            status = "fixed" if changed else "same"
        results.append({"url": url, "status": status})
    if results:
        reports_api({"results": results})


def attach_translations(sources: list[dict]) -> None:
    """Добавляет к каждой нерусскоязычной новости поле "ru": {"title", "summary"}, а ко всем — "lead" (см. lead_state).
    MARKUP — оценки модели: kind (event, opinion, service, feature), what (что произошло, одной фразой), entities,
    place, topic; у русскоязычных новостей — с 13.11 (markup_batch), у переведённых до 07.11.2026 их нет."""
    # один и тот же текст агентства бывает у нескольких изданий: переводим один раз, раздаём всем
    todo, copies, source_of = {}, {}, {}
    marks = {}  # русскоязычные: ключ разметки → новости (переводить не нужно, только разметить)
    for src in sources:
        for item in src["items"]:
            if item.get("lang", src["lang"]) == "ru":
                marks.setdefault("ru" + text_key(item), []).append(item)
            else:
                bare = {**item, "title": split_live(item["title"])[1]}
                key = text_key(bare)
                todo[key] = bare
                copies.setdefault(key, []).append(item)
                source_of.setdefault(key, src["name"])

    cache = load_cache()
    # такие сбои уже могли попасть в кэш до появления проверки — переводим их заново
    for key in [k for k in todo if k in cache and (lost_text(todo[k], cache[k]) or latin_words(todo[k], cache[k]))]:
        del cache[key]
    missing = [(key, item) for key, item in todo.items() if key not in cache]

    # новости, на которые пожаловались читатели (кнопка «Плохой перевод?»), один раз переводим заново моделью посильнее
    reported = reported_urls()
    review = [(key, todo[key]) for key, items in copies.items()
              if key in cache and cache[key].get("by") != REVIEW_MODEL and any(i["url"] in reported for i in items)][:REVIEW_LIMIT]
    unmarked = [(key, items[0]) for key, items in marks.items() if key not in cache]
    anthropic, client = connect() if missing or review or unmarked else (None, None)

    if missing and client:
        batches = [missing[i:i + BATCH_SIZE] for i in range(0, len(missing), BATCH_SIZE)]
        failed = 0
        rejected = {}  # что отбраковала проверка: причина пойдёт подсказкой в повтор, а если не поможет — в Telegram
        print(f"\nПеревожу {len(missing)} новостей ({len(batches)} пачек)…", file=sys.stderr)

        def run(batch, kind="new", strict=True):
            try:
                return translate_batch(client, batch, kind, strict, rejected)
            except anthropic.AuthenticationError:
                raise
            except Exception as exc:  # noqa: BLE001 — не переведённое попробуем в следующую сборку
                print(f"  пачка не переведена: {type(exc).__name__}: {exc}"[:200], file=sys.stderr)
                return None

        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for n, result in enumerate(pool.map(run, batches), 1):
                    if result is None:
                        failed += 1
                    else:
                        cache.update(result)
                    print(f"  пачка {n}/{len(batches)}", file=sys.stderr, flush=True)

            # что модель не перевела (или переписала по-сербски кириллицей) — ещё раз, внимательнее
            # сбои модели случайны («Рейнland»): со второй-третьей попытки текст обычно переводится.
            # если и на последней попытке перевод похож на оригинал, но чист от сербского — значит, так и есть
            for attempt in range(RETRIES):
                retry = [(key, item) for key, item in missing if key not in cache]
                if not retry:
                    break
                print(f"  повтор для {len(retry)} непереведённых, effort={RETRY_EFFORT}", file=sys.stderr, flush=True)
                for batch in (retry[i:i + RETRY_BATCH] for i in range(0, len(retry), RETRY_BATCH)):
                    result = run(batch, "retry", strict=attempt < RETRIES - 1)
                    if result:
                        cache.update(result)
        except anthropic.AuthenticationError:
            print("Перевод пропущен: ключ ANTHROPIC_API_KEY не подошёл", file=sys.stderr)

        tokens_in, tokens_out, usd = cost("new", "retry")
        left = sum(key not in cache for key, _ in missing)
        print(
            f"Перевод: {len(missing)} новых, не переведено {left}, {failed} пачек с ошибкой, "
            f"{tokens_in} + {tokens_out} токенов ≈ ${usd:.3f}",
            file=sys.stderr,
        )
        # не перевелось и с подсказкой — шлём в Telegram (через worker/, каждую новость один раз):
        # обычно это название, которое стоит добавить в glossary.json
        stuck = [{"url": item["url"], "source": source_of[key], "title": item["title"], "summary": item.get("summary", ""),
                  **rejected[key]} for key, item in missing if key not in cache and key in rejected]
        for one in stuck:
            print(f"  не перевелось: {one['title'][:80]} — {one['reason']}", file=sys.stderr)
        if stuck:
            reports_api({"items": stuck}, "untranslated")

    if unmarked and client:
        marked = 0
        for batch in (unmarked[i:i + BATCH_SIZE] for i in range(0, len(unmarked), BATCH_SIZE)):
            try:
                done = markup_batch(client, batch)
            except Exception as exc:  # noqa: BLE001 — разметим в следующую сборку
                print(f"  разметка не получена: {type(exc).__name__}: {exc}"[:200], file=sys.stderr)
                continue
            cache.update(done)
            marked += len(done)
        print(f"Разметка русскоязычных: {marked} из {len(unmarked)}", file=sys.stderr)

    before = {}  # переводы до жалобы — чтобы понять, изменила ли что-то сильная модель
    if review and client:
        before = {key: cache[key] for key, _ in review}
        print(f"\nПо жалобам читателей перевожу заново {len(review)} новостей ({REVIEW_MODEL})…", file=sys.stderr)
        for batch in (review[i:i + RETRY_BATCH] for i in range(0, len(review), RETRY_BATCH)):
            try:
                done = translate_batch(client, batch, "review")
            except Exception as exc:  # noqa: BLE001 — попробуем в следующую сборку
                print(f"  пачка не переведена: {type(exc).__name__}: {exc}"[:200], file=sys.stderr)
                continue
            cache.update({key: {**tr, "by": REVIEW_MODEL} for key, tr in done.items()})
        tokens_in, tokens_out, usd = cost("review")
        print(f"По жалобам: переведено {sum(cache[k].get('by') == REVIEW_MODEL for k, _ in review)} из {len(review)}, "
              f"{tokens_in} + {tokens_out} токенов ≈ ${usd:.3f}", file=sys.stderr)
    if USAGE:
        reports_api({"kinds": {kind: {**total, "usd": round(cost(kind)[2], 6)} for kind, total in USAGE.items()}}, "stats")
    if reported:
        report_results(reported, copies, cache, before)

    for key, items in copies.items():
        for item in items:
            if key in cache:
                item["ru"] = {k: cache[key][k] for k in ("title", "summary")}
                item["ru"]["title"] = split_live(item["title"])[0] + item["ru"]["title"]
            else:
                item.pop("ru", None)  # мог остаться в данных прошлой сборки

    judged = {id(item): cache[key] for key, items in [*copies.items(), *marks.items()] if key in cache for item in items}
    for src in sources:
        for item in src["items"]:
            lead = lead_state(item, judged.get(id(item), {}).get("lead_ok"))
            markup = [(field, judged.get(id(item), {}).get(field)) for field in MARKUP]
            for field, value in [("lead", lead), *markup]:
                if value:
                    item[field] = value
                else:
                    item.pop(field, None)

    # в кэше держим только то, что сейчас на странице, чтобы файл не рос бесконечно
    CACHE_FILE.parent.mkdir(exist_ok=True)
    CACHE_FILE.write_text(json.dumps({k: cache[k] for k in [*todo, *marks] if k in cache}, ensure_ascii=False), "utf-8")
