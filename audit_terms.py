#!/usr/bin/env python3
"""Собирает из готовых переводов учреждения, партии, госкомпании и прочие повторяющиеся термины
и показывает, как модель их перевела: где верно, где по-разному, где с ошибкой.

Читает site/data.json (то, что сейчас на странице), отдаёт модели пары «оригинал → перевод»
и пишет кандидатов в glossary.json — в консоль и в audit_terms.json. Сам глоссарий не меняет: решать вам.
Запуск: .venv/bin/python audit_terms.py   (Sonnet, около $0.5 за прогон)
"""

import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import anthropic

from translate import load_glossary

ROOT = Path(__file__).parent
OUT_FILE = ROOT / "audit_terms.json"
# проверять переводы дешёвой модели должна модель посильнее
MODEL = "claude-sonnet-5-5"
BATCH = 80

PROMPT = """Ниже пары: сербская новость (заголовок и подводка) и её перевод на русский.

Выпиши из них все термины, которые стоит один раз закрепить в словаре переводчика, чтобы они всегда переводились одинаково и правильно:
- государственные органы и учреждения: суды, прокуратуры, министерства, агентства, советы, комиссии, управления, полиция, армия, местная власть (VJT, Viši sud, MUP, RIK, REM, Poreska uprava, Gradska uprava);
- политические партии, коалиции, движения и списки (SNS, SPS, Kreni-Promeni, Studentska lista);
- государственные и крупные компании (EPS, NIS, Srbijagas, Železnice Srbije);
- топонимы, которые по-русски пишут не так, как кажется (Novi Sad — Нови-Сад, Kosovska Mitrovica — Косовска-Митровица, CG — Черногория);
- устойчивые общественно-политические понятия и юридические термины, которые легко перевести калькой (zbor građana, blokada, pritvor, krivična prijava, vanredni izbori).

Не выписывай имена людей, названия СМИ, обычные слова и разовые названия, которые вряд ли встретятся снова.

Для каждого термина укажи:
- sr — как он пишется в оригинале, в именительном падеже, латиницей; аббревиатуру — как в тексте (VJT, SNS);
- category — одно из: institution, party, company, place, term;
- translated_as — как его перевели в этих парах (если по-разному — все варианты через « | »);
- should_be — как его принято писать в русских СМИ; для сербских аббревиатур — расшифровку по-русски и, если её принято оставлять, саму аббревиатуру в скобках;
- status — ok (переведено верно), inconsistent (переведено по-разному), wrong (переведено неверно или калькой)."""

SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sr": {"type": "string"},
                    "category": {"type": "string", "enum": ["institution", "party", "company", "place", "term"]},
                    "translated_as": {"type": "string"},
                    "should_be": {"type": "string"},
                    "status": {"type": "string", "enum": ["ok", "inconsistent", "wrong"]},
                },
                "required": ["sr", "category", "translated_as", "should_be", "status"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["terms"],
    "additionalProperties": False,
}


def audit(client, items: list[dict]) -> tuple[list[dict], int, int]:
    text = "\n".join(
        f'{i["title"]}\n{i.get("summary", "")}\n→ {i["ru"]["title"]}\n{i["ru"].get("summary", "")}\n' for i in items
    )
    with client.messages.stream(
        model=MODEL,
        max_tokens=32000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": PROMPT + "\n\n" + text}],
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason in ("max_tokens", "refusal"):
        print(f"  пачка пропущена: stop_reason={response.stop_reason}", file=sys.stderr)
        return [], response.usage.input_tokens, response.usage.output_tokens
    terms = json.loads(next(b.text for b in response.content if b.type == "text"))["terms"]
    return terms, response.usage.input_tokens, response.usage.output_tokens


def main() -> int:
    data = json.loads((ROOT / "site" / "data.json").read_text("utf-8"))
    seen, items = set(), []
    for src in data["sources"]:
        for item in src["items"]:
            if item.get("ru") and item["title"] not in seen:
                seen.add(item["title"])
                items.append(item)
    known = {entry["sr"].lower() for entry in load_glossary()}
    client = anthropic.Anthropic()
    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]
    print(f"Проверяю {len(items)} переводов ({len(batches)} пачек)…", file=sys.stderr)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda b: audit(client, b), batches))

    # один термин из разных пачек: сводим варианты перевода, худший статус и сколько раз встретился
    rank = {"ok": 0, "inconsistent": 1, "wrong": 2}
    merged = defaultdict(lambda: {"count": 0, "translated_as": set(), "status": "ok"})
    for terms, _, _ in results:
        for t in terms:
            if t["sr"].lower() in known:
                continue
            m = merged[t["sr"]]
            m["count"] += 1
            m["category"] = t["category"]
            m["should_be"] = t["should_be"]
            m["translated_as"].update(v.strip() for v in t["translated_as"].split("|") if v.strip())
            if rank[t["status"]] > rank[m["status"]]:
                m["status"] = t["status"]
    for m in merged.values():
        if len(m["translated_as"]) > 1 and m["status"] == "ok":
            m["status"] = "inconsistent"
        m["translated_as"] = sorted(m["translated_as"])

    rows = sorted(({"sr": sr, **m} for sr, m in merged.items()), key=lambda r: (-rank[r["status"]], r["category"], -r["count"]))
    OUT_FILE.write_text(json.dumps(rows, ensure_ascii=False, indent=1), "utf-8")
    for r in rows:
        print(f'{r["status"]:12s} {r["category"]:11s} {r["count"]:2d}×  {r["sr"]}: '
              f'«{" | ".join(r["translated_as"])}» → {r["should_be"]}')
    t_in = sum(r[1] for r in results)
    t_out = sum(r[2] for r in results)
    print(f"\nТерминов: {len(rows)}; {t_in} + {t_out} токенов ≈ ${t_in / 1e6 * 2 + t_out / 1e6 * 10:.2f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
