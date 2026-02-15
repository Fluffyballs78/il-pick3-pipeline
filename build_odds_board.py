import os
import json
import math
import sqlite3
from collections import deque
from datetime import datetime, timedelta

# -----------------------------
# Config (env overrides)
# -----------------------------
DB_PATH = os.getenv("DB_PATH", "data/pick3.sqlite")
OUT_DIR = os.getenv("OUT_DIR", "data")
TABLE_NAME = os.getenv("TABLE_NAME", "")  # optional; auto-detect if blank

# Momentum windows (in draws)
W10, W25, W50, W100 = 10, 25, 50, 100

# Drought buckets (in draws since last seen)
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
    """
    Find a table that contains:
      draw_date, draw_time, pick3_d1, pick3_d2, pick3_d3, fireball
    """
    if TABLE_NAME:
        return TABLE_NAME

    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]

    required = {"draw_date", "draw_time", "pick3_d1", "pick3_d2", "pick3_d3", "fireball"}
    for t in tables:
        cur2 = conn.execute(f"PRAGMA table_info({t})")
        cols = {r[1] for r in cur2.fetchall()}
        if required.issubset(cols):
            return t

    raise RuntimeError(
        "Could not auto-detect Pick3+Fireball table. "
        "Set TABLE_NAME env var to the correct table."
    )

def load_draws(conn: sqlite3.Connection, table: str):
    """
    Load all draws in canonical order: draw_date ASC, MIDDAY then EVENING.
    Include fireball so we treat this as a 4-digit event.
    """
    q = f"""
    SELECT draw_date, draw_time, pick3_d1, pick3_d2, pick3_d3, fireball
    FROM {table}
    ORDER BY
      draw_date ASC,
      CASE WHEN draw_time='MIDDAY' THEN 0 ELSE 1 END ASC
    """
    rows = conn.execute(q).fetchall()
    if not rows:
        raise RuntimeError("No rows found in database table.")
    return rows

def digits_present_4(d1, d2, d3, fb):
    """
    Presence indicator across FOUR digits: d1,d2,d3,fireball.
    """
    present = [0] * 10
    present[d1] = 1
    present[d2] = 1
    present[d3] = 1
    # fb can be NULL in rare cases; guard it
    if fb is not None:
        present[int(fb)] = 1
    return present

def infer_next_draw_slot(last_date: str, last_time: str):
    """
    Two draws/day: MIDDAY then EVENING.
    If last was MIDDAY -> next is EVENING same date.
    If last was EVENING -> next is MIDDAY next date.
    """
    if last_time == "MIDDAY":
        return last_date, "EVENING"
    # evening -> next day midday
    dt = datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)
    return dt.strftime("%Y-%m-%d"), "MIDDAY"

def pass1_learn_drought_lift(rows):
    """
    Learn P(hit | drought bucket) over ALL digits pooled,
    treating each draw as FOUR digits (Pick3+Fireball).
    For each draw, for each digit, we look at drought state BEFORE the draw,
    then whether it appeared in this draw.
    """
    last_seen = [-10**9] * 10
    bucket_totals = {b[2]: 0 for b in BUCKETS}
    bucket_hits = {b[2]: 0 for b in BUCKETS}

    total_digit_events = 0      # sum of pres[d] across all digits, all draws
    total_draw_digit_slots = 0  # draws * 10 digits

    for i, (_date, _time, d1, d2, d3, fb) in enumerate(rows):
        pres = digits_present_4(d1, d2, d3, fb)

        for digit in range(10):
            drought = i - last_seen[digit] - 1
            if last_seen[digit] < -1e8:
                drought = 10**9
            b = bucket_for(drought)
            bucket_totals[b] += 1
            bucket_hits[b] += pres[digit]
            total_draw_digit_slots += 1
            total_digit_events += pres[digit]

        # update last_seen after evaluating drought state for this draw
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
        "digit_event_definition": "present at least once in 4-digit draw (Pick3 digits + Fireball digit)",
    }

def pass2_backtest_and_latest(rows, drought_model):
    """
    Backtest Top-4: for each draw t, build odds board from history up to t-1 and predict draw t.
    Here, "hit" means a digit appears in ANY of the FOUR digits (Pick3+Fireball).

    Also compute latest board for NEXT draw after last row.
    """
    lift = drought_model["bucket_lift"]

    # rolling deques per digit
    dq10 = [deque(maxlen=W10) for _ in range(10)]
    dq25 = [deque(maxlen=W25) for _ in range(10)]
    dq50 = [deque(maxlen=W50) for _ in range(10)]
    dq100 = [deque(maxlen=W100) for _ in range(10)]

    # rolling sums per digit
    s10 = [0] * 10
    s25 = [0] * 10
    s50 = [0] * 10
    s100 = [0] * 10

    last_seen = [-10**9] * 10

    def push_digit(digit, val):
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
        # momentum features
        r10, r25, r50 = [], [], []
        for d in range(10):
            r10.append(current_rate(s10[d], dq10[d]))
            r25.append(current_rate(s25[d], dq25[d]))
            r50.append(current_rate(s50[d], dq50[d]))

        z10 = zscores(r10)
        z25 = zscores(r25)
        z50 = zscores(r50)

        drought_scores = []
        droughts = []
        buckets = []
        for d in range(10):
            drought = t_index - last_seen[d] - 1
            if last_seen[d] < -1e8:
                drought = 10**9
            b = bucket_for(drought)
            l = max(lift.get(b, 1.0), 1e-6)
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
        order = sorted(range(10), key=lambda x: probs[x], reverse=True)
        top4 = order[:4]

        return {
            "probs": probs,
            "scores": scores,
            "top4": top4,
            "droughts": droughts,
            "drought_buckets": buckets,
            "momentum": {"rate10": r10, "rate25": r25, "rate50": r50},
        }

    # Backtest distribution: how many of Top4 appeared in the 4-digit draw (0..4)
    dist = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    total_preds = 0

    for t, (draw_date, draw_time, d1, d2, d3, fb) in enumerate(rows):
        pres = digits_present_4(d1, d2, d3, fb)

        # predict draw t using state before observing it
        if t >= 1:
            board = odds_board_for_next_draw(t)
            top4 = set(board["top4"])
            hits = sum(1 for d in top4 if pres[d] == 1)
            if hits > 4:
                hits = 4
            dist[hits] += 1
            total_preds += 1

        # update rolling and last_seen after observing this draw
        for digit in range(10):
            val = pres[digit]
            push_digit(digit, val)
            if val == 1:
                last_seen[digit] = t

    # Latest board for NEXT draw
    next_index = len(rows)
    latest_board = odds_board_for_next_draw(next_index)

    last_draw = rows[-1]
    last_date, last_time = last_draw[0], last_draw[1]
    next_date, next_time = infer_next_draw_slot(last_date, last_time)

    summary = {
        "total_predictions": total_preds,
        "top4_hit_rate_any": (1.0 - dist[0] / total_preds) if total_preds else None,
        "distribution_next_draw_hits_in_top4": dist,
        "hit_count_mean": (
            (0*dist[0] + 1*dist[1] + 2*dist[2] + 3*dist[3] + 4*dist[4]) / total_preds
        ) if total_preds else None,
        "digit_event_definition": "Top4 evaluated vs presence in 4-digit draw (Pick3 digits + Fireball digit)",
    }

    latest = {
        "last_observed_draw": {
            "draw_date": last_date,
            "draw_time": last_time,
            "pick4_like": f"{last_draw[2]}{last_draw[3]}{last_draw[4]}{'' if last_draw[5] is None else int(last_draw[5])}",
            "pick3": f"{last_draw[2]}{last_draw[3]}{last_draw[4]}",
            "fireball": None if last_draw[5] is None else int(last_draw[5]),
        },
        "next_expected_draw": {
            "draw_date": next_date,
            "draw_time": next_time,
            "note": "Two draws/day (MIDDAY then EVENING). This is the next draw slot in sequence.",
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
        },
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

    with open(os.path.join(OUT_DIR, "model_drought_lift.json"), "w", encoding="utf-8") as f:
        json.dump(drought_model, f, indent=2)
    with open(os.path.join(OUT_DIR, "model_backtest_top4_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(OUT_DIR, "odds_board_latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, indent=2)

    print("[model] wrote outputs to data/")
    print(f"[model] backtest total_predictions={summary['total_predictions']}, any_hit_rate={summary['top4_hit_rate_any']}")

if __name__ == "__main__":
    main()
