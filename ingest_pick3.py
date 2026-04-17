# reverted to Lottery USA parser with tolerant full-text fallbacks
# placeholder due lengthimport os
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
# SOURCE: Lottery.net year archives (true history)
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

WEEKDAYS = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}

MONTH_MAP_FULL = {
    "January": 1, "February": 2, "March": 3, "April": 4,
    "May": 5, "June": 6, "July": 7, "August": 8,
    "September": 9, "October": 10, "November": 11, "December": 12,
}

# Two-line date format (older pages):
#   Wednesday
#   December 31, 2025
DATE_LINE_RE = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),\s+(20\d{2})$")

# One-line combined format (sometimes appears):
#   Wednesday January 21, 2026   23571
COMBINED_LINE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+"
    r"([A-Za-z]+)\s+(\d{1,2}),\s+(20\d{2})\b"
)

# ✅ NEW: matches digit lines like:
# "2", "* 2", "• 2", "- 2"
BULLET_DIGIT_RE = re.compile(r"^[\*\u2022\-\u2013\u2014]?\s*(\d)\s*$")


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
    r = requests.get(url, headers=HEADERS, timeout=45)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    text = soup.get_text("\n", strip=True)
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def extract_draw_date(lines: List[str], idx: int) -> Tuple[Optional[str], int]:
    """
    Supports:
      A) One-line: 'Wednesday January 21, 2026 23571'
      B) Two-line: 'Wednesday' then 'January 21, 2026'
    Returns (YYYY-MM-DD, header_index_used).
    """
    # One-line combined
    m = COMBINED_LINE_RE.match(lines[idx])
    if m:
        month = m.group(2)
        day = int(m.group(3))
        year = int(m.group(4))
        month_num = MONTH_MAP_FULL.get(month)
        if month_num:
            return datetime(year, month_num, day).strftime("%Y-%m-%d"), idx
        return None, idx

    # Two-line weekday + date next
    if lines[idx] in WEEKDAYS and idx + 1 < len(lines):
        m2 = DATE_LINE_RE.match(lines[idx + 1])
        if m2:
            month = m2.group(1)
            day = int(m2.group(2))
            year = int(m2.group(3))
            month_num = MONTH_MAP_FULL.get(month)
            if month_num:
                return datetime(year, month_num, day).strftime("%Y-%m-%d"), idx + 1

    return None, idx


def parse_lotterynet_year_page(lines: List[str], draw_time: str, source_url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    i = 0

    while i < len(lines) - 6:
        draw_date, header_idx = extract_draw_date(lines, i)
        if not draw_date:
            i += 1
            continue

        # Collect next 4 digits after header: 3 pick digits + fireball
        digits: List[int] = []
        j = header_idx + 1
        while j < min(header_idx + 120, len(lines)) and len(digits) < 4:
            m = BULLET_DIGIT_RE.match(lines[j])
            if m:
                digits.append(int(m.group(1)))
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
                repeated_digit = int([k for k, v in counts.items() if v == 2][0])

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

    os.makedirs(os.path.dirname(DB_PATH) or "data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    all_rows: List[Dict[str, Any]] = []

    for year, draw_time, url in iter_year_urls(start_year, end_year):
        print(f"[ingest] fetching {draw_time} year={year}: {url}")
        lines = fetch_lines(url)
        parsed = parse_lotterynet_year_page(lines, draw_time, url)
        print(f"[ingest] parsed {len(parsed)} rows for {draw_time} {year}")
        all_rows.extend(parsed)

    # Filter by START_DATE (inclusive)
    all_rows = [r for r in all_rows if datetime.strptime(r["draw_date"], "%Y-%m-%d") >= start_dt]

    if not all_rows:
        raise SystemExit("Ingested 0 rows (source blocked or parsing changed).")

    for r in all_rows:
        upsert(conn, r)

    export_csv(conn, CSV_PATH)
    conn.close()

    print(f"[ingest] Done. Upserted {len(all_rows)} rows into {CSV_PATH} and {DB_PATH}.")


if __name__ == "__main__":
    main()
