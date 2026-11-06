"""Мини-карты для карточек региональных групп: контур Сербии вместе с Косово и Метохией и подсвеченная область.

    uv run --with shapely python regions.py   — пишет regions.json (его читает build.py)

Границы — geoBoundaries (данные OpenStreetMap, ODbL): округа и общины Сербии, отдельно контур Косова.
Скачиваются один раз в cache/geo/. Белград ни в одну группу не входит — на карте он часть общего контура.
"""
import json
import urllib.request
from math import cos, radians
from pathlib import Path

from shapely.geometry import Polygon, shape
from shapely.ops import unary_union

ROOT = Path(__file__).parent
GEO_DIR = ROOT / "cache" / "geo"
OUT = ROOT / "regions.json"
BASE = "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/releaseData/gbOpen"
FILES = {"districts": "SRB/ADM1/geoBoundaries-SRB-ADM1.geojson",
         "municipalities": "SRB/ADM2/geoBoundaries-SRB-ADM2.geojson",
         "kosovo": "XKX/ADM0/geoBoundaries-XKX-ADM0.geojson"}

SIZE = 100  # высота карты в единицах viewBox
TOLERANCE = 0.35  # упрощение контура: на иконке 32 px это меньше трети пикселя
SANDZAK = ["Novi Pazar City", "Tutin Municipality", "Sjenica Municipality",
           "Prijepolje Municipality", "Priboj Municipality", "Nova Varos Municipality"]
# названия — как у групп в sources.json; округа — по статистическим регионам, Шумадия и Запад поделены надвое
REGIONS = {
    "Vojvodina": ["North Backa District", "West Backa District", "South Backa District", "North Banat District",
                  "Central Banat District", "South Banat District", "Syrmia District"],
    "Centralna Srbija": ["Sumadija District", "Pomoravlje District", "Rasina District", "Raska District"],
    "Zapadna Srbija": ["Macva District", "Kolubara District", "Moravica District", "Zlatibor District"],
    "Jug i istok Srbije": ["Bor District", "Branicevo District", "Zajecar District", "Jablanica District",
                           "Nisava District", "Pirot District", "Podunavlje District", "Pcinja District",
                           "Toplica District"],
}


def load(name: str) -> dict:
    path = GEO_DIR / Path(FILES[name]).name
    if not path.exists():
        GEO_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(urllib.request.urlopen(f"{BASE}/{FILES[name]}", timeout=60).read())
    return {f["properties"]["shapeName"]: shape(f["geometry"]) for f in json.loads(path.read_text("utf-8"))["features"]}


def main() -> None:
    districts, municipalities, kosovo = load("districts"), load("municipalities"), load("kosovo")
    sandzak = unary_union([municipalities[name] for name in SANDZAK])
    regions = {name: unary_union([districts[d] for d in names]).difference(sandzak) for name, names in REGIONS.items()}
    regions["Sandžak"] = sandzak
    regions["Kosovo"] = unary_union(list(kosovo.values()))
    # буфер туда-обратно закрывает щели между границами Сербии и Косова из разных наборов данных
    country = unary_union(list(districts.values()) + [regions["Kosovo"]]).buffer(0.01).buffer(-0.01)

    min_x, min_y, max_x, max_y = country.bounds
    k = cos(radians((min_y + max_y) / 2))  # долгота на широте Сербии короче широты
    scale = SIZE / (max_y - min_y)

    def path(geom) -> str:
        geom = geom.simplify(TOLERANCE / scale, preserve_topology=True)
        polygons = getattr(geom, "geoms", [geom])
        parts = []
        for polygon in polygons:
            if polygon.area * scale * scale * k < 1:  # островки меньше квадратной единицы на иконке не видны
                continue
            # щели между округами (стыки границ в OSM) — тоже мелкие дыры, их не рисуем
            holes = [r for r in polygon.interiors if Polygon(r).area * scale * scale * k >= 1]
            for ring in [polygon.exterior, *holes]:
                points = [f"{(x - min_x) * k * scale:.1f} {(max_y - y) * scale:.1f}" for x, y in ring.coords[:-1]]
                parts.append("M" + "L".join(points) + "Z")
        return "".join(parts)

    width = (max_x - min_x) * k * scale
    data = {"viewBox": f"0 0 {width:.1f} {SIZE}", "country": path(country),
            "regions": {name: path(geom) for name, geom in regions.items()}}
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(f"{OUT.name}: {OUT.stat().st_size} байт, viewBox {data['viewBox']}")


if __name__ == "__main__":
    main()
