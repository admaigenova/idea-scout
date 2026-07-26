"""Persistence for scored ideas: an append-only CSV the workflow commits back."""

from __future__ import annotations

import csv
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "data" / "ideas.csv"

FIELDS = [
    "date", "title", "url", "source", "points", "comments",
    "payer", "demand", "revenue_3mo", "buildable", "difficulty",
    "total", "summary", "verdict",
]


def append_ideas(ideas: list[dict], run_date: str) -> int:
    """Append one row per idea for this run; returns the number written."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    new_file = not DATA_FILE.exists()
    with DATA_FILE.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        for idea in ideas:
            writer.writerow({
                "date": run_date,
                "title": idea["title"],
                "url": idea["url"],
                "source": idea["source"],
                "points": idea["points"],
                "comments": idea["comments"],
                "payer": idea["scores"]["payer"],
                "demand": idea["scores"]["demand"],
                "revenue_3mo": idea["scores"]["revenue_3mo"],
                "buildable": idea["scores"]["buildable"],
                "difficulty": idea["scores"].get("difficulty", 5),
                "total": idea["total"],
                "summary": idea["summary"],
                "verdict": idea["verdict"],
            })
    return len(ideas)


def load_ideas() -> list[dict]:
    """All logged rows with numeric fields coerced; [] when no data exists yet."""
    if not DATA_FILE.exists():
        return []
    records = []
    with DATA_FILE.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                for key in ("points", "comments", "payer", "demand",
                            "revenue_3mo", "buildable", "difficulty"):
                    row[key] = int(float(row[key]))
                row["total"] = float(row["total"])
            except (TypeError, ValueError, KeyError):
                continue  # skip a corrupt row rather than break the dashboard
            records.append(row)
    return records
