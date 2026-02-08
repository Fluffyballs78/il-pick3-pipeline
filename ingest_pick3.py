import os
import re
import csv
import json
import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import requests
from bs4 import BeautifulSoup

# Source that has Midday/Evening + Fireball in a single page stream
LOTTERYPOST_PAST_URL = "https://www.lotterypost.com/results/il/pick3/past"

DB_PATH = os.getenv("DB_PATH", "data/pick3.sqlite")
CSV_PATH = os.getenv("CSV_PATH", "data/pick3.csv")

MODE = os.getenv("MODE", "daily")  # daily | backfill
START_DATE = os.getenv("START_DATE", "2025-09-01")  # inclusive, YYYY-MM-DD

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Pick3Bot/1.0; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------- utilities ----------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def compute_checksum(draw_date: str, draw_time: str, d1: int, d2: int, d3: int, fb: Optional[int]) -> str:
    s = f"{draw_date}|{draw_time}|{d1}{d2}{d3}|{'' if fb is None else fb}"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def parse_start_date() -> datetime:
    return datetime.strptime(START_DATE, "%Y-%m-%d")


# ---------- DB ----------
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


def upsert(conn: sqlite3.Connection, row: Dict[str, Any]) -> None:
    now = utc_now_iso()

    cur = conn.execute(
        "SELECT checksum, row_version, pick3_d1, pick3_d2, pick3_d3, fireball "
        "FROM pick3_draws WHERE draw_date=? AND draw_time=?",
        (row["draw_date"], row["draw_time"]),
    )
    existing = cur.fetchone()

    if existing is None:
        conn.execute("""
            INSERT INTO pick3_draws (
                draw_date, draw_time, pick3_d1, pick3_d2, pick3_d3, fireball,
                pick3_str, sorted_str, has_double, has_triple, repeated_digit, digit_counts,
                source_url, ingested_at, updated_at, row_version, checksum
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            row["draw_date"], row["draw_time"], row["d1"], row["d2"], row["d3"], row["fireball"],
            row["pick3_str"], row["sorted_str"], row["has_double"], row["has_triple"], row["repeated_digit"], row["digit_counts"],
            row["source_url"],
            now, now, 1, row["checksum"]
        ))
        conn.commit()
        return

    old_checksum, old_version, od1, od2, od3, ofb = existing
    if old_checksum == row["checksum"]:
        conn.execute(
            "UPDATE pick3_draws SET updated_at=? WHERE draw_date=? AND draw_time=?",
            (now, row["draw_date"], row["draw_time"]),
        )
        conn.commit()
        return

    # audit + update
    conn.execute("""
        INSERT INTO pick3_audit (
            draw_date, draw_time, changed_at, old_checksum, new_checksum,
            old_payload_json, new_payload_json, reason
        ) VALUES (?,?,?,?,?,?,?,?)
    """, (
        row["draw_date"], row["draw_time"], now,
        old_checksum, row["checksum"],
        json.dumps({"d1": od1, "d2": od2, "d3": od3, "fireball": ofb, "checksum": old_checksum}),
        json.dumps(row),
        "source mismatch / correction"
    ))

    conn.execute("""
        UPDATE pick3_draws SET
            pick3_d1=?, pick3_d2=?, pick3_d3=?, fireball=?,
            pick3_str=?, sorted_str=?, has_double=?, has_triple=?, repeated_digit=?, digit_counts=?,
            source_url=?,
            updated_at=?, row_version=?, checksum=?
        WHERE draw_date=? AND draw_time=?
    """, (
        row["d1"], row["d2"], row["d3"], row["fireball"],
        row["pick3_str"], row["sorted_str"], row["has_double"], row["has_triple"], row["repeated_digit"], row["digit_counts"],
        row["source_url"],
        now, old_version + 1, row["checksum"],
        row["draw_date"], row["draw_time"]
    ))
    conn.commit()


def export_csv(conn: sqlite3.Connection, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = conn.execute("""
        SELECT draw_date, draw_time, pick3_d1, pick3_d2, pick3_d3, fireball,
               pick3_str, sorted_str, has_double, has_triple, repeated_digit, digit_counts,
               source_url, ingested_at, updated_at, row_version, checksum
        FROM pick3_draws
        ORDER BY draw_date ASC, CASE WHEN draw_time='MIDDAY' THEN 0 ELSE 1 END ASC
    """).fetchall()

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "draw_date","draw_time","pick3_d1","pick3_d2","pick3_d3","fireball",
            "pick3_str","sorted_str","has_double","has_triple","repeated_digit","digit_counts",
            "source_url","ingested_at","updated_at","row_version","checksum"
        ])
        w.writerows(rows)


# ---------- parsing LotteryPost ----------
DATE_RE = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})$",
                     re.IGNORECASE)

def month_str_to_num(m: str) -> int:
    return datetime.strptime(m[:3], "%b").month

def normalize_time(t: str) -> Optional[str]:
    t = t.strip().lower()
    if t == "midday":
        return "MIDDAY"
    if t == "evening":
        return "EVENING"
    return None

def parse_lp_page(text_lines: List[str]) -> List[Tuple[str, str, int, int, int, Optional[int]]]:
    """
    Converts the LotteryPost past-results page text into a list of:
      (draw_date YYYY-MM-DD, draw_time MIDDAY/EVENING, d1,d2,d3, fireball)
    The page is structured as repeating blocks of:
      Date line
      Midday -> 3 digits -> Fireball -> 1 digit
      Evening -> 3 digits -> Fireball -> 1 digit
    """
    out = []
    i = 0
    current_date = None

    while i < len(text_lines):
        line = text_lines[i].strip()
        m = DATE_RE.match(line)
        if m:
            month = month_str_to_num(m.group(2))
            day = int(m.group(3))
            year = int(m.group(4))
            current_date = datetime(year, month, day).strftime("%Y-%m-%d")
            i += 1
            continue

        t = normalize_time(line)
        if current_date and t:
            # Expect 3 single digits somewhere soon after
            # LotteryPost renders them as bullet list items; in text they appear as standalone numbers on lines.
            digits = []
            fb = None

            j = i + 1
            while j < min(i + 40, len(text_lines)) and len(digits) < 3:
                s = text_lines[j].strip()
                if re.fullmatch(r"\d", s):
                    digits.append(int(s))
                j += 1

            # Find fireball label and digit
            k = j
            while k < min(i + 60, len(text_lines)) and fb is None:
                s = text_lines[k].strip().lower()
                if s.startswith("fireball"):
                    # next digit line after "Fireball:"
                    kk = k + 1
                    while kk < min(k + 10, len(text_lines)):
                        d = text_lines[kk].strip()
                        if re.fullmatch(r"\d", d):
                            fb = int(d)
                            break
                        kk += 1
                    break
                k += 1

            if len(digits) == 3:
                out.append((current_date, t, digits[0], digits[1], digits[2], fb))

        i += 1

    return out


def fetch_lotterypost_page(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def main() -> None:
    start_dt = parse_start_date()

    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    html = fetch_lotterypost_page(LOTTERYPOST_PAST_URL)
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n", strip=True)
    lines = [ln for ln in text.split("\n") if ln.strip()]

    parsed = parse_lp_page(lines)

    # Filter to >= START_DATE
    rows = []
    for draw_date, draw_time, d1, d2, d3, fb in parsed:
        dt = datetime.strptime(draw_date, "%Y-%m-%d")
        if dt >= start_dt:
            pick3_str = f"{d1}{d2}{d3}"
            sorted_str = "".join(sorted(pick3_str))
            counts = {str(i): 0 for i in range(10)}
            for d in (d1, d2, d3):
                counts[str(d)] += 1
            has_triple = 1 if 3 in counts.values() else 0
            has_double = 1 if (2 in counts.values() and has_triple == 0) else 0
            repeated_digit = None
            if has_double:
                repeated_digit = int([k for k, v in counts.items() if v == 2][0])

            row = {
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
                "source_url": LOTTERYPOST_PAST_URL,
                "checksum": compute_checksum(draw_date, draw_time, d1, d2, d3, fb),
            }
            rows.append(row)

    inserted = 0
    for row in rows:
        # upsert() counts changes inside DB; for simplicity we count rows attempted
        upsert(conn, row)
        inserted += 1

    export_csv(conn, CSV_PATH)
    conn.close()

    # If we ingested nothing, fail the run so you never get "green but empty" again.
    if inserted == 0:
        raise SystemExit("Ingested 0 rows (source blocked or parsing changed).")

    print(f"Done. Parsed & upserted {inserted} rows from LotteryPost into {CSV_PATH} and {DB_PATH}.")


if __name__ == "__main__":
    main()
