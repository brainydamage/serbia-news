"""Снимок архива новостей за 4 дня (build.ARCHIVE_DAYS) из последней удачной сборки — для экспериментов на настоящих данных (сюжеты).

    python3 snapshot.py           — в ../serbia-news-data/datasets/archive-ГГГГ-ММ-ДД.json
    python3 snapshot.py <папка>   — в другую папку

Сборка выкладывает cache/archive.json артефактом «archive» (build.yml, хранится сутки). Нужен gh с входом в GitHub.
Дата в имени — день последней новости по Белграду; второй снимок за тот же день заменяет первый, он полнее.
"""
import collections
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).parent
DATASETS = ROOT.parent / "serbia-news-data" / "datasets"
LOCAL_TZ = ZoneInfo("Europe/Belgrade")


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit(f"gh {' '.join(args)}:\n{result.stderr.strip()}")
    return result.stdout


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DATASETS
    runs = json.loads(gh("run", "list", "--workflow", "build.yml", "--status", "success", "--limit", "1",
                         "--json", "databaseId,createdAt"))
    if not runs:
        sys.exit("нет удачных сборок")
    run = runs[0]
    with tempfile.TemporaryDirectory() as tmp:
        gh("run", "download", str(run["databaseId"]), "--name", "archive", "--dir", tmp)
        archive = json.loads((Path(tmp) / "archive.json").read_text("utf-8"))

    seen = sorted(a["seen"] for a in archive.values())
    day = datetime.fromisoformat(seen[-1]).astimezone(LOCAL_TZ).date()
    target.mkdir(parents=True, exist_ok=True)
    out = target / f"archive-{day}.json"
    out.write_text(json.dumps(archive, ensure_ascii=False), "utf-8")

    leads = collections.Counter(a.get("lead") or "без отметки" for a in archive.values())
    print(f"сборка {run['databaseId']} от {run['createdAt']}: {len(archive)} новостей, seen {seen[0]} — {seen[-1]}")
    print("lead: " + ", ".join(f"{k} {v}" for k, v in leads.most_common()))
    kinds = collections.Counter(a.get("kind") or "без отметки" for a in archive.values())
    print("kind: " + ", ".join(f"{k} {v}" for k, v in kinds.most_common()))
    topics = collections.Counter(a.get("topic") or "без отметки" for a in archive.values())
    print("topic: " + ", ".join(f"{k} {v}" for k, v in topics.most_common()))
    print(f"→ {out}")


if __name__ == "__main__":
    main()
