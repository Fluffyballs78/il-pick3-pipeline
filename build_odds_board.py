import os
import json
import math
import sqlite3
from collections import deque

# -----------------------------
# Config (env overrides)
# -----------------------------
DB_PATH = os.getenv("DB_PATH", "data/pick3.sqlite")
OUT_DIR = os.getenv("OUT_DIR", "data")
TABLE_NAME = os.getenv("TABLE_NAME", "")  # optional; auto-detect if blank

# windows for momentum
W10, W25, W50, W100 = 10, 25, 50, 100

# drought buckets (in draws since last seen)
BUCKETS = [
    (0, 2, "0-2"),
    (3, 5, "3-5"),
    (6, 10, "6-10"),
    (11, 20, "11-20"),
    (21, 40, "21-40"),
    (41, 10**9, "41+"),
]

def bucket_for(drought: int) -> str:
    for lo, hi, name in BUCKETS:
        if lo <= drought <= hi:
            return name
    return "41+"

def zscores(vals):
    """Return z-scores for a list of numbers; if stdev ~ 0, return zeros."""
    n = len(vals)
    if n == 0:
        return []
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    sd = math.sqrt(var)
    if sd < 1e-12:
        return [0.0] * n
    return [(v - mean) / sd for v in vals]

def softmax(scores):
    m = max(scores)
    exps = [math.exp(s - m) for s in scores]
    denom = sum(exps)
    if denom <= 0:
        return [1.0 / len(scores)] * len(scores)
    return [e / denom for e in exps]

def ensure_outdir():
    os.makedirs(OUT_DIR, exist_ok=True)

def detect_table(conn: sqlite3.Connection) -> str:
    """Find a table that contains pick3_d1/pick3_d2/pick3_d3 + draw_date + draw_time."""
    if TABLE_NAME:
        return TABLE_NAME

    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]

    required = {"draw_date", "draw_time", "pick3_d1", "pick3_d2", "pick3_d3"}
    for t in tables:
        cur2 = conn.execute(f"PRAGMA table_info({t})")
        cols = {r[1] for r in cur2.fetchall()}
        if required.issubset(cols):
            return t

    raise RuntimeError(
        "Could not auto-detect Pick 3 table. "
        "Set TABLE_NAME env var to the correct table."
    )

def load_draws(conn: sqlite3.Connection, table: str):
    """Load all draws in canonical order: draw_date ASC, MIDDAY then EVENING."""
    q = f"""
    SELECT draw_date, draw_time, pick3_d1, pick3_d2, pick3_d3
    FROM {table}
    ORDER BY
      draw_date ASC,
      CASE WHEN draw_time='MIDDAY' THEN 0 ELSE 1 END ASC
    """
    rows = conn.execute(q).fetchall()
    if not rows:
        raise RuntimeError("No rows found in database table.")
    return rows

def digits_present(d1, d2, d3):
    present = [0]*10
    present[d1] = 1
    present[d2] = 1
    present[d3] = 1
    return present

def pass1_learn_drought_lift(rows):
    """
    Learn P(hit | drought bucket) over ALL digits pooled.
    For each draw, for each digit, we look at drought state BEFORE the draw,
    then whether it hit in this draw.
    """
    last_seen = [-10**9] * 10
    bucket_totals = {b[2]: 0 for b in BUCKETS}
    bucket_hits = {b[2]: 0 for b in BUCKETS}

    # overall baseline for "digit appears at least once in a draw"
    total_digit_events = 0  # 10 per draw possibilities, but hits vary
    total_draw_digit_slots = 0  # draws * 10 digits

    for i, (_date, _time, d1, d2, d3) in enumerate(rows):
        pres = digits_present(d1, d2, d3)
        for digit in range(10):
            drought = i - last_seen[digit] - 1
            if last_seen[digit] < -1e8:
                drought = 10**9  # "never seen yet" bucket -> 41+
            b = bucket_for(drought)
            bucket_totals[b] += 1
            bucket_hits[b] += pres[digit]
            total_draw_digit_slots += 1
            total_digit_events += pres[digit]

        # update last_seen after evaluating bucket for this draw
        for digit in range(10):
            if pres[digit] == 1:
                last_seen[digit] = i

    baseline = total_digit_events / total_draw_digit_slots if total_draw_digit_slots else 0.0
    lift = {}
    for b in bucket_totals:
        p = (bucket_hits[b] / bucket_totals[b]) if bucket_totals[b] else 0.0
        lift[b] = (p / baseline) if baseline > 0 else 1.0

    return {
        "baseline": baseline,
        "bucket_totals": bucket_totals,
        "bucket_hits": bucket_hits,
        "bucket_lift": lift,
    }

def pass2_backtest_and_latest(rows, drought_model):
    """
    Backtest Top-4: for each draw t (starting after enough history),
    build odds board from history up to t-1 and predict draw t.
    Also compute latest board for NEXT draw after last row.
    """
    baseline = drought_model["baseline"]
    lift = drought_model["bucket_lift"]

    # rolling deques per digit
    dq10 = [deque(maxlen=W10) for _ in range(10)]
    dq25 = [deque(maxlen=W25) for _ in range(10)]
    dq50 = [deque(maxlen=W50) for _ in range(10)]
    dq100 = [deque(maxlen=W100) for _ in range(10)]

    # rolling sums per digit (so we don't sum deques each time)
    s10 = [0]*10
    s25 = [0]*10
    s50 = [0]*10
    s100 = [0]*10

    # last seen and cumulative counts (baseline-by-digit if you want later)
    last_seen = [-10**9] * 10
    cum_hits = [0]*10
    cum_draws = 0

    # backtest stats: number of digits (0..3) from Top4 that appeared in next draw
    dist = {0: 0, 1: 0, 2: 0, 3: 0}
    total_preds = 0

    # helper to push rolling window
    def push_digit(digit, val):
        nonlocal dq10, dq25, dq50, dq100, s10, s25, s50, s100

        # 10
        if len(dq10[digit]) == dq10[digit].maxlen:
            s10[digit] -= dq10[digit][0]
        dq10[digit].append(val)
        s10[digit] += val

        # 25
        if len(dq25[digit]) == dq25[digit].maxlen:
            s25[digit] -= dq25[digit][0]
        dq25[digit].append(val)
        s25[digit] += val

        # 50
        if len(dq50[digit]) == dq50[digit].maxlen:
            s50[digit] -= dq50[digit][0]
        dq50[digit].append(val)
        s50[digit] += val

        # 100
        if len(dq100[digit]) == dq100[digit].maxlen:
            s100[digit] -= dq100[digit][0]
        dq100[digit].append(val)
        s100[digit] += val

    def current_rate(sumv, dq):
        ln = len(dq)
        return (sumv / ln) if ln else 0.0

    def odds_board_for_next_draw(t_index):
        """
        Build p_k for digits 0..9 using history up to t_index-1.
        Uses:
          - z(momentum rates)
          - drought lift (log)
        """
        # momentum features per digit
        r10 = []
        r25 = []
        r50 = []
        for d in range(10):
            r10.append(current_rate(s10[d], dq10[d]))
            r25.append(current_rate(s25[d], dq25[d]))
            r50.append(current_rate(s50[d], dq50[d]))

        z10 = zscores(r10)
        z25 = zscores(r25)
        z50 = zscores(r50)

        # drought feature (state BEFORE draw t_index)
        drought_scores = []
        droughts = []
        buckets = []
        for d in range(10):
            drought = t_index - last_seen[d] - 1
            if last_seen[d] < -1e8:
                drought = 10**9
            b = bucket_for(drought)
            # log lift so it combines additively
            l = lift.get(b, 1.0)
            l = max(l, 1e-6)
            drought_scores.append(math.log(l))
            droughts.append(drought if drought < 10**8 else None)
            buckets.append(b)

        # Weighted score (tunable)
        scores = []
        for d in range(10):
            score = (
                0.40 * z25[d]
                + 0.25 * z50[d]
                + 0.15 * z10[d]
                + 0.20 * drought_scores[d]
            )
            scores.append(score)

        probs = softmax(scores)

        # top4 digits
        order = sorted(range(10), key=lambda x: probs[x], reverse=True)
        top4 = order[:4]

        return {
            "probs": probs,
            "scores": scores,
            "top4": top4,
            "droughts": droughts,
            "drought_buckets": buckets,
            "momentum": {
                "rate10": r10,
                "rate25": r25,
                "rate50": r50,
            },
        }

    # We can start predicting once we have at least W50 history, but it's optional.
    # We'll start at draw index 1 so we can still record something; momentum will be sparse early.
    for t, (draw_date, draw_time, d1, d2, d3) in enumerate(rows):
        pres = digits_present(d1, d2, d3)

        if t >= 1:
            board = odds_board_for_next_draw(t)  # predict draw t using state before it
            top4 = set(board["top4"])
            hits = sum(1 for d in top4 if pres[d] == 1)
            if hits > 3:
                hits = 3
            dist[hits] += 1
            total_preds += 1

        # update rolling + last_seen + cumulative after observing this draw
        for digit in range(10):
            val = pres[digit]
            push_digit(digit, val)
            if val == 1:
                last_seen[digit] = t
                cum_hits[digit] += 1
        cum_draws += 1

    # Latest board for NEXT draw (after final row)
    next_index = len(rows)
    latest_board = odds_board_for_next_draw(next_index)
    last_draw = rows[-1]

    # Backtest summary
    summary = {
        "total_predictions": total_preds,
        "top4_hit_rate_any": (1.0 - dist[0] / total_preds) if total_preds else None,
        "distribution_next_draw_hits_in_top4": dist,
        "notes": {
            "hit_count_mean": (
                (0*dist[0] + 1*dist[1] + 2*dist[2] + 3*dist[3]) / total_preds
            ) if total_preds else None
        }
    }

    latest = {
        "last_observed_draw": {
            "draw_date": last_draw[0],
            "draw_time": last_draw[1],
            "pick3": f"{last_draw[2]}{last_draw[3]}{last_draw[4]}",
        },
        "next_draw_odds": {
            "digits": [
                {
                    "digit": d,
                    "prob": round(latest_board["probs"][d], 6),
                    "score": round(latest_board["scores"][d], 6),
                    "drought_draws": latest_board["droughts"][d],
                    "drought_bucket": latest_board["drought_buckets"][d],
                    "rate10": round(latest_board["momentum"]["rate10"][d], 6),
                    "rate25": round(latest_board["momentum"]["rate25"][d], 6),
                    "rate50": round(latest_board["momentum"]["rate50"][d], 6),
                }
                for d in range(10)
            ],
            "top4": latest_board["top4"],
        }
    }

    return summary, latest

def main():
    ensure_outdir()

    conn = sqlite3.connect(DB_PATH)
    try:
        table = detect_table(conn)
        rows = load_draws(conn, table)
    finally:
        conn.close()

    drought_model = pass1_learn_drought_lift(rows)
    summary, latest = pass2_backtest_and_latest(rows, drought_model)

    # Write outputs
    ensure_outdir()
    with open(os.path.join(OUT_DIR, "model_drought_lift.json"), "w", encoding="utf-8") as f:
        json.dump(drought_model, f, indent=2)
    with open(os.path.join(OUT_DIR, "model_backtest_top4_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(OUT_DIR, "odds_board_latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, indent=2)

    print("[model] Wrote:")
    print(f"  - {os.path.join(OUT_DIR, 'model_drought_lift.json')}")
    print(f"  - {os.path.join(OUT_DIR, 'model_backtest_top4_summary.json')}")
    print(f"  - {os.path.join(OUT_DIR, 'odds_board_latest.json')}")
    print(f"[model] Backtest total_predictions={summary['total_predictions']}, any_hit_rate={summary['top4_hit_rate_any']}")

if __name__ == "__main__":
    main()
