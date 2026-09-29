"""Persistent ledger of Google Maps places we've already scraped.

Keyed on Apify's `placeId` so re-running the pipeline for the same city
returns only fresh leads. Storage lives at `data/seen_places.json` (a
gitignored, persistent location — `.tmp/` would be wrong because it's
disposable by convention).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DATA_DIR  # noqa: E402

LEDGER_PATH = DATA_DIR / "seen_places.json"


def load_seen() -> dict:
    if not LEDGER_PATH.exists():
        return {}
    return json.loads(LEDGER_PATH.read_text(encoding="utf-8"))


def filter_unseen(items: list[dict]) -> list[dict]:
    """Return only items whose `place_id` isn't in the ledger.

    Items lacking a `place_id` pass through — defensive, shouldn't happen
    with Apify but we don't want to silently drop a lead over a missing key.
    """
    seen = load_seen()
    out = []
    for item in items:
        pid = item.get("place_id")
        if pid and pid in seen:
            continue
        out.append(item)
    return out


def mark_seen(items: list[dict], category: str, city: str) -> None:
    """Record each item's place_id in the ledger. Atomic write."""
    seen = load_seen()
    today = date.today().isoformat()
    for item in items:
        pid = item.get("place_id")
        if not pid:
            continue
        seen[pid] = {
            "name": item.get("name"),
            "city": city,
            "category": category,
            "first_seen": today,
        }
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = LEDGER_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(seen, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, LEDGER_PATH)
