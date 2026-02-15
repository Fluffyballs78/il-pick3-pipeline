import os
import json
import sqlite3
from datetime import datetime
from collections import defaultdict

DB_PATH = os.getenv("DB_PATH", "data/pick3.sqlite")
OUT_DIR = os.getenv("OUT_DIR", "data")
TABLE_NAME = os.getenv("TABLE_NAME", "")  # optional; auto-detect if blank

# Horizons in draws (lookahead windows)
HORIZONS = [1, 2, 3, 5, 10]

# Cap drought to keep tables compact; still keeps long tail via "cap+"
MAX_DROUGHT = 60  # anything > MAX_DROUGHT goes into "60+"

def ensure_outdir():
    os.makedirs(OUT_DIR, exist_ok=True)

def detect_table(conn: sqlite3.Connection) -> str:
    if TABLE_NAME:
        return TABLE_NAME
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    required = {"draw_date", "draw_time", "pick3_d1", "pick3_d2", "pick3_d3", "fireball"}
    for t in tables:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
        if required.issubset(cols):
            return t
    raise RuntimeError("Could not auto-detect draw table. Set TABLE_NAME env var.")

def load_rows(conn: sqlite3.Connection, table: str):
    q = f"""
    SELECT draw_date, draw_time, pick3_d1, pick3_d2, pick3_d3, fireball
    FROM {table}
    ORDER BY
      draw_date ASC,
      CASE WHEN draw_time='MIDDAY' THEN 0 ELSE 1 END ASC
    """
    rows = conn.execute(q).fetchall()
    if not rows:
        raise RuntimeError("No rows found in DB.")
    return rows

def present_set(row):
    _, _, d1, d2, d3, fb = row
    s = {int(d1), int(d2), int(d3)}
    if fb is not None:
        s.add(int(fb))
    return s

def main():
    ensure_outdir()

    conn = sqlite3.connect(DB_PATH)
    try:
        table = detect_table(conn)
        rows = load_rows(conn, table)
    finally:
        conn.close()

    n = len(rows)
    sets = [present_set(r) for r in rows]

    # last seen index per digit
    last_seen = [-10**9] * 10

    # counts[drought_bucket][h] -> (hits, total)
    hits = {h: defaultdict(int) for h in HORIZONS}
    tot = {h: defaultdict(int) for h in HORIZONS}

    # helper to bucket drought
    def drought_bucket(d):
        return f"{MAX_DROUGHT}+" if d > MAX_DROUGHT else str(d)

    for t in range(n):
        # drought BEFORE seeing draw t (i.e., state at time t)
        for d in range(10):
            if last_seen[d] < -1e8:
                drought = 10**9
            else:
                drought = t - last_seen[d] - 1
            b = drought_bucket(drought if drought < 10**8 else 10**9)

            # for each horizon, look ahead and see if digit hits within next H draws
            for h in HORIZONS:
                if t + h > n:  # not enough future draws to evaluate
                    continue
                future_window = sets[t:t+h]
                did_hit = any(d in s for s in future_window)
                tot[h][b] += 1
                if did_hit:
                    hits[h][b] += 1

        # now update last_seen with draw t
        for d in sets[t]:
            last_seen[d] = t

    # Build output tables
    # Collect all buckets observed
    buckets = set()
    for h in HORIZONS:
        buckets.update(tot[h].keys())
    # sort numeric buckets then MAX+
    def bucket_sort_key(b):
        if b.endswith("+"):
            return (10**9, 0)
        try:
            return (int(b), 0)
        except:
            return (10**9-1, 0)
    buckets_sorted = sorted(buckets, key=bucket_sort_key)

    # baseline probability of a digit appearing in a random draw window of size h:
    # estimate from empirical digit presence rate per draw, convert to window "at least once" rate
    # We'll compute directly: for each digit, for each t/h, same as above but ignore drought. However that's heavy.
    # Instead estimate p1 = avg P(d present in a draw). Then baseline window hit ≈ 1 - (1-p1)^h
    # p1 can be estimated by average unique-digit presence across draws divided by 10.
    avg_unique = sum(len(s) for s in sets) / n
    p1 = avg_unique / 10.0
    baseline_by_h = {h: 1.0 - (1.0 - p1) ** h for h in HORIZONS}

    table_out = []
    for b in buckets_sorted:
        row = {"drought": b}
        for h in HORIZONS:
            T = tot[h].get(b, 0)
            Hh = hits[h].get(b, 0)
            p = (Hh / T) if T else None
            row[f"p_hit_next_{h}"] = p
            row[f"n_{h}"] = T
            if p is not None:
                row[f"lift_next_{h}"] = p / baseline_by_h[h] if baseline_by_h[h] > 0 else None
            else:
                row[f"lift_next_{h}"] = None
        table_out.append(row)

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "db_path": DB_PATH,
        "table": table,
        "n_draws": n,
        "definition": "For each digit and time t, drought is draws since last seen BEFORE draw t. p_hit_next_h = P(digit appears at least once within next h draws).",
        "horizons": HORIZONS,
        "max_drought_bucket": MAX_DROUGHT,
        "baseline": {
            "avg_unique_digits_per_draw": avg_unique,
            "per_digit_p1_estimate": p1,
            "baseline_window_hit_by_h": baseline_by_h,
            "note": "Baseline window hit uses p1 estimate: 1-(1-p1)^h."
        },
        "rows": table_out
    }

    out_path = os.path.join(OUT_DIR, "drought_hazard.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"[ok] wrote {out_path}")
    print(f"[ok] horizons={HORIZONS}, buckets={len(table_out)}")

if __name__ == "__main__":
    main()
