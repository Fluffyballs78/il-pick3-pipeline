#!/usr/bin/env python3
"""
build_prev_recap.py (PROD)

Fix
---
Your live_performance_log.csv does NOT have outcome_* columns.
It has:
  draw_date, draw_time, actual_pick3, actual_fireball, regime, [struct_weight?],
  top4, hits_in_top4, any_hit, hit_2plus

This script:
- Preserves your existing output keys:
    generated_at_utc
    last_observed_draw
    previous_draw_prediction
- Adds a render-ready object:
    prev_draw_card
  so the GPT can ALWAYS show:
    - Last draw (actual_4) from odds.last_observed_draw.pick4_like
    - Model Top4 (last draw) from live_performance_log.csv top4
    - Hits from hits_in_top4
    - Hit digits computed as intersection(actual_4 digits, model_top4 digits)

Inputs
------
- data/live_performance_log.csv (or LIVE_LOG_PATH env var)
- data/odds_board_latest.json   (or ODDS_PATH env var)

Output
------
- data/prev_draw_recap.json     (or PREV_RECAP_PATH env var)
"""

import csv
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, List


LIVE_LOG_PATH = os.getenv("LIVE_LOG_PATH", "data/live_performance_log.csv")
ODDS_PATH = os.getenv("ODDS_PATH", "data/odds_board_latest.json")
OUT_PATH = os.getenv("PREV_RECAP_PATH", "data/prev_draw_recap.json")


def _utc_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_int(x: Any) -> Optional[int]:
    if x is None:
        return None
    s = str(x).strip()
    if s == "":
        return None
    try:
        return int(float(s))
    except Exception:
        return None


def _digits_from_pick4_like(s: Any) -> List[str]:
    s = ("" if s is None else str(s)).strip()
    # Expected: "3704"
    if s.isdigit() and len(s) == 4:
        return list(s)
    # Fallback: extract digits
    return [ch for ch in s if ch.isdigit()]


def _digits_from_top4(s: Any) -> List[str]:
    s = ("" if s is None else str(s)).strip()
    if s.isdigit() and len(s) == 4:
        return list(s)
    cleaned = s.replace(",", " ").replace("|", " ")
    parts = [p.strip() for p in cleaned.split() if p.strip()]
    return [p for p in parts if p.isdigit() and len(p) == 1][:4]


def _read_last_row(path: Path) -> Optional[Dict[str, str]]:
    """
    Reads the last row of the CSV (DictReader).
    """
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        rows = list(r)
    if not rows:
        return None
    return rows[-1]


def main() -> None:
    live_path = Path(LIVE_LOG_PATH)
    odds_path = Path(ODDS_PATH)
    out_path = Path(OUT_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    odds = _load_json(odds_path)
    last_draw = odds.get("last_observed_draw", {}) if isinstance(odds, dict) else {}

    last_log = _read_last_row(live_path)

    # Build "previous_draw_prediction" from your actual schema
    previous_draw_prediction: Dict[str, Any] = {}
    if last_log:
        previous_draw_prediction = {
            "draw_date": (last_log.get("draw_date") or "").strip(),
            "draw_time": (last_log.get("draw_time") or "").strip(),
            "actual_pick3": (last_log.get("actual_pick3") or "").strip(),
            "actual_fireball": (last_log.get("actual_fireball") or "").strip(),
            "regime": _safe_int(last_log.get("regime")),
            "top4": (last_log.get("top4") or "").strip(),
            "hits_in_top4": _safe_int(last_log.get("hits_in_top4")),
            "any_hit": _safe_int(last_log.get("any_hit")),
            "hit_2plus": _safe_int(last_log.get("hit_2plus")),
        }
        # keep struct_weight if present in your CSV
        if "struct_weight" in last_log:
            try:
                previous_draw_prediction["struct_weight"] = float(last_log.get("struct_weight")) if last_log.get("struct_weight") not in (None, "") else None
            except Exception:
                previous_draw_prediction["struct_weight"] = None

    # Render-ready prev_draw_card for the Play Card
    actual_4 = (last_draw.get("pick4_like") if isinstance(last_draw, dict) else None)
    model_top4 = previous_draw_prediction.get("top4") if previous_draw_prediction else None
    hit_count = previous_draw_prediction.get("hits_in_top4") if previous_draw_prediction else None

    actual_digits = set(_digits_from_pick4_like(actual_4))
    model_digits = set(_digits_from_top4(model_top4))
    hit_digits = sorted(actual_digits.intersection(model_digits)) if actual_digits and model_digits else []

    prev_draw_card = {
        "draw_date": last_draw.get("draw_date") if isinstance(last_draw, dict) else None,
        "draw_time": last_draw.get("draw_time") if isinstance(last_draw, dict) else None,
        "actual_4": (str(actual_4).strip() if actual_4 is not None else None),
        "model_top4": (str(model_top4).strip() if model_top4 else None),
        "hit_count": hit_count,
        "hit_digits": hit_digits,
    }

    recap: Dict[str, Any] = {
        "generated_at_utc": _utc_now(),
        "last_observed_draw": last_draw,
        "previous_draw_prediction": previous_draw_prediction,
        "prev_draw_card": prev_draw_card,
    }

    out_path.write_text(json.dumps(recap, indent=2), encoding="utf-8")
    print(f"[prev_recap] wrote {out_path}")


if __name__ == "__main__":
    main()
