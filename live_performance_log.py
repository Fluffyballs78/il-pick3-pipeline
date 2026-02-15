import os
import csv
import math
import sqlite3
from collections import deque
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "data/pick3.sqlite")
OUT_DIR = os.getenv("OUT_DIR", "data")
TABLE_NAME = os.getenv("TABLE_NAME", "")  # optional; auto-detect if blank
OUT_CSV = os.getenv("LIVE_LOG_PATH", os.path.join(OUT_DIR, "live_performance_log.csv"))

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
    raise RuntimeError("Could not auto-detect Pick3+Fireball table. Set TABLE_NAME env var.")

def load_draws(conn: sqlite3.Connection, table: str):
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

def digits_set_4(d1, d2, d3, fb):
    s = {int(d1), int(d2), int(d3)}
    if fb is not None:
        s.add(int(fb))
    return s

def digits_present_4(d1, d2, d3, fb):
    pres = [0] * 10
    pres[int(d1)] = 1
    pres[int(d2)] = 1
    pres[int(d3)] = 1
    if fb is not None:
        pres[int(fb)] = 1
    return pres

def learn_drought_lift(rows):
    last_seen = [-10**9] * 10
    bucket_totals = {b[2]: 0 for b in BUCKETS}
    bucket_hits = {b[2]: 0 for b in BUCKETS}

    total_digit_events = 0
    total_slots = 0

    for i, (_date, _time, d1, d2, d3, fb) in enumerate(rows):
        pres = digits_present_4(d1, d2, d3, fb)
        for digit in range(10):
            drought = i - last_seen[digit] - 1
            if last_seen[digit] < -1e8:
                drought = 10**9
            b = bucket_for(drought)
            bucket_totals[b] += 1
            bucket_hits[b] += pres[digit]
            total_slots += 1
            total_digit_events += pres[digit]
        for digit in range(10):
            if pres[digit] == 1:
                last_seen[digit] = i

    baseline = total_digit_events / total_slots if total_slots else 0.0
    lift = {}
    for b in bucket_totals:
        p = (bucket_hits[b] / bucket_totals[b]) if bucket_totals[b] else 0.0
        lift[b] = (p / baseline) if baseline > 0 else 1.0
    return lift

def learn_repeat_regime_multipliers(rows):
    if len(rows) < 3:
        return {r: 1.0 for r in range(5)}
    sets = [digits_set_4(*r[2:6]) for r in rows]

    total_present_by_regime = {r: 0 for r in range(5)}
    repeat_hits_by_regime = {r: 0 for r in range(5)}
    total_present_all = 0
    repeat_hits_all = 0

    for t in range(1, len(sets) - 1):
        prev_repeats = len(sets[t - 1].intersection(sets[t]))
        prev_repeats = max(0, min(4, prev_repeats))

        now_present = len(sets[t])
        now_repeats = len(sets[t].intersection(sets[t + 1]))

        total_present_by_regime[prev_repeats] += now_present
        repeat_hits_by_regime[prev_repeats] += now_repeats
        total_present_all += now_present
        repeat_hits_all += now_repeats

    overall = (repeat_hits_all / total_present_all) if total_present_all else 0.0
    mult = {}
    for r in range(5):
        p = (repeat_hits_by_regime[r] / total_present_by_regime[r]) if total_present_by_regime[r] else 0.0
        mult[r] = (p / overall) if overall > 0 else 1.0
    return mult

def p2plus_from_q(q: float, n_slots: int = 4) -> float:
    if q <= 0.0:
        return 0.0
    if q >= 1.0:
        return 1.0
    # 1 - P0 - P1 for Binomial(n, q)
    p0 = (1 - q) ** n_slots
    p1 = n_slots * q * ((1 - q) ** (n_slots - 1))
    return max(0.0, min(1.0, 1.0 - p0 - p1))

def best_set_for_2plus(p_digits, regime_r: int, repeat_mult: dict, n_slots: int = 4):
    # brute-force all 4-digit combos
    M = float(repeat_mult.get(regime_r, 1.0))
    best = None
    best_score = -1.0
    for a in range(10):
        for b in range(a+1, 10):
            for c in range(b+1, 10):
                for d in range(c+1, 10):
                    S = (a,b,c,d)
                    q = float(p_digits[a] + p_digits[b] + p_digits[c] + p_digits[d])
                    base = p2plus_from_q(q, n_slots=n_slots)
                    score = min(0.999999, base * M)
                    if score > best_score:
                        best_score = score
                        best = S
    return list(best), best_score

def write_live_log(rows):
    os.makedirs(OUT_DIR, exist_ok=True)

    # Learn pooled models from full history (same as build_odds_board)
    drought_lift = learn_drought_lift(rows)
    repeat_mult = learn_repeat_regime_multipliers(rows)

    # Production structural repeat weight schedule (Variant B)
    STRUCT_W_BY_REGIME = {0: 0.02, 1: 0.05, 2: 0.09, 3: 0.04, 4: 0.08}
    DEFAULT_STRUCT_W = 0.05

    # rolling deques per digit
    dq10 = [deque(maxlen=W10) for _ in range(10)]
    dq25 = [deque(maxlen=W25) for _ in range(10)]
    dq50 = [deque(maxlen=W50) for _ in range(10)]

    s10 = [0] * 10
    s25 = [0] * 10
    s50 = [0] * 10

    last_seen = [-10**9] * 10
    sets = [digits_set_4(*r[2:6]) for r in rows]

    def push(digit, val):
        if len(dq10[digit]) == dq10[digit].maxlen:
            s10[digit] -= dq10[digit][0]
        dq10[digit].append(val); s10[digit] += val

        if len(dq25[digit]) == dq25[digit].maxlen:
            s25[digit] -= dq25[digit][0]
        dq25[digit].append(val); s25[digit] += val

        if len(dq50[digit]) == dq50[digit].maxlen:
            s50[digit] -= dq50[digit][0]
        dq50[digit].append(val); s50[digit] += val

    def rate(sumv, dq):
        return (sumv / len(dq)) if dq else 0.0

    # Prepare output rows
    out_rows = []
    # We can only predict draw t using info up to t-1, starting at t=1
    for t in range(1, len(rows)):
        draw_date, draw_time, d1, d2, d3, fb = rows[t]
        last_draw_set = sets[t-1]

        # regime for predicting t
        if t - 2 >= 0:
            regime_r = len(sets[t-2].intersection(sets[t-1]))
        else:
            regime_r = 0
        regime_r = max(0, min(4, regime_r))

        # momentum
        r10 = [rate(s10[d], dq10[d]) for d in range(10)]
        r25 = [rate(s25[d], dq25[d]) for d in range(10)]
        r50 = [rate(s50[d], dq50[d]) for d in range(10)]
        z10, z25, z50 = zscores(r10), zscores(r25), zscores(r50)

        drought_scores = []
        droughts = []
        buckets = []
        for d in range(10):
            drought = t - last_seen[d] - 1
            if last_seen[d] < -1e8:
                drought = 10**9
            b = bucket_for(drought)
            l = max(drought_lift.get(b, 1.0), 1e-6)
            drought_scores.append(math.log(l))
            droughts.append(None if drought >= 10**8 else drought)
            buckets.append(b)

        # structural repeat bonus for digits in last draw, with regime-aware weight
        m = max(float(repeat_mult.get(regime_r, 1.0)), 1e-6)
        repeat_bonus = math.log(m)
        struct_w = float(STRUCT_W_BY_REGIME.get(regime_r, DEFAULT_STRUCT_W))

        scores = []
        for d in range(10):
            struct = repeat_bonus if d in last_draw_set else 0.0
            score = (
                0.40 * z25[d]
                + 0.25 * z50[d]
                + 0.15 * z10[d]
                + 0.15 * drought_scores[d]
                + struct_w * struct
            )
            scores.append(score)

        probs = softmax(scores)
        top4, _ = best_set_for_2plus(probs, regime_r=regime_r, repeat_mult=repeat_mult, n_slots=4)

        pres = digits_present_4(d1, d2, d3, fb)
        hits = sum(1 for d in top4 if pres[d] == 1)

        out_rows.append({
            "draw_date": draw_date,
            "draw_time": draw_time,
            "actual_pick3": f"{int(d1)}{int(d2)}{int(d3)}",
            "actual_fireball": "" if fb is None else str(int(fb)),
            "regime": regime_r,
            "struct_weight": struct_w,
            "top4": "".join(str(x) for x in top4),
            "hits_in_top4": hits,
            "any_hit": 1 if hits >= 1 else 0,
            "hit_2plus": 1 if hits >= 2 else 0,
        })

        # update rolling state with observed draw t
        for digit in range(10):
            v = pres[digit]
            push(digit, v)
            if v == 1:
                last_seen[digit] = t

    # Write CSV (overwrite each run for simplicity)
    fieldnames = ["draw_date","draw_time","actual_pick3","actual_fireball","regime","struct_weight","top4","hits_in_top4","any_hit","hit_2plus"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    print(f"[ok] wrote {OUT_CSV} rows={len(out_rows)}")

def main():
    conn = sqlite3.connect(DB_PATH)
    try:
        table = detect_table(conn)
        rows = load_draws(conn, table)
    finally:
        conn.close()

    write_live_log(rows)

if __name__ == "__main__":
    main()
