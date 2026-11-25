"""Текст статьи для «сути одной фразой» (essences.py): только сама новость, именно та, что в ленте, без хвостов.
Проверено 25.11 на 195 статьях 65 изданий: 185 прошли проверку «это та статья». Не выходит у Balkan Insight (платный
доступ), у ссылок Google News (KoSSev, Kruševac Grad) и у Kosovo Online (отвечает 403).
Порядок: trafilatura по очищенному DOM (корень — у h1 с заголовком из ленты, без чужих <article>, блоков «по теме»,
комментариев) и JSON-LD articleBody — из прошедших проверку берём более длинный; потом тот же DOM в режиме recall,
потом вся страница. Берём первый вариант, прошедший проверку «это та статья». Потом чистим строки и режем хвост."""
import html as html_mod, json, re, unicodedata
import lxml.html, trafilatura
from trafilatura.utils import decode_file

CAP = 8000

CYR = dict(zip("абвгдђежзијклљмнњопрстћуфхцчџш", ["a", "b", "v", "g", "d", "dj", "e", "z", "z", "i", "j", "k", "l", "lj", "m", "n", "nj",
                                                  "o", "p", "r", "s", "t", "c", "u", "f", "h", "c", "c", "dz", "s"]))
WORD = re.compile(r"[a-z0-9]{4,}")


def fold(s: str) -> str:
    """Кириллица → латиница, без диакритики, нижний регистр — чтобы сравнивать тексты в любой азбуке."""
    s = "".join(CYR.get(ch, ch) for ch in s.lower()).replace("đ", "dj")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def words(s: str) -> set:
    """Основы слов: первые 5 букв — падежные окончания не мешают сравнению."""
    return {w[:5] for w in WORD.findall(fold(s))}


# блоки, которые не бывают текстом статьи: по классу/id
JUNK_ATTR = re.compile(r"comment|komentar|related|povezan|recommend|preporu|share|social|newsletter|banner|promo|advert|reklam|"
                       r"most-?read|najcitan|najnovij|popular|tags?\b|breadcrumb|sidebar|footer|author-box|read-?more|procitaj|"
                       r"ostale|izdvoj|widget|gallery-thumbs|cookie|consent|subscribe|paywall", re.I)


def clean_dom(raw: bytes, title: str, root: bool = True):
    html = decode_file(raw)
    doc = lxml.html.fromstring(html)
    for el in doc.xpath("//script|//style|//noscript|//iframe"):
        el.drop_tree()
    # меню, подвал, формы. Битая разметка (Nova) заворачивает в <nav> всю страницу — такой блок не трогаем
    all_p = sum(len(x.text_content()) for x in doc.xpath("//p")) or 1
    for el in doc.xpath("//aside|//nav|//footer|//form"):
        if el.getparent() is not None and sum(len(x.text_content()) for x in el.xpath(".//p")) < 0.3 * all_p:
            el.drop_tree()
    tw = words(title)
    # заголовок статьи: h1, в котором больше всего слов из заголовка ленты
    h1s = sorted(doc.xpath("//h1"), key=lambda h: -len(tw & words(h.text_content())))
    h1 = h1s[0] if h1s and tw and len(tw & words(h1s[0].text_content())) >= max(1, len(tw) // 3) else None
    own = set(h1.iterancestors()) if h1 is not None else set()
    # корень статьи — ближайший к h1 предок, в котором уже есть текст в абзацах. На страницах с бесконечной лентой
    # (Nova) следом идут целые чужие статьи — они остаются за пределами корня
    if root and h1 is not None:
        for anc in h1.iterancestors():
            if sum(len(x.text_content()) for x in anc.xpath(".//p")) >= 400:
                doc = anc
                break
    # чужие <article> — карточки других новостей: короткие и не содержат h1
    for a in doc.xpath(".//article"):
        if a not in own and sum(len(x.text_content()) for x in a.xpath(".//p")) < 600:
            a.drop_tree()
    body_len = len(doc.text_content()) or 1
    p_len = sum(len(x.text_content()) for x in doc.xpath(".//p")) or 1
    for el in doc.xpath(".//*[@class or @id]"):
        if el in own or el.getparent() is None or el.tag in ("body", "html", "main"):
            continue
        # класс страницы вроде "single-post comments-open tag-…" — это обёртка всей статьи, а не блок комментариев
        if JUNK_ATTR.search((el.get("class") or "") + " " + (el.get("id") or "")) and len(el.text_content()) < 0.4 * body_len \
                and sum(len(x.text_content()) for x in el.xpath(".//p")) < 0.3 * p_len:
            el.drop_tree()
    return doc, h1


def from_jsonld(raw: bytes) -> str:
    html = decode_file(raw)
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack += d
            elif isinstance(d, dict):
                if isinstance(d.get("articleBody"), str) and len(d["articleBody"]) > 200:
                    body = lxml.html.fromstring(d["articleBody"]).text_content() if "<" in d["articleBody"] else d["articleBody"]
                    while (u := html_mod.unescape(body)) != body:
                        body = u
                    return re.sub(r"\n\s*\n+", "\n", body).strip()
                stack += [v for v in d.values() if isinstance(v, (dict, list))]
    return ""


# строки-мусор: выкидываем саму строку
DROP = re.compile(r"^(foto|photo|izvor|video|autor|funkcija|objavljeno|vreme citanja|podeli|share|tagovi|tags|"
                  r"preuzmite|pratite nas|dodaj .*google|postavi .*google|\(?\d{1,2}[./ ]\s*\w+[./ ]?\s*\d{4}|\d{1,2}:\d{2}$)", re.I)
INLINE_DROP = re.compile(r"prikazi ovu objavu u aplikaciji|view this post on instagram|kolacic|prihvatite i nastavite|dozvolite sadrzaj|preskocite .*sadrzaj|kraj sadrzaja sa", re.I)
# с этой строки начинается хвост: режем её и всё дальше (если она не в самом начале текста)
STOP = re.compile(r"^(komentar|ostale teme|procitajte (jos|i)|povezane|vise (vesti|iz)|najnovije|najcitanije|bonus video|"
                  r"sadrzaj objavljenih komentara|vas komentar|komentar uspesno|this site is protected|arhiva nedeljnika|"
                  r"srodni link|preporucujemo|izdvajamo|pogledajte i|ukoliko zelite da|pravila komentarisanja|"
                  r"pretplatite se|subscribe to|procitajte vise|vise o .* procitajte|ceo izvestaj procitajte)", re.I)
# «Добавьте N1 в Google»: у Nova эта фраза в конце склеенной шапки — шапку отрезаем вместе с ней
GOOGLE = re.compile(r"^.*?(dodajte?|postavi(te)?) [^.]{0,40}google izvor(a|u)?( vise)?\s*", re.I)


TAIL = re.compile(
    # слова «komentar», «donacija» бывают и в самой новости — поэтому только целые служебные фразы
    r"podrzite nas|(jednokratnom|mesecnom) donacij|doniraj|govor mrznje|^(komentari|ostavite komentar|posalji komentar)|"
    r"ucestvuj u diskusiji|slanjem komentara|pravila komentarisanja|opsirnije u videu|pogledajte (video|galerij)|video ?:$|"
    r"pretplatite se|pretplata|newsletter|zadrzava(mo)? pravo|^pratite nas|vise informacija iz ovog grada|"
    r"jos vesti iz|zaprati .*google|google news platform|klikni na zvezdicu|^pogledaj sve$|zabranjeno je ucestvovanje maloletnih|"
    r"igre na srecu mogu|uz obavezno navodjenje izvora|preuzimanje (sadrzaja|teksta)|sva prava zadrzana|copyright|"
    r"oglasi za posao|radno mesto mozete oglasiti|potrebu za radnom snagom|oglasi radno mesto|videa? na linku:?$|^- (biografija|latest posts)$|"
    r"^[A-ZČĆŽŠĐ ,]{6,}$|^\W*\w+(\.\w+)?\W*$", re.I)


def tidy(text: str, title: str) -> str:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    out = []
    total = sum(len(l) for l in lines) or 1
    seen = 0
    tw = words(title)
    for i, l in enumerate(lines):
        l = re.sub(r"\s*BONUS VIDEO:?\s*$", "", l, flags=re.I)
        l = re.sub(r"\s*View this post on Instagram.*?A post shared by [^)]*\)\s*", " ", l).strip()
        l = re.sub(r"\s*(A post shared by|Pogledajte ovu objavu na Instagramu).*$", "", l).strip()
        if not l:
            continue
        f = fold(l)
        if (m := GOOGLE.match(f)):
            l = l[len(m.group(0)):].strip() if len(m.group(0)) < 250 else l
            f = fold(l)
            if not l:
                continue
        # подпись к фото («Foto: Tanjug/AP», «Gradilište, foto: Srđan Ilić»)
        if len(l) < 100 and re.search(r"\bfoto\s*:", f):
            continue
        # «| Izvor: Beta»
        if len(l) < 60 and re.match(r"^\W*(izvor|autor)\s*:", f):
            continue
        seen += len(l)
        if STOP.match(f):
            if seen / total > 0.25:
                break
            continue
        if DROP.match(f) and len(l) < 120 or INLINE_DROP.search(f):
            continue
        # заголовок, повторённый первой строкой
        if not out and tw and len(words(l)) <= len(tw) + 3 and len(tw & words(l)) >= 0.7 * len(tw):
            continue
        out.append(l)
    # хвост: правила комментирования, просьбы о донациях, «смотрите видео» — снимаем с конца, пока такие строки
    # шапка: рубрика («Vesti», «Klimatske promene»), дата со временем (RTS), «© Sputnik / …». Город заглавными (JUGpress) —
    # это место события, его оставляем
    def header(l):
        return len(l) < 50 and (l.startswith("©") or re.search(r"\d{1,2}\.\d{1,2}\.\d{4}.*\d{1,2}:\d{2}", l)
                                or (len(l.split()) <= 3 and l[-1] not in ".!?:\"”»" and not l.isupper()))

    # встроенная карточка другой новости посреди текста: строка-анонс + рубрика («Vesti») — убираем обе,
    # если анонс не про эту новость
    def rubric(l):
        return len(l.split()) <= 2 and l[-1] not in ".!?:\"”»" and not l.isupper() and not l.startswith("- ")

    i = 0
    while i < len(out):
        if rubric(out[i]):
            if i > 0 and len(words(out[i - 1]) & tw) < 2:
                del out[i - 1:i + 1]
                i -= 1
            else:
                del out[i]
            continue
        i += 1

    while out and header(out[0]):
        out.pop(0)

    def signature(l):
        f = fold(l)
        return len(l) < 50 and not l.startswith("- ") and (
            l.startswith("(") or re.match(r"^(pise|tekst|autor|izvor|foto)\s*:", f) or "/" in l
            or re.search(r"\.(net|rs|com)\b", f) or re.fullmatch(r"[\w .]+, \d{1,2}\.\d{1,2}\.\d{4}\.?", l))

    while out and (TAIL.search(fold(out[-1])) or signature(out[-1])):
        out.pop()
    text = "\n".join(out)
    if len(text) > CAP:
        cut = text.rfind("\n", 0, CAP)
        text = text[: cut if cut > CAP // 2 else CAP]
    return text


def page_title(raw: bytes) -> str:
    h = decode_file(raw)
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)', h) or re.search(r"<h1[^>]*>(.*?)</h1>", h, re.S)
    return html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""


def matches(text: str, title: str, summary: str) -> tuple:
    """Та ли это статья: слова заголовка в тексте, а если в ленте есть подводка — её слова в начале текста."""
    tw, sw, all_w = words(title), words(summary), words(text)
    t_hit = len(tw & words(text[:3000])) / len(tw) if tw else 0
    s_hit = len(sw & all_w) / len(sw) if len(sw) >= 6 else None
    lines = text.split("\n")
    # меню — короткие строки подряд; списки («- deo Takovske;») меню не считаем
    prose = sum(len(l) for l in lines if len(l) >= 60 or l.startswith(("- ", "• ", "– "))) / (len(text) or 1)
    ok = len(text) >= 250 and prose >= 0.6 and (t_hit >= (0.5 if s_hit is not None else 0.35) or (s_hit or 0) >= 0.5)
    return ok, round(t_hit, 2), s_hit if s_hit is None else round(s_hit, 2)


def restore_head(rec: dict, tries: list, title: str, summary: str = "") -> dict:
    """Точный режим trafilatura иногда теряет первый абзац (Nova). Режим «шире» по тому же корню его видит:
    если в нём перед первой строкой точного текста есть 1–2 полноценных абзаца — ставим их в начало."""
    if rec["how"] != "dom":
        return rec
    recall = next((t for h, t in tries if h == "dom_recall"), "")
    lines = [l.strip() for l in tidy(recall, title).split("\n") if l.strip()]
    first = rec["text"].split("\n")[0][:80]
    pos = next((i for i, l in enumerate(lines) if first and first in l), None)
    if not pos:
        return rec
    tw, topic = words(title), words(title) | words(summary)

    def good(l):
        lw = words(l)
        return (len(l) >= 80 and (l[0].isupper() or l[0] in "\"„“«") and l[-1] in ".!?\"”“»" and not TAIL.search(fold(l)) and l not in rec["text"]
                and len(lw & tw) < 0.6 * len(tw)        # не заголовок
                and len(lw & topic) >= 2)               # про эту же новость, а не соседняя строка

    head = [l for l in lines[max(0, pos - 2):pos] if good(l)]
    return {**rec, "text": "\n".join(head + [rec["text"]]), "head_restored": len(head)} if head else rec


def extract(raw: bytes, title: str, summary: str) -> dict:
    tries = []
    try:
        doc, h1 = clean_dom(raw, title)
        cleaned = lxml.html.tostring(doc, encoding="unicode")
        tries.append(("dom", trafilatura.extract(cleaned, favor_precision=True, include_comments=False, include_tables=False) or ""))
        # JSON-LD — запасной: у Newsmax там пересказ вместо статьи, у B92 абзацы склеены
        if (body := from_jsonld(raw)):
            tries.append(("jsonld", body))
        tries.append(("dom_recall", trafilatura.extract(cleaned, favor_recall=True, include_comments=False, include_tables=False) or ""))
    except Exception:
        pass
    # вся страница, но без карточек чужих новостей и служебных блоков
    try:
        page = lxml.html.tostring(clean_dom(raw, title, root=False)[0], encoding="unicode")
    except Exception:
        page = raw
    tries.append(("page", trafilatura.extract(page, favor_precision=True, include_comments=False) or ""))
    tries.append(("page_recall", trafilatura.extract(page, favor_recall=True, include_comments=False) or ""))
    best, first = None, []
    for how, t in tries:
        t = tidy(t, title).strip()
        ok, th, sh = matches(t, title, summary)
        rec = {"how": how, "ok": ok, "title_hit": th, "summary_hit": sh, "text": t}
        # DOM и JSON-LD — равноправные: берём более длинный из прошедших (у Newsmax JSON-LD — пересказ,
        # у B92 DOM теряет текст, а в JSON-LD он есть)
        if how in ("dom", "jsonld"):
            if ok:
                first.append(rec)
            if how == "jsonld" or "jsonld" not in [h for h, _ in tries]:
                if first:
                    return restore_head(max(first, key=lambda r: len(r["text"])), tries, title, summary)
        elif ok:
            return rec
        if len(t) >= 250 and (best is None or th + (sh or 0) > best["title_hit"] + (best["summary_hit"] or 0)):
            best = rec
    if best is None:
        return {"how": "none", "ok": False, "text": "", "title_hit": 0, "summary_hit": None}
    # статья та же по адресу, но сайт её переписал: заголовок страницы уже другой — сверяем с ним
    pt = page_title(raw)
    if pt and matches(best["text"], pt, "")[0]:
        return {**best, "ok": True, "updated": pt}
    return {**best, "ok": False}
