!/usr/bin/env python3
"""
build_prev_recap.py

Purpose
-------
Builds data/prev_draw_recap.json from the most recent completed draw in
data/live_performance_log.csv.

This file is designed to be *drop-in* for your existing pipeline:
- It preserves your existing top-level keys (previous_draw_prediction.*)
  while adding a new, render-ready object: prev_draw_card
- It does NOT require web access
- It relies on live_performance_log.csv being produced earlier in the run

Inputs
------
- data/live_performance_log.csv

Outputs
-------
- data/prev_draw_recap.json

Notes
-----
- Treats "Pick3 + Fireball" as a 4-digit-like string for intersection and recap.
- If your live_performance_log schema differs, update the column names in
  `COLUMN_MAP` below.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from datetime import datetime, timezone

DATA_DIR = Path("data")
LOG_PATH = DATA_DIR / "live_performance_log.csv"
OUT_PATH = DATA_DIR / "prev_draw_recap.json"

# Adjust these if your CSV column names differ.
COLUMN_MAP = {
    "draw_date": "draw_date",
    "draw_time": "draw_time",
    "actual_pick3": "actual_pick3",
    "actual_fireball": "actual_fireball",
    "top4": "top4",                 # model top4 used for that draw
    "hits_in_top4": "hits_in_top4", # integer 0-4 (or string parseable to int)
    # Optional columns — we include if present:
    "regime": "regime",
    "struct_weight": "struct_weight",
}

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def _read_last_row_csv(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Ensure live_performance_log.py ran first.")

    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if any((v or "").strip() for v in r.values())]
    if not rows:
        raise ValueError(f"{path} has no data rows.")
    return rows[-1]

def _digits_from_actual(pick3: str, fireball: str) -> list[str]:
    pick3 = (pick3 or "").strip()
    fireball = (fireball or "").strip()
    # Pad pick3 if needed
    if pick3.isdigit() and len(pick3) < 3:
        pick3 = pick3.zfill(3)
    digits = list(pick3) + ([fireball] if fireball != "" else [])
    # Filter to single digits only
    return [d for d in digits if d.isdigit() and len(d) == 1]

def _digits_from_top4(top4: str) -> list[str]:
    top4 = (top4 or "").strip()
    # "0135" -> ["0","1","3","5"]
    if top4.isdigit() and len(top4) == 4:
        return list(top4)
    # If stored like "0,1,3,5" or "0 1 3 5"
    cleaned = top4.replace(",", " ").replace("|", " ")
    parts = [p.strip() for p in cleaned.split() if p.strip()]
    return [p for p in parts if p.isdigit() and len(p) == 1][:4]

def main() -> None:
    last = _read_last_row_csv(LOG_PATH)

    def get(col_key: str, default: str = "") -> str:
        col = COLUMN_MAP[col_key]
        return (last.get(col) or default).strip()

    draw_date = get("draw_date")
    draw_time = get("draw_time").upper()
    actual_pick3 = get("actual_pick3")
    actual_fireball = get("actual_fireball")
    model_top4 = get("top4")

    # Hits
    hits_raw = get("hits_in_top4", "0")
    try:
        hit_count = int(float(hits_raw))
    except Exception:
        hit_count = 0

    actual_digits = _digits_from_actual(actual_pick3, actual_fireball)
    model_digits = _digits_from_top4(model_top4)

    actual_set = set(actual_digits)
    model_set = set(model_digits)

    hit_digits = sorted(actual_set.intersection(model_set))
    miss_digits = sorted(model_set.difference(actual_set))

    actual_4 = "".join(actual_digits) if actual_digits else ""
    model_4 = "".join(model_digits) if model_digits else ""

    # Optional metrics
    regime = last.get(COLUMN_MAP["regime"]) if COLUMN_MAP.get("regime") in (last.keys()) else None
    struct_weight = last.get(COLUMN_MAP["struct_weight"]) if COLUMN_MAP.get("struct_weight") in (last.keys()) else None

    prev_draw_card = {
        "draw_date": draw_date,
        "draw_time": draw_time,
        "actual_4": actual_4,
        "actual_pick3": actual_pick3,
        "actual_fireball": actual_fireball,
        "model_top4": model_4,
        "hit_count": hit_count,
        "hit_digits": hit_digits,
        "miss_digits": miss_digits,
    }
    if regime is not None and str(regime).strip() != "":
        prev_draw_card["regime"] = regime
    if struct_weight is not None and str(struct_weight).strip() != "":
        # keep as string to avoid float rounding changes
        prev_draw_card["struct_weight"] = struct_weight

    # Preserve your existing structure and add new fields
    out = {
        "fetched_at_utc": _utc_now_iso(),
        "previous_draw_prediction": {
            "top4": model_4,
            "outcome_hit_count": hit_count,
        },
        # New: render-ready recap object
        "prev_draw_card": prev_draw_card,
        # Helpful extras (non-breaking)
        "debug": {
            "source": "live_performance_log.csv:last_row",
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Wrote {OUT_PATH} (draw={draw_date} {draw_time}, hits={hit_count})")

if __name__ == "__main__":
    main()
