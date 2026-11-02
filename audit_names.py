#!/usr/bin/env python3
"""Ищет в готовых переводах названия, которые модель перевела по смыслу, хотя их надо было
транслитерировать: «Почтовая сберегательная касса» вместо «Поштанска штедионица».

Читает site/data.json (то, что сейчас на странице), отдаёт модели пары «оригинал → перевод»
и печатает найденное — кандидатов в glossary.json. Сам глоссарий не меняет: решать вам.
Запуск: .venv/bin/python audit_names.py   (стоит около цента)
"""

import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

from translate import MODEL, load_glossary

ROOT = Path(__file__).parent
BATCH = 100

PROMPT = """Ниже пары: заголовок сербской новости и его перевод на русский.

Найди собственные названия, которые в переводе переведены по смыслу, хотя их надо было передать русскими буквами, как имя: названия компаний, банков, спортивных клубов, брендов, магазинов, заведений, фестивалей и мероприятий. Пример: Poštanska štedionica → надо «Поштанска штедионица», а не «Почтовая сберегательная касса»; Crvena zvezda → «Црвена звезда», а не «Красная звезда».

Не включай:
- государственные органы, суды, министерства и учреждения — у них есть привычный русский перевод (Народная скупщина, Высший суд, Национальный банк Сербии);
- политические партии, списки и движения — их принято переводить (Сербская прогрессивная партия, «Студенческий список»);
- географические названия и имена людей;
- названия, которые переданы правильно.

Для каждого найденного названия укажи: как оно в оригинале (в именительном падеже), как его перевели и как надо."""

SCHEMA = {
    "type": "object",
    "properties": {
        "names": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sr": {"type": "string"},
                    "translated_as": {"type": "string"},
                    "should_be": {"type": "string"},
                },
                "required": ["sr", "translated_as", "should_be"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["names"],
    "additionalProperties": False,
}


def audit(client, pairs: list[tuple[str, str]]) -> list[dict]:
    text = "\n".join(f"{sr}\n→ {ru}\n" for sr, ru in pairs)
    response = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": PROMPT + "\n\n" + text}],
    )
    return json.loads(next(b.text for b in response.content if b.type == "text"))["names"]


def main() -> int:
    data = json.loads((ROOT / "site" / "data.json").read_text("utf-8"))
    pairs = sorted({(i["title"], i["ru"]["title"]) for s in data["sources"] for i in s["items"] if i.get("ru")})
    known = {entry["sr"].lower() for entry in load_glossary()}
    client = anthropic.Anthropic()
    with ThreadPoolExecutor(4) as pool:
        found = [n for names in pool.map(lambda b: audit(client, b), (pairs[i:i + BATCH] for i in range(0, len(pairs), BATCH))) for n in names]
    counts = Counter(n["sr"] for n in found)
    shown = set()
    print(f"Проверено переводов: {len(pairs)}\n")
    for n in sorted(found, key=lambda n: -counts[n["sr"]]):
        if n["sr"] in shown or n["sr"].lower() in known:
            continue
        shown.add(n["sr"])
        print(f'{counts[n["sr"]]:3d}×  {n["sr"]}: «{n["translated_as"]}» → {n["should_be"]}')
    if not shown:
        print("Переведённых по смыслу названий не нашлось.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
