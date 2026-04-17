import os
import re
import csv
import json
import hashlib
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple

import requests
from bs4 import BeautifulSoup

# =========================
# SOURCE: Lottery USA latest + year archive
# =========================
MIDDAY_LATEST_URL = "https://www.lotteryusa.com/illinois/midday-3/"
EVENING_LATEST_URL = "https://www.lotteryusa.com/illinois/daily-3/"
MIDDAY_YEAR_URL = "https://www.lotteryusa.com/illinois/midday-3/year"
EVENING_YEAR_URL = "https://www.lotteryusa.com/illinois/daily-3/year"

DB_PATH = os.getenv("DB_PATH", "data/pick3.sqlite")
CSV_PATH = os.getenv("CSV_PATH", "data/pick3.csv")
START_DATE = os.getenv("START_DATE", "2010-01-01")  # inclusive, YYYY-MM-DD

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Pick3Bot/1.0; +https://github.com/)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

MONTH_MAP = {
    "Jan": 1, "January": 1,
    "Feb": 2, "February": 2,
    "Mar": 3, "March": 3,
    "Apr": 4, "April": 4,
    "May": 5,
    "Jun": 6, "June": 6,
    "Jul": 7, "July": 7,
    "Aug": 8, "August": 8,
    "Sep": 9, "Sept": 9, "September": 9,
    "Oct": 10, "October": 10,
    "Nov": 11, "November": 11,
    "Dec": 12, "December": 12,
}

DATE_LINE_RE = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"([A-Za-z]{3,9})\s+(\d{1,2}),\s+(20\d{2})$"
)
DIGIT_LINE_RE = re.compile(r"^\*\s*(\d)$")
FB_LINE_RE = re.compile(r"^\*\s*FB\s*:\s*(\d)$", re.IGNORECASE)
COMPACT_RESULT_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"([A-Za-z]{3,9})\s+(\d{1,2}),\s+(20\d{2})\s+"
    r"(\d),\s*(\d),\s*(\d),\s*FB:\s*(\d)",
    re.IGNORECASE,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def compute_checksum(draw_date: str, draw_time: str, d1: int, d2: int, d3: int, fb: Optional[int]) -> str:
    s = f"{draw_date}|{draw_time}|{d1}{d2}{d3}|{'' if fb is None else fb}"
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS pick3_draws (
        draw_date TEXT NOT NULL,
        draw_time TEXT NOT NULL,
        pick3_d1 INTEGER NOT NULL,
        pick3_d2 INTEGER NOT NULL,
        pick3_d3 INTEGER NOT NULL,
        fireball INTEGER,
        pick3_str TEXT NOT NULL,
        sorted_str TEXT NOT NULL,
        has_double INTEGER NOT NULL,
        has_triple INTEGER NOT NULL,
        repeated_digit INTEGER,
        digit_counts TEXT NOT NULL,
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
    return [line.strip() for line in text.splitlines() if line.strip()]


def normalize_draw_date(month_name: str, day: str, year: str) -> str:
    month_num = MONTH_MAP[month_name]
    return datetime(int(year), month_num, int(day)).strftime("%Y-%m-%d")


def parse_draw_date(line: str) -> Optional[str]:
    match = DATE_LINE_RE.match(line)
    if not match:
        return None
    return normalize_draw_date(match.group(2), match.group(3), match.group(4))


def build_row(draw_date: str, draw_time: str, d1: int, d2: int, d3: int, fb: Optional[int], source_url: str) -> Dict[str, Any]:
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

    return {
        "draw_date": draw_date,
        "draw_time": draw_time,
        "d1": d1,
        "d2": d2,
        "d3": d3,
        "fireball": fb,
        "pick3_str": pick3_str,
        "sorted_str": sorted_str,
        "has_double": has_double,
        "has_triple": has_triple,
        "repeated_digit": repeated_digit,
        "digit_counts": json.dumps(counts),
        "source_url": source_url,
        "checksum": compute_checksum(draw_date, draw_time, d1, d2, d3, fb),
    }


def parse_compact_results(lines: List[str], draw_time: str, source_url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in lines:
        for match in COMPACT_RESULT_RE.finditer(line):
            draw_date = normalize_draw_date(match.group(2), match.group(3), match.group(4))
            out.append(
                build_row(
                    draw_date,
                    draw_time,
                    int(match.group(5)),
                    int(match.group(6)),
                    int(match.group(7)),
                    int(match.group(8)),
                    source_url,
                )
            )
    return out


def parse_lotteryusa_page(lines: List[str], draw_time: str, source_url: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    current_date: Optional[str] = None
    digits: List[int] = []
    fireball: Optional[int] = None

    def flush_current() -> None:
        nonlocal current_date, digits, fireball
        if current_date is not None and len(digits) == 3 and fireball is not None:
            out.append(build_row(current_date, draw_time, digits[0], digits[1], digits[2], fireball, source_url))
        current_date = None
        digits = []
        fireball = None

    for line in lines:
        draw_date = parse_draw_date(line)
        if draw_date is not None:
            flush_current()
            current_date = draw_date
            continue

        if current_date is None:
            continue

        digit_match = DIGIT_LINE_RE.match(line)
        if digit_match:
            if len(digits) < 3:
                digits.append(int(digit_match.group(1)))
            continue

        fb_match = FB_LINE_RE.match(line)
        if fb_match:
            fireball = int(fb_match.group(1))
            continue

    flush_current()

    if out:
        return out

    return parse_compact_results(lines, draw_time, source_url)


def iter_source_urls() -> List[Tuple[str, str]]:
    return [
        ("MIDDAY", MIDDAY_LATEST_URL),
        ("EVENING", EVENING_LATEST_URL),
        ("MIDDAY", MIDDAY_YEAR_URL),
        ("EVENING", EVENING_YEAR_URL),
    ]


def dedupe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        deduped[(row["draw_date"], row["draw_time"])] = row
    return list(deduped.values())


def main() -> None:
    start_dt = datetime.strptime(START_DATE, "%Y-%m-%d")
    today = datetime.now()

    print(f"[ingest] START_DATE={START_DATE}")

    if start_dt < today - timedelta(days=370):
        print("[ingest] WARNING: Lottery USA archive coverage is limited. Older backfills may require an existing DB/CSV seed.")

    os.makedirs(os.path.dirname(DB_PATH) or "data", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    all_rows: List[Dict[str, Any]] = []

    for draw_time, url in iter_source_urls():
        print(f"[ingest] fetching {draw_time}: {url}")
        lines = fetch_lines(url)
        parsed = parse_lotteryusa_page(lines, draw_time, url)
        print(f"[ingest] parsed {len(parsed)} rows for {draw_time} from {url}")
        all_rows.extend(parsed)

    all_rows = dedupe_rows(all_rows)
    all_rows = [r for r in all_rows if datetime.strptime(r["draw_date"], "%Y-%m-%d") >= start_dt]
    all_rows.sort(key=lambda r: (r["draw_date"], 0 if r["draw_time"] == "MIDDAY" else 1))

    if not all_rows:
        raise SystemExit("Ingested 0 rows (source blocked or parsing changed).")

    for r in all_rows:
        upsert(conn, r)

    export_csv(conn, CSV_PATH)
    conn.close()

    print(f"[ingest] Done. Upserted {len(all_rows)} rows into {CSV_PATH} and {DB_PATH}.")


if __name__ == "__main__":
    main()
