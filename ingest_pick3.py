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

# LotteryUSA sources (include FB)
MIDDAY_YEAR_URL = "https://www.lotteryusa.com/illinois/midday-3/year"
EVENING_YEAR_URL = "https://www.lotteryusa.com/illinois/daily-3/year"

DB_PATH = os.getenv("DB_PATH", "data/pick3.sqlite")
CSV_PATH = os.getenv("CSV_PATH", "data/pick3.csv")

MODE = os.getenv("MODE", "daily")              # daily | backfill (same behavior; both just refresh recent history)
START_DATE = os.getenv("START_DATE", "2025-09-01")  # inclusive, YYYY-MM-DD

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Pick3Bot/1.0; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# Example: "Friday,  Feb 6, 2026"
DATE_LINE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+([A-Za-z]{3})\s+(\d{1,2}),\s+(20\d{2})$",
    re.IGNORECASE,
)

MONTH_MAP = {m: i for i, m in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1)}


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


def fetch_lines(url: str) -> List[str]:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    text = soup.get_text("\n", strip=True)
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def parse_year_page(lines: List[str], draw_time: str, source_url: str) -> List[Dict[str, Any]]:
    """
    Parses LotteryUSA '.../year' pages which contain repeating blocks:
      <weekday, Mon dd, yyyy>
        * d1
        * d2
        * d3
        * FB : x
    """
    out: List[Dict[str, Any]] = []
    i = 0

    while i < len(lines):
        m = DATE_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue

        mon = m.group(2).title()
        day = int(m.group(3))
        year = int(m.group(4))
        draw_date = datetime(year, MONTH_MAP[mon], day).strftime("%Y-%m-%d")

        # Look ahead for digits + FB
        digits: List[int] = []
        fb: Optional[int] = None

        j = i + 1
        while j < min(i + 25, len(lines)) and len(digits) < 3:
            if re.fullmatch(r"\d", lines[j]):
                digits.append(int(lines[j]))
            j += 1

        k = i + 1
        while k < min(i + 40, len(lines)):
            if "FB" in lines[k]:
                # Could be "FB : 6" or "FB: 6"
                mfb = re.search(r"FB\s*:?\s*(\d)", lines[k])
                if mfb:
                    fb = int(mfb.group(1))
                break
            k += 1

        if len(digits) == 3:
            d1, d2, d3 = digits
            pick3_str = f"{d1}{d2}{d3}"
            sorted_str = "".join(sorted(pick3_str))
            counts: Dict[str, int] = {str(x): 0 for x in range(10)}
            for d in (d1, d2, d3):
                counts[str(d)] += 1
            has_triple = 1 if 3 in counts.values() else 0
            has_double = 1 if (2 in counts.values() and has_triple == 0) else 0
            repeated_digit = None
            if has_double:
                repeated_digit = int([k2 for k2, v2 in counts.items() if v2 == 2][0])

            out.append({
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
                "source_url": source_url,
                "checksum": compute_checksum(draw_date, draw_time, d1, d2, d3, fb),
            })

        i += 1

    return out


def main() -> None:
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d")

    os.makedirs("data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    midday_lines = fetch_lines(MIDDAY_YEAR_URL)
    evening_lines = fetch_lines(EVENING_YEAR_URL)

    rows = []
    rows.extend(parse_year_page(midday_lines, "MIDDAY", MIDDAY_YEAR_URL))
    rows.extend(parse_year_page(evening_lines, "EVENING", EVENING_YEAR_URL))

    # filter by START_DATE
    rows = [r for r in rows if datetime.strptime(r["draw_date"], "%Y-%m-%d") >= start_dt]

    if not rows:
        raise SystemExit("Ingested 0 rows (source blocked or parsing changed).")

    for r in rows:
        upsert(conn, r)

    export_csv(conn, CSV_PATH)
    conn.close()
    print(f"Done. Upserted {len(rows)} rows from LotteryUSA into {CSV_PATH}.")


if __name__ == "__main__":
    main()
