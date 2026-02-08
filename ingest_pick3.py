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

# =========================
# SOURCE: Lottery.net (has real year archives)
# =========================
MIDDAY_YEAR_URL_TMPL = "https://www.lottery.net/illinois/pick-3-midday/numbers/{year}"
EVENING_YEAR_URL_TMPL = "https://www.lottery.net/illinois/pick-3-evening/numbers/{year}"

DB_PATH = os.getenv("DB_PATH", "data/pick3.sqlite")
CSV_PATH = os.getenv("CSV_PATH", "data/pick3.csv")
START_DATE = os.getenv("START_DATE", "2010-01-01")  # inclusive, YYYY-MM-DD

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Pick3Bot/1.0; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

WEEKDAYS = {"Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"}

# Lottery.net uses:
#   Tuesday
#   December 31, 2019
DATE_LINE_RE_2 = re.compile(
    r"^([A-Za-z]+)\s+(\d{1,2}),\s+(20\d{2})$"
)

MONTH_MAP_FULL = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compute_checksum(draw_date: str, draw_time: str, d1: int, d2: int, d3: int, fb: Optional[int]) -> str:
    s = f"{draw_date}|{draw_time}|{d1}{d2}{d3}|{'' if fb is None else fb}"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def init_db(conn: sqlite3.Connection) -> None:
    """
    IMPORTANT: This schema has NO source_draw_id.
    Primary key is (draw_date, draw_time).
    """
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pick3_draws_date ON pick3_draws(draw_date);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pick3_draws_time ON pick3_draws(draw_time);")
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
            row["source_url"], now, now, 1, row["checksum"]
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
        json.dumps({
            "pick3_d1": od1, "pick3_d2": od2, "pick3_d3": od3,
            "fireball": ofb, "checksum": old_checksum
        }),
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


def parse_lotterynet_year_page(lines: List[str], draw_time: str, source_url: str) -> List[Dict[str, Any]]:
    """
    Lottery.net year pages look like:
        Tuesday
        December 31, 2019
          * 9
          * 1
          * 7
          * 5   (Fireball)

    We extract 3 Pick3 digits + Fireball (4th digit), if present.
    """
    out: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines) - 6:
        if lines[i] not in WEEKDAYS:
            i += 1
            continue

        m = DATE_LINE_RE_2.match(lines[i + 1])
        if not m:
            i += 1
            continue

        month_name = m.group(1)
        day = int(m.group(2))
        year = int(m.group(3))

        month_num = MONTH_MAP_FULL.get(month_name)
        if not month_num:
            i += 1
            continue

        draw_date = datetime(year, month_num, day).strftime("%Y-%m-%d")

        # collect the next 4 single-digit lines after the date block
        digits: List[int] = []
        j = i + 2
        while j < min(i + 40, len(lines)) and len(digits) < 4:
            if re.fullmatch(r"\d", lines[j]):
                digits.append(int(lines[j]))
            j += 1

        if len(digits) >= 3:
            d1, d2, d3 = digits[0], digits[1], digits[2]
            fb: Optional[int] = digits[3] if len(digits) >= 4 else None

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


def iter_year_urls(start_year: int, end_year: int) -> List[Tuple[int, str, str]]:
    """
    Returns list of (year, draw_time, url) for both MIDDAY and EVENING.
    """
    out: List[Tuple[int, str, str]] = []
    for y in range(start_year, end_year + 1):
        out.append((y, "MIDDAY", MIDDAY_YEAR_URL_TMPL.format(year=y)))
        out.append((y, "EVENING", EVENING_YEAR_URL_TMPL.format(year=y)))
    return out


def main() -> None:
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d")
    now = datetime.now()
    start_year = start_dt.year
    end_year = now.year

    print(f"[ingest] START_DATE={START_DATE} (years {start_year}..{end_year})")
    os.makedirs("data", exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    all_rows: List[Dict[str, Any]] = []
    for year, draw_time, url in iter_year_urls(start_year, end_year):
        print(f"[ingest] fetching {draw_time} year={year}: {url}")
        lines = fetch_lines(url)
        parsed = parse_lotterynet_year_page(lines, draw_time, url)
        all_rows.extend(parsed)

    # Filter by START_DATE
    all_rows = [r for r in all_rows if datetime.strptime(r["draw_date"], "%Y-%m-%d") >= start_dt]

    if not all_rows:
        raise SystemExit("Ingested 0 rows (source blocked or parsing changed).")

    for r in all_rows:
        upsert(conn, r)

    export_csv(conn, CSV_PATH)
    conn.close()

    print(f"Done. Upserted {len(all_rows)} rows into {CSV_PATH} and {DB_PATH}.")


if __name__ == "__main__":
    main()
