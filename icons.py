"""Иконки изданий для карточек: оригинальная квадратная картинка издания → static/icons/<домен>.png (128×128).

    .venv/bin/python icons.py            — скачать иконки, которых ещё нет
    .venv/bin/python icons.py --force    — скачать все заново
    .venv/bin/python icons.py n1info.rs  — только эти домены (заново)

Запускается вручную, когда добавляем источник; иконки лежат в git. Картинку только уменьшаем — не перекрашиваем
и не дорисовываем: углы скругляет страница. У кого иконки нет, страница берёт её у Google, как раньше
(favicon в template.html). Нужен Pillow: uv pip install --python .venv/bin/python pillow
"""
import io
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

ROOT = Path(__file__).parent
ICONS_DIR = ROOT / "static" / "icons"
SIZE = 128  # на странице иконка 32×32, на ретине нужно 64; запас — на будущее
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

# Откуда берём иконку. Лучше всего иконка приложения в App Store (номер приложения): квадрат во всю площадь,
# без своих скруглений. Нет приложения — аватарка канала на YouTube, а нет и её — картинка с сайта или из X.
ICONS = {
    "n1info.rs": "appstore:920779491",
    "nova.rs": "appstore:1500012231",
    "www.danas.rs": "appstore:1344184178",
    "radar.nova.rs": "https://radar.nova.rs/wp-content/uploads/fbrfg/android-chrome-512x512.png",
    "www.vreme.com": "youtube:channel/UCGd-RXHkOoJwSdKM0GQE-Sw",
    "insajder.net": "appstore:1297639983",
    "serbiantimes.info": "youtube:channel/UCPvJNYBK2I7uXH7e5-STVAg",
    "www.nin.rs": "youtube:@NedeljnikNIN",
    "www.nedeljnik.rs": "appstore:1182491047",
    "www.masina.rs": "youtube:channel/UCUHTXIkLVSVHMBFYaiUMITA",
    "beta.rs": "youtube:user/betanewsagency",
    "www.krik.rs": "appstore:1585641404",
    "www.cins.rs": "youtube:user/CINSrbija",
    "raskrikavanje.rs": "youtube:@raskrikavanje",
    "www.istinomer.rs": "youtube:user/Istinomer",
    "www.cenzolovka.rs": "https://unavatar.io/x/cenzolovka?fallback=false",  # unavatar быстро упирается в лимит
    "pescanik.net": "youtube:user/pescanik",
    "www.rts.rs": "appstore:1292362685",
    "www.tanjug.rs": "appstore:619825419",
    "www.euronews.rs": "appstore:1582475666",
    "newsmaxbalkans.com": "appstore:6651861538",
    "mondo.rs": "appstore:673090202",
    "www.srbija.gov.rs": "https://www.srbija.gov.rs/img/logo_60x120-2.webp",  # герб, узкий — страница вписывает
    "www.blic.rs": "appstore:518272723",
    "www.politika.rs": "appstore:1121964370",
    "www.telegraf.rs": "appstore:548251852",
    "www.dnevnik.rs": "youtube:user/dnevnikonline",
    "www.informer.rs": "appstore:972983596",
    "www.republika.rs": "appstore:1305054259",
    "www.novosti.rs": "appstore:911283260",
    "www.alo.rs": "appstore:1452042063",
    "www.kurir.rs": "appstore:572918944",
    "pink.rs": "appstore:1069323902",
    "www.b92.net": "appstore:513918593",
    "www.espreso.co.rs": "appstore:1148226620",
    "www.srbijadanas.com": "appstore:905741898",
    "novaekonomija.rs": "appstore:1364517600",
    "biznis.rs": "https://biznis.rs/wp-content/uploads/2022/09/cropped-biznisRS-favicon-192x192.png",
    "balkaninsight.com": "youtube:channel/UCd33KZF7ikDr7dICyNt7LVA",
    "www.slobodnaevropa.org": "appstore:475986784",  # приложение RFE/RL, общее для всех служб
    "www.bbc.com": "appstore:364147881",
    "www.dw.com": "youtube:@dwsrpski",  # у иконки приложения DW свои скругления со светлым ободком
    "sputnikportal.rs": "https://cdn1.img.sputnikportal.rs/i/favicon/favicon-192x192.png",
    "www.reddit.com": "appstore:1064216828",
}


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def image_url(spec: str) -> str:
    kind, _, value = spec.partition(":")
    if kind == "appstore":
        app = json.loads(fetch(f"https://itunes.apple.com/lookup?id={value}&country=rs"))["results"][0]
        return app["artworkUrl512"]
    if kind == "youtube":
        page = fetch(f"https://www.youtube.com/{value}").decode("utf-8", "replace")
        return re.search(r'<meta property="og:image" content="([^"]+)"', page).group(1)
    return spec


def save_icon(host: str) -> str:
    image = Image.open(io.BytesIO(fetch(image_url(ICONS[host]))))
    image = image.convert("RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB")
    image.thumbnail((SIZE, SIZE), Image.LANCZOS)  # только уменьшаем; неквадратную страница вписывает сама
    for old in ICONS_DIR.glob(f"{host}.*"):
        old.unlink()
    image.save(ICONS_DIR / f"{host}.png", optimize=True)
    return f"{image.width}×{image.height}"


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv or bool(args)
    config = json.loads((ROOT / "sources.json").read_text("utf-8"))
    # в разделах с группами (региональные) карточка на группу, и у неё вместо иконки карта
    hosts = {urlparse(s["site"]).hostname for sec in config["sections"] if not sec.get("groups") for s in sec["sources"]}
    for host in sorted(hosts):
        if args and host not in args:
            continue
        if host not in ICONS:
            if not host.endswith("google.com"):
                print(f"{host:28} нет в ICONS — будет иконка Google")
            continue
        if not force and any(ICONS_DIR.glob(f"{host}.*")):
            continue
        try:
            print(f"{host:28} {save_icon(host)}")
        except Exception as exc:  # noqa: BLE001
            print(f"{host:28} не скачалась: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
