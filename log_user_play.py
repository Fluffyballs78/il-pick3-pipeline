#!/usr/bin/env python3
"""
log_user_play.py

Records what YOU played for a specific draw into:
  - SQLite: data/pick3.sqlite (table: user_plays)
  - CSV:    data/user_plays.csv (append-only for easy auditing)

Intended use:
  - Via GitHub Actions workflow_dispatch inputs (draw_date, draw_time, play4, optional notes)

Play format:
  - play4 must be exactly 4 digits (e.g., "0456") representing Pick3 + Fireball treated as 4 digits.
"""

import argparse
import csv
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

OUT_DIR = os.getenv("OUT_DIR", "data")
DB_PATH = os.getenv("DB_PATH", os.path.join(OUT_DIR, "pick3.sqlite"))
CSV_PATH = os.getenv("USER_PLAYS_CSV", os.path.join(OUT_DIR, "user_plays.csv"))

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def normalize_draw_time(s: str) -> str:
    s = (s or "").strip().upper()
    if s in ("MIDDAY", "DAY", "NOON"):
        return "MIDDAY"
    if s in ("EVENING", "PM", "NIGHT"):
        return "EVENING"
    return s

def validate_play4(play4: str) -> str:
    play4 = (play4 or "").strip()
    if not re.fullmatch(r"\d{4}", play4):
        raise ValueError("play4 must be exactly 4 digits (e.g., 0456).")
    return play4

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

def insert_play(conn: sqlite3.Connection, created_at_utc: str, draw_date: str, draw_time: str, play4: str, notes: str) -> bool:
    """Returns True if inserted, False if already exists."""
    try:
        conn.execute(
            "INSERT INTO user_plays(created_at_utc, draw_date, draw_time, play4, notes) VALUES(?,?,?,?,?)",
            (created_at_utc, draw_date, draw_time, play4, notes or None),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def append_csv(csv_path: Path, created_at_utc: str, draw_date: str, draw_time: str, play4: str, notes: str) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    exists = csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["created_at_utc", "draw_date", "draw_time", "play4", "notes"])
        w.writerow([created_at_utc, draw_date, draw_time, play4, notes or ""])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draw-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--draw-time", required=True, help="MIDDAY or EVENING")
    ap.add_argument("--play4", required=True, help="4 digits, e.g. 0456")
    ap.add_argument("--notes", default="", help="Optional notes")
    args = ap.parse_args()

    draw_date = args.draw_date.strip()
    draw_time = normalize_draw_time(args.draw_time)
    play4 = validate_play4(args.play4)
    notes = (args.notes or "").strip()

    created_at = utc_now_iso()

    db_path = Path(DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path.as_posix())
    try:
        ensure_table(conn)
        inserted = insert_play(conn, created_at, draw_date, draw_time, play4, notes)
    finally:
        conn.close()

    if inserted:
        append_csv(Path(CSV_PATH), created_at, draw_date, draw_time, play4, notes)
        print(f"[ok] logged play {play4} for {draw_date} {draw_time}")
    else:
        print(f"[skip] play already exists for {draw_date} {draw_time}: {play4}")

if __name__ == "__main__":
    main()
