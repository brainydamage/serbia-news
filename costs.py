"""Сколько сборки потратили на перевод — из статистики, которую копит Worker жалоб (worker/, ключ KV "stats").

    .venv/bin/python costs.py              — по дням за последний месяц и итоги по месяцам
    .venv/bin/python costs.py 2026-11-03   — каждая сборка за этот день (хранятся две недели)
    --local                                — из локального wrangler dev, а не с Cloudflare

Читает через wrangler, которому нужен вход в аккаунт Cloudflare (npx wrangler login). Суммы — оценка
по токенам и ценам из translate.py; сколько списали на самом деле — в Anthropic Console → Usage / Cost.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

WRANGLER = ["npx", "--yes", "wrangler@4.150.0"]
KINDS = {"new": "новые", "retry": "повтор", "review": "жалобы"}
MONTHS = "январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь".split()
DAYS_SHOWN = 31


def load(local: bool) -> dict:
    command = [*WRANGLER, "kv", "key", "get", "stats", "--binding", "REPORTS", "--local" if local else "--remote"]
    result = subprocess.run(command, cwd=Path(__file__).parent / "worker", capture_output=True, text=True,
                            env={**os.environ, "WRANGLER_SEND_METRICS": "false"})
    if result.returncode != 0:
        sys.exit(f"wrangler не смог прочитать статистику:\n{result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        sys.exit("Статистики пока нет — её пишет сборка, которая что-то переводила.")


def cell(spent: dict | None) -> str:
    return f"{spent['items']:>5} · {spent['usd']:.4f}" if spent else f"{'—':>13}"


def total(row: dict) -> float:
    return sum(row.get(kind, {}).get("usd", 0) for kind in KINDS)


def table(first: str, rows: list[tuple[str, dict]], count: str | None) -> None:
    print(f"{first:<14}{'сборок' if count else '':>7}" + "".join(f"{name:>16}" for name in KINDS.values()) + f"{'итого':>10}")
    for label, row in rows:
        print(f"{label:<14}{row.get(count, '') if count else '':>7}"
              + "".join(f"{cell(row.get(kind)):>16}" for kind in KINDS) + f"{total(row):>10.4f}")


def by_days(stats: dict) -> None:
    days = sorted(stats["days"].items())
    print("Новостей · $ по дням (время белградское; «сборок» — тех, что что-то переводили)\n")
    table("день", days[-DAYS_SHOWN:], "builds")

    months: dict[str, dict] = {}
    for day, row in days:
        month = months.setdefault(day[:7], {"builds": 0})
        month["builds"] += row["builds"]
        for kind in KINDS:
            if kind in row:
                sum_ = month.setdefault(kind, {"items": 0, "usd": 0})
                sum_["items"] += row[kind]["items"]
                sum_["usd"] += row[kind]["usd"]
    print("\nПо месяцам\n")
    table("месяц", [(f"{MONTHS[int(m[5:]) - 1]} {m[:4]}", row) for m, row in months.items()], "builds")


def by_builds(stats: dict, day: str) -> None:
    builds = [b for b in stats["builds"] if b["at"].startswith(day)]
    if not builds:
        sys.exit(f"За {day} сборок нет: каждая сборка хранится две недели, и только если что-то переводила.")
    print(f"Сборки за {day}: новостей · $\n")
    table("время", [(b["at"][11:16], b) for b in builds], None)
    print(f"\nВсего: ${sum(map(total, builds)):.3f}")


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--local"]
    stats = load("--local" in sys.argv)
    by_builds(stats, args[0]) if args else by_days(stats)


if __name__ == "__main__":
    main()
