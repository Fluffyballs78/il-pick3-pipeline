#!/usr/bin/env python3
"""
build_user_play_recap.py

Builds a recap for YOUR plays for the most recently observed draw.

Inputs:
  - data/pick3.sqlite (table: user_plays)
  - data/odds_board_latest.json (for last_observed_draw.pick4_like + draw_date/time)

Output:
  - data/user_play_recap.json

Notes:
  - Hit logic matches your model-style tracking: treat draw as a 4-digit-like string.
  - Hit count is the size of set intersection between played digits and actual digits.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

OUT_DIR = os.getenv("OUT_DIR", "data")
DB_PATH = os.getenv("DB_PATH", os.path.join(OUT_DIR, "pick3.sqlite"))
ODDS_PATH = os.getenv("ODDS_PATH", os.path.join(OUT_DIR, "odds_board_latest.json"))
OUT_PATH = os.getenv("USER_PLAY_RECAP_PATH", os.path.join(OUT_DIR, "user_play_recap.json"))

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))

def digits(s: str) -> List[str]:
    s = (s or "").strip()
    return [ch for ch in s if ch.isdigit()]

def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS user_plays (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at_utc TEXT NOT NULL,
        draw_date TEXT NOT NULL,
        draw_time TEXT NOT NULL,
        play4 TEXT NOT NULL,
        notes TEXT
    );
    """)
    conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_user_plays_draw ON user_plays(draw_date, draw_time);
    """)
    conn.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_user_plays_unique ON user_plays(draw_date, draw_time, play4);
    """)
    conn.commit()

def fetch_plays(conn: sqlite3.Connection, draw_date: str, draw_time: str) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT created_at_utc, play4, COALESCE(notes,'') FROM user_plays WHERE draw_date=? AND draw_time=? ORDER BY created_at_utc ASC",
        (draw_date, draw_time),
    )
    out = []
    for created_at_utc, play4, notes in cur.fetchall():
        out.append({"created_at_utc": created_at_utc, "play4": play4, "notes": notes})
    return out

def score_play(play4: str, actual4: str) -> Dict[str, Any]:
    pset = set(digits(play4))
    aset = set(digits(actual4))
    hit = sorted(pset.intersection(aset))
    hit_count = len(hit)
    return {
        "play4": play4,
        "hit_count": hit_count,
        "any_hit": 1 if hit_count >= 1 else 0,
        "hit_2plus": 1 if hit_count >= 2 else 0,
        "hit_digits": hit,
    }

def main():
    odds = load_json(Path(ODDS_PATH))
    last = odds.get("last_observed_draw", {}) if isinstance(odds, dict) else {}

    draw_date = (last.get("draw_date") or "").strip()
    draw_time = (last.get("draw_time") or "").strip()
    actual4 = (last.get("pick4_like") or "").strip()

    out_path = Path(OUT_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    recap: Dict[str, Any] = {
        "generated_at_utc": utc_now_iso(),
        "last_observed_draw": last,
        "your_plays_for_last_draw": [],
        "summary": {
            "n_plays": 0,
            "any_play_any_hit": 0,
            "any_play_hit_2plus": 0,
            "best_hit_count": None,
            "best_play4": None,
            "best_hit_digits": [],
        },
    }

    if not (draw_date and draw_time and actual4):
        out_path.write_text(json.dumps(recap, indent=2), encoding="utf-8")
        print(f"[user_play_recap] wrote {out_path} (missing last_observed_draw info)")
        return

    db_path = Path(DB_PATH)
    if not db_path.exists():
        out_path.write_text(json.dumps(recap, indent=2), encoding="utf-8")
        print(f"[user_play_recap] wrote {out_path} (no db yet)")
        return

    conn = sqlite3.connect(db_path.as_posix())
    try:
        ensure_table(conn)
        plays = fetch_plays(conn, draw_date, draw_time)
    finally:
        conn.close()

    scored = []
    best = None
    for p in plays:
        s = score_play(p["play4"], actual4)
        s["created_at_utc"] = p["created_at_utc"]
        if p.get("notes"):
            s["notes"] = p["notes"]
        scored.append(s)
        if best is None or s["hit_count"] > best["hit_count"]:
            best = s

    recap["your_plays_for_last_draw"] = scored
    recap["summary"]["n_plays"] = len(scored)
    recap["summary"]["any_play_any_hit"] = 1 if any(x["any_hit"] == 1 for x in scored) else 0
    recap["summary"]["any_play_hit_2plus"] = 1 if any(x["hit_2plus"] == 1 for x in scored) else 0
    if best is not None:
        recap["summary"]["best_hit_count"] = best["hit_count"]
        recap["summary"]["best_play4"] = best["play4"]
        recap["summary"]["best_hit_digits"] = best["hit_digits"]

    out_path.write_text(json.dumps(recap, indent=2), encoding="utf-8")
    print(f"[user_play_recap] wrote {out_path} ({draw_date} {draw_time}, plays={len(scored)})")

if __name__ == "__main__":
    main()
