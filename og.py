"""Картинки-превью для соцсетей и мессенджеров, по одной на язык → static/og.png, static/og-ru.png (1200×630).

    .venv/bin/python og.py

Запускается вручную, когда меняются названия разделов, их цвета или иконка сайта; картинки лежат в git.
Какая картинка у какой версии сайта — PAGES[...]["og_image"] в build.py. Названия разделов берём из
sources.json, цвета — из template.html (--c-<раздел>), иконку — из static/apple-touch-icon.png, так что после
переименования достаточно перезапустить скрипт. Шрифт Nunito, как на сайте: скачивается в cache/ при первом
запуске. Нужен Pillow (см. icons.py). После выкладки превью в Telegram обновить: отправить обе ссылки
(vestomat.rs и vestomat.rs/ru) боту @WebpageBot.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from build import PAGES

ROOT = Path(__file__).parent
ICON = ROOT / "static" / "apple-touch-icon.png"
FONT = ROOT / "cache" / "Nunito.ttf"
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/nunito/Nunito%5Bwght%5D.ttf"
W, H = 1200, 630
X = 90  # левое поле; правое такое же

# Подпись под названием и разделы в легенде. Все разделы не влезают: мельче 32px подписи не читаются в маленьком
# превью, а кириллица в Nunito шире латиницы — по-русски помещаются только три. Подпись раздела — его название
# с маленькой буквы (title или title_ru), если в short не задана короче.
TEXTS = {
    "sr": {
        "lines": ["Srbija: vesti iz svih medija", "Naslovi svih izvora na jednom mestu"],
        "title_key": "title",
        "chips": ["independent", "progov", "state", "foreign-sr"],
        "short": {"foreign-sr": "strani mediji"},
    },
    "ru": {
        "lines": ["Сербия: новости на русском", "Заголовки переводит ИИ"],
        "title_key": "title_ru",
        "chips": ["independent", "progov", "state"],
        "short": {},
    },
}


def font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT), size)
    f.set_variation_by_axes([weight])
    return f


def chips(text: dict) -> list[tuple[str, str]]:
    sections = json.loads((ROOT / "sources.json").read_text("utf-8"))["sections"]
    titles = {s["id"]: s[text["title_key"]] for s in sections}
    colors = dict(re.findall(r"--c-([\w-]+):\s*(#[0-9a-fA-F]{6})", (ROOT / "template.html").read_text("utf-8")))
    return [(colors[i], text["short"].get(i) or titles[i][0].lower() + titles[i][1:]) for i in text["chips"]]


def fits(right: float, what: str) -> None:
    if right > W - X:
        sys.exit(f"{what} шире картинки на {round(right - (W - X))}px")


def render(lang: str, icon: Image.Image) -> Path:
    text = TEXTS[lang]
    img = Image.new("RGBA", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    img.alpha_composite(icon, (X, 99))
    d.text((X + 126, 144), "Vestomat", font=font(116, 900), fill="#1f1f1f", anchor="lm")
    for (size, color, y), line in zip([(56, "#333333", 270), (40, "#6b6b6b", 350)], text["lines"]):
        fits(X + d.textlength(line, font=font(size, 700)), f"«{line}»")
        d.text((X, y), line, font=font(size, 700), fill=color)

    # разделы точками, как на сайте
    f = font(32, 700)
    cx, cy = X, 470
    for color, label in chips(text):
        d.ellipse((cx, cy - 9, cx + 18, cy + 9), fill=color)
        d.text((cx + 28, cy), label, font=f, fill="#6b6b6b", anchor="lm")
        cx += 28 + d.textlength(label, font=f) + 38
    fits(cx - 38, f"Легенда ({lang}): убрать раздел из chips или задать подпись в short —")

    d.text((X, 560), ("vestomat.rs/" + PAGES[lang]["path"]).rstrip("/"), font=font(36, 800), fill="#2563eb", anchor="lm")
    d.rectangle((0, H - 10, W, H), fill="#c6363c")
    d.rectangle((0, H - 6, W, H - 3), fill="#0c4076")
    out = ROOT / "static" / PAGES[lang]["og_image"]
    img.convert("RGB").save(out, optimize=True)
    return out


def main() -> None:
    if not FONT.exists():
        FONT.parent.mkdir(exist_ok=True)
        urllib.request.urlretrieve(FONT_URL, FONT)
    # иконка квадратная (для iPhone), скругляем как в icon.svg: радиус 8 из 32
    icon = Image.open(ICON).convert("RGBA").resize((86, 86), Image.LANCZOS)
    mask = Image.new("L", (344, 344), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 343, 343), radius=86, fill=255)
    icon.putalpha(mask.resize((86, 86), Image.LANCZOS))
    for lang in TEXTS:
        print(render(lang, icon))


if __name__ == "__main__":
    main()
