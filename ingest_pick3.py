import os
import re
import csv
import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import requests
from bs4 import BeautifulSoup

IL_PICK3_DRAW_URL_TMPL = "https://www.illinoislottery.com/dbg/results/pick3/draw/{draw_id}"

START_DRAW_ID = int(os.getenv("START_DRAW_ID", "23286"))   # 2025-09-01 Midday
MODE = os.getenv("MODE", "daily")                          # daily | backfill
DAILY_LOOKBACK = int(os.getenv("DAILY_LOOKBACK", "120"))   # used only in daily mode
MAX_DRAW_ID = os.getenv("MAX_DRAW_ID")                     # optional override to avoid slow probing

DB_PATH = os.getenv("DB_PATH", "data/pick3.sqlite")
CSV_PATH = os.getenv("CSV_PATH", "data/pick3.csv")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Pick3Bot/1.0; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.illinoislottery.com/",
    "Connection": "keep-alive",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compute_checksum(draw_date: str, draw_time: str, d1: int, d2: int, d3: int, fb: Optional[int]) -> str:
    s = f"{draw_date}|{draw_time}|{d1}{d2}{d3}|{'' if fb is None else fb}"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS pick3_draws (
        draw_date TEXT NOT NULL,
        draw_time TEXT NOT NULL,              -- MIDDAY | EVENING
        pick3_d1 INTEGER NOT NULL,
        pick3_d2 INTEGER NOT NULL,
        pick3_d3 INTEGER NOT NULL,
        fireball INTEGER,
        pick3_str TEXT NOT NULL,
        sorted_str TEXT NOT NULL,
        has_double INTEGER NOT NULL,
        has_triple INTEGER NOT NULL,
        repeated_digit INTEGER,
        digit_counts TEXT NOT NULL,           -- JSON
        source_draw_id INTEGER NOT NULL,
        source_url TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        row_version INTEGER NOT NULL,
        checksum TEXT NOT NULL,
        PRIMARY KEY (draw_date, draw_time)
    );
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS pick3_audit (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        draw_date TEXT NOT NULL,
        draw_time TEXT NOT NULL,
        changed_at TEXT NOT NULL,
        old_checksum TEXT,
        new_checksum TEXT NOT NULL,
        old_payload_json TEXT,
        new_payload_json TEXT NOT NULL,
        reason TEXT NOT NULL
    );
    """)
    conn.commit()


def url_exists(url: str) -> bool:
    """
    For probing only. If we get 403, treat it as NOT usable for existence checks
    (otherwise probing can take forever).
    """
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code == 404:
        return False
    if r.status_code == 403:
        return False
    r.raise_for_status()
    return True


def get_latest_draw_id_fallback(start_hint: int) -> int:
    low = start_hint
    step = 1

    # Find an upper bound where page no longer exists
    while True:
        test_id = low + step
        if not url_exists(IL_PICK3_DRAW_URL_TMPL.format(draw_id=test_id)):
            high = test_id
            break
        low = test_id
        step *= 2
        if step > 8192:
            high = low + step
            break

    left, right = low, high

    # Binary search for last existing id
    while left + 1 < right:
        mid = (left + right) // 2
        if url_exists(IL_PICK3_DRAW_URL_TMPL.format(draw_id=mid)):
            left = mid
        else:
            right = mid

    return left


def get_latest_draw_id(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT MAX(source_draw_id) FROM pick3_draws")
    row = cur.fetchone()
    last_id = row[0] if row and row[0] is not None else None
    hint = int(last_id) if last_id is not None else START_DRAW_ID
    return get_latest_draw_id_fallback(hint)


def parse_draw(draw_id: int) -> Optional[Dict[str, Any]]:
    url = IL_PICK3_DRAW_URL_TMPL.format(draw_id=draw_id)
    r = requests.get(url, headers=HEADERS, timeout=30)

    if r.status_code in (404, 403):
        return None
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")
    text = soup.get_text(" ", strip=True)

    m_date = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(20\d{2})\b", text)
    m_time = re.search(r"\b(midday|evening)\b", text, re.IGNORECASE)
    if not m_date or not m_time:
        return None

    dt = datetime.strptime(f"{m_date.group(1)} {m_date.group(2)} {m_date.group(3)}", "%b %d %Y")
    draw_date = dt.strftime("%Y-%m-%d")
    draw_time = m_time.group(1).upper()  # MIDDAY / EVENING

    idx = text.lower().find("results")
    tail = text[idx: idx + 600] if idx >= 0 else text[:600]
    digits = [int(x) for x in re.findall(r"\b\d\b", tail)]
    if len(digits) < 4:
        return None

    d1, d2, d3, fb = digits[0], digits[1], digits[2], digits[3]

    if draw_time not in ("MIDDAY", "EVENING"):
        return None
    if any(d < 0 or d > 9 for d in (d1, d2, d3, fb)):
        return None

    pick3_str = f"{d1}{d2}{d3}"
    sorted_str = "".join(sorted(pick3_str))

    counts: Dict[str, int] = {str(i): 0 for i in range(10)}
    for d in (d1, d2, d3):
        counts[str(d)] += 1

    has_triple = 1 if 3 in counts.values() else 0
    has_double = 1 if (2 in counts.values() and has_triple == 0) else 0
    repeated_digit = None
    if has_double:
        repeated_digit = int([k for k, v in counts.items() if v == 2][0])

    csum = compute_checksum(draw_date, draw_time, d1, d2, d3, fb)

    return {
        "draw_date": draw_date,
        "draw_time": draw_time,
        "d1": d1, "d2": d2, "d3": d3,
        "fireball": fb,
        "pick3_str": pick3_str,
        "sorted_str": sorted_str,
        "has_double": has_double,
        "has_triple": has_triple,
        "repeated_digit": repeated_digit,
        "digit_counts": json.dumps(counts),
        "source_draw_id": draw_id,
        "source_url": url,
        "checksum": csum,
    }


def upsert(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    now = utc_now_iso()

    cur = conn.execute(
        "SELECT checksum, row_version, pick3_d1, pick3_d2, pick3_d3, fireball, source_draw_id "
        "FROM pick3_draws WHERE draw_date=? AND draw_time=?",
        (row["draw_date"], row["draw_time"]),
    )
    existing = cur.fetchone()

    if existing is None:
        conn.execute("""
            INSERT INTO pick3_draws (
                draw_date, draw_time, pick3_d1, pick3_d2, pick3_d3, fireball,
                pick3_str, sorted_str, has_double, has_triple, repeated_digit, digit_counts,
                source_draw_id, source_url,
                ingested_at, updated_at, row_version, checksum
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row["draw_date"], row["draw_time"], row["d1"], row["d2"], row["d3"], row["fireball"],
            row["pick3_str"], row["sorted_str"], row["has_double"], row["has_triple"], row["repeated_digit"], row["digit_counts"],
            row["source_draw_id"], row["source_url"],
            now, now, 1, row["checksum"]
        ))
        conn.commit()
        return

    old_checksum, old_version, od1, od2, od3, ofb, old_draw_id = existing

    if old_checksum == row["checksum"]:
        conn.execute(
            "UPDATE pick3_draws SET updated_at=? WHERE draw_date=? AND draw_time=?",
            (now, row["draw_date"], row["draw_time"]),
        )
        conn.commit()
        return

    conn.execute("""
        INSERT INTO pick3_audit (
            draw_date, draw_time, changed_at, old_checksum, new_checksum,
            old_payload_json, new_payload_json, reason
        ) VALUES (?,?,?,?,?,?,?,?)
    """, (
        row["draw_date"], row["draw_time"], now,
        old_checksum, row["checksum"],
        json.dumps({"checksum": old_checksum}),
        json.dumps(row),
        "correction sweep detected mismatch"
    ))

    conn.execute("""
        UPDATE pick3_draws SET
            pick3_d1=?, pick3_d2=?, pick3_d3=?, fireball=?,
            pick3_str=?, sorted_str=?, has_double=?, has_triple=?, repeated_digit=?, digit_counts=?,
            source_draw_id=?, source_url=?,
            updated_at=?, row_version=?, checksum=?
        WHERE draw_date=? AND draw_time=?
    """, (
        row["d1"], row["d2"], row["d3"], row["fireball"],
        row["pick3_str"], row["sorted_str"], row["has_double"], row["has_triple"], row["repeated_digit"], row["digit_counts"],
        row["source_draw_id"], row["source_url"],
        now, old_version + 1, row["checksum"],
        row["draw_date"], row["draw_time"],
    ))
    conn.commit()


def export_csv(conn: sqlite3.Connection, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = conn.execute("""
        SELECT draw_date, draw_time, pick3_d1, pick3_d2, pick3_d3, fireball,
               pick3_str, sorted_str, has_double, has_triple, repeated_digit, digit_counts,
               source_draw_id, source_url, ingested_at, updated_at, row_version, checksum
        FROM pick3_draws
        ORDER BY draw_date ASC, CASE WHEN draw_time='MIDDAY' THEN 0 ELSE 1 END ASC
    """).fetchall()

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "draw_date","draw_time","pick3_d1","pick3_d2","pick3_d3","fireball",
            "pick3_str","sorted_str","has_double","has_triple","repeated_digit","digit_counts",
            "source_draw_id","source_url","ingested_at","updated_at","row_version","checksum"
        ])
        w.writerows(rows)


def main() -> None:
    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    # If MAX_DRAW_ID is provided, use it (prevents slow probing).
    if MAX_DRAW_ID:
        latest = int(MAX_DRAW_ID)
    else:
        latest = get_latest_draw_id(conn)

    if MODE == "backfill":
        start = START_DRAW_ID
    else:
        start = max(START_DRAW_ID, latest - DAILY_LOOKBACK)

    for draw_id in range(start, latest + 1):
        row = parse_draw(draw_id)
        if row:
            upsert(conn, row)

    export_csv(conn, CSV_PATH)
    conn.close()
    print(f"Done. Latest draw_id seen: {latest}. Updated {CSV_PATH} and {DB_PATH}.")


if __name__ == "__main__":
    main()
