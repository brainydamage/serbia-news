"""Картинка-превью для соцсетей и мессенджеров → static/og.png (1200×630).

    .venv/bin/python og.py

Запускается вручную, когда меняются названия разделов, их цвета или иконка сайта; картинка лежит в git.
Названия разделов берём из sources.json, цвета — из template.html (--c-<раздел>), иконку — из
static/apple-touch-icon.png, так что после переименования достаточно перезапустить скрипт.
Шрифт Nunito, как на сайте: скачивается в cache/ при первом запуске. Нужен Pillow (см. icons.py).
После выкладки превью в Telegram обновить: отправить ссылку боту @WebpageBot.
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent
OUT = ROOT / "static" / "og.png"
ICON = ROOT / "static" / "apple-touch-icon.png"
FONT = ROOT / "cache" / "Nunito.ttf"
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/nunito/Nunito%5Bwght%5D.ttf"
W, H = 1200, 630
X = 90  # левое поле; правое такое же

# Разделы в легенде. Все не влезают: полное «Kritični prema SNS-u» длинное, а мельче 32px подписи не читаются
# в маленьком превью. Подпись — название раздела с маленькой буквы, если не задана короче.
CHIPS = ["independent", "progov", "state", "foreign-sr"]
SHORT = {"foreign-sr": "strani mediji"}


def font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT), size)
    f.set_variation_by_axes([weight])
    return f


def chips() -> list[tuple[str, str]]:
    titles = {s["id"]: s["title"] for s in json.loads((ROOT / "sources.json").read_text("utf-8"))["sections"]}
    colors = dict(re.findall(r"--c-([\w-]+):\s*(#[0-9a-fA-F]{6})", (ROOT / "template.html").read_text("utf-8")))
    return [(colors[i], SHORT.get(i) or titles[i][0].lower() + titles[i][1:]) for i in CHIPS]


def main() -> None:
    if not FONT.exists():
        FONT.parent.mkdir(exist_ok=True)
        urllib.request.urlretrieve(FONT_URL, FONT)

    img = Image.new("RGBA", (W, H), "#ffffff")
    d = ImageDraw.Draw(img)
    # иконка квадратная (для iPhone), скругляем как в icon.svg: радиус 8 из 32
    icon = Image.open(ICON).convert("RGBA").resize((86, 86), Image.LANCZOS)
    mask = Image.new("L", (344, 344), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, 343, 343), radius=86, fill=255)
    icon.putalpha(mask.resize((86, 86), Image.LANCZOS))
    img.alpha_composite(icon, (X, 99))
    d.text((X + 126, 144), "Vestomat", font=font(116, 900), fill="#1f1f1f", anchor="lm")
    d.text((X, 270), "Srbija: vesti", font=font(56, 700), fill="#333333")
    d.text((X, 340), "Сербия: новости на русском", font=font(56, 700), fill="#333333")

    # разделы точками, как на сайте
    f = font(32, 700)
    cx, cy = X, 470
    for color, label in chips():
        d.ellipse((cx, cy - 9, cx + 18, cy + 9), fill=color)
        d.text((cx + 28, cy), label, font=f, fill="#6b6b6b", anchor="lm")
        cx += 28 + d.textlength(label, font=f) + 38
    if cx - 38 > W - X:
        sys.exit(f"Легенда шире картинки на {round(cx - 38 - (W - X))}px: убрать раздел из CHIPS или задать подпись в SHORT")

    d.text((X, 560), "vestomat.rs", font=font(36, 800), fill="#2563eb", anchor="lm")
    d.rectangle((0, H - 10, W, H), fill="#c6363c")
    d.rectangle((0, H - 6, W, H - 3), fill="#0c4076")
    img.convert("RGB").save(OUT, optimize=True)
    print(OUT)


if __name__ == "__main__":
    main()
