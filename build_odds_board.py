import os
import json
import math
import sqlite3
import itertools
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

def digits_set_4(d1, d2, d3, fb):
    s = {int(d1), int(d2), int(d3)}
    if fb is not None:
        s.add(int(fb))
    return s

def digits_present_4(d1, d2, d3, fb):
    present = [0] * 10
    present[int(d1)] = 1
    present[int(d2)] = 1
    present[int(d3)] = 1
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
    dt = datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)
    return dt.strftime("%Y-%m-%d"), "MIDDAY"

# -----------------------------
# 2+ scoring (optimize P(2+ hits))
# -----------------------------
def p2plus_from_q(q: float, n_slots: int = 4) -> float:
    """
    Approximate P(X>=2) where X~Binomial(n_slots, q).
    Here, n_slots=4 because the project evaluates presence across 4 digits
    (Pick3 digits + Fireball digit).
    """
    if q <= 0.0:
        return 0.0
    if q >= 1.0:
        return 1.0
    n = n_slots
    # P(X>=2) = 1 - P(0) - P(1)
    p0 = (1 - q) ** n
    p1 = n * q * ((1 - q) ** (n - 1))
    return max(0.0, min(1.0, 1.0 - p0 - p1))

def best_set_for_2plus(p_digits, m: int = 4, regime_r: int = 2, per_regime_multiplier=None, n_slots: int = 4):
    """
    Brute-force all combos of size m (C(10, m) is tiny) and pick the set
    maximizing P(2+ hits), with an optional regime multiplier.
    """
    M = 1.0
    if per_regime_multiplier:
        M = float(per_regime_multiplier.get(regime_r, 1.0))

    best = None
    best_score = -1.0
    best_base = -1.0

    for S in itertools.combinations(range(10), m):
        q = sum(float(p_digits[d]) for d in S)
        base = p2plus_from_q(q, n_slots=n_slots)
        score = min(0.999999, base * M)
        if score > best_score:
            best_score = score
            best_base = base
            best = S

    return list(best), best_score, best_base

# -----------------------------
# Structural conditioning (regime-learned)
# -----------------------------
def learn_repeat_regime_multipliers(rows):
    """
    We learn a regime based on the *previous* transition repeat count:
      regime r = |set(t-1) ∩ set(t)|  where sets are 4-digit unique digit sets.

    Then we measure: for transitions t -> t+1, among digits present in set(t),
    how often do they repeat into set(t+1), conditioned on regime r?

    Output:
      - overall_repeat_given_present
      - per_regime_repeat_given_present[r]
      - per_regime_multiplier[r] = per_regime / overall
    """
    if len(rows) < 3:
        return {
            "overall_repeat_given_present": None,
            "per_regime_repeat_given_present": {},
            "per_regime_multiplier": {r: 1.0 for r in range(5)},
            "notes": "Not enough data to learn structural repeat regime."
        }

    # Build sets for each draw
    sets = [digits_set_4(*r[2:6]) for r in rows]

    # For each t in [1..n-2], define regime based on (t-1 -> t),
    # then evaluate repeats from (t -> t+1)
    total_present_by_regime = {r: 0 for r in range(5)}  # sum of |set(t)| across transitions in regime
    repeat_hits_by_regime = {r: 0 for r in range(5)}    # sum of |set(t) ∩ set(t+1)| across transitions in regime

    total_present_all = 0
    repeat_hits_all = 0

    for t in range(1, len(sets) - 1):
        prev_repeats = len(sets[t - 1].intersection(sets[t]))
        prev_repeats = max(0, min(4, prev_repeats))  # clamp 0..4

        now_present = len(sets[t])
        now_repeats = len(sets[t].intersection(sets[t + 1]))

        total_present_by_regime[prev_repeats] += now_present
        repeat_hits_by_regime[prev_repeats] += now_repeats

        total_present_all += now_present
        repeat_hits_all += now_repeats

    overall = (repeat_hits_all / total_present_all) if total_present_all else 0.0

    per_regime = {}
    mult = {}
    for r in range(5):
        p = (repeat_hits_by_regime[r] / total_present_by_regime[r]) if total_present_by_regime[r] else 0.0
        per_regime[r] = p
        if overall > 0:
            mult[r] = p / overall
        else:
            mult[r] = 1.0

    return {
        "overall_repeat_given_present": overall,
        "per_regime_repeat_given_present": per_regime,
        "per_regime_multiplier": mult,
        "regime_definition": "prev_repeats = |set(t-1) ∩ set(t)| using 4-digit digit-sets",
        "measurement_definition": "P(digit repeats into next draw | digit present in current draw, conditioned on prev_repeats regime)",
        "counts": {
            "total_present_by_regime": total_present_by_regime,
            "repeat_hits_by_regime": repeat_hits_by_regime,
            "total_present_all": total_present_all,
            "repeat_hits_all": repeat_hits_all,
        },
    }

def current_prev_repeat_regime(rows):
    """
    Determine the current regime r = |set(last-1) ∩ set(last)|.
    """
    if len(rows) < 2:
        return 0, set(), set()
    s_prev = digits_set_4(*rows[-2][2:6])
    s_last = digits_set_4(*rows[-1][2:6])
    r = len(s_prev.intersection(s_last))
    r = max(0, min(4, r))
    return r, s_prev, s_last

# -----------------------------
# Drought lift (pooled)
# -----------------------------
def pass1_learn_drought_lift(rows):
    """
    Learn P(hit | drought bucket) pooled across digits,
    treating each draw as FOUR digits (Pick3+Fireball).

    For each draw, for each digit, we look at drought state BEFORE the draw,
    then whether it appeared in this draw.
    """
    last_seen = [-10**9] * 10
    bucket_totals = {b[2]: 0 for b in BUCKETS}
    bucket_hits = {b[2]: 0 for b in BUCKETS}

    total_digit_events = 0
    total_draw_digit_slots = 0

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

# -----------------------------
# Backtest + latest odds board
# -----------------------------
def pass2_backtest_and_latest(rows, drought_model, repeat_model):
    """
    Combined model, 4-digit event.
    Adds a *regime-learned repeat boost* for digits in the last draw:
      repeat_bonus = log(multiplier[current_regime]) if digit was in last draw else 0

    Backtest Top4: how many of Top4 appear in the 4-digit draw (0..4).

    NEW: Top4 selection is optimized for P(2+ hits), not "top-4 by prob".
    """
    lift = drought_model["bucket_lift"]
    repeat_mult = repeat_model["per_regime_multiplier"]

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

    # sets per draw for quick overlap
    sets = [digits_set_4(*r[2:6]) for r in rows]

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

    def odds_board_for_next_draw(t_index, last_draw_set, regime_r):
        # momentum
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

        # structural repeat bonus for digits in last draw
        # multiplier is learned for the current regime r
        m = max(repeat_mult.get(regime_r, 1.0), 1e-6)
        repeat_bonus = math.log(m)

        scores = []
        for d in range(10):
            struct = repeat_bonus if d in last_draw_set else 0.0
            score = (
                0.40 * z25[d]
                + 0.25 * z50[d]
                + 0.15 * z10[d]
                + 0.15 * drought_scores[d]
                + 0.05 * struct  # small weight; the multiplier already reflects history
            )
            scores.append(score)

        probs = softmax(scores)

        # Keep 'order' for diagnostics
        order = sorted(range(10), key=lambda x: probs[x], reverse=True)

        # NEW: choose Top4 by maximizing P(2+ hits) under current regime
        top4, top4_score, top4_score_base = best_set_for_2plus(
            p_digits=probs,
            m=4,
            regime_r=regime_r,
            per_regime_multiplier=repeat_mult,
            n_slots=4
        )

        return {
            "probs": probs,
            "scores": scores,
            "order": order,
            "top4": top4,
            "top4_2plus_score": top4_score,
            "top4_2plus_score_base": top4_score_base,
            "droughts": droughts,
            "drought_buckets": buckets,
            "momentum": {"rate10": r10, "rate25": r25, "rate50": r50},
            "structural": {
                "prev_repeat_regime": regime_r,
                "regime_multiplier": m,
                "repeat_bonus_log": repeat_bonus,
                "last_draw_digits": sorted(list(last_draw_set)),
            }
        }

    # Backtest: 0..4 hits in top4 vs 4-digit draw
    dist = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    total_preds = 0

    # We can only compute a regime starting at t>=1 (needs t-1 and t)
    for t in range(1, len(rows)):
        # predict draw t using info up to t-1
        last_draw_set = sets[t - 1]

        # regime uses overlap between (t-2) and (t-1) if available
        if t - 2 >= 0:
            regime_r = len(sets[t - 2].intersection(sets[t - 1]))
        else:
            regime_r = 0
        regime_r = max(0, min(4, regime_r))

        # build board for draw t
        board = odds_board_for_next_draw(t, last_draw_set, regime_r)

        # actual draw t presence
        pres = digits_present_4(*rows[t][2:6])

        hits = sum(1 for d in board["top4"] if pres[d] == 1)
        dist[min(4, hits)] += 1
        total_preds += 1

        # now update rolling/drought state based on observed draw t
        for digit in range(10):
            val = pres[digit]
            push_digit(digit, val)
            if val == 1:
                last_seen[digit] = t

    # Latest odds for NEXT draw after last row
    last_draw = rows[-1]
    last_date, last_time = last_draw[0], last_draw[1]
    next_date, next_time = infer_next_draw_slot(last_date, last_time)

    current_regime_r, prev_set, last_set = current_prev_repeat_regime(rows)

    latest_board = odds_board_for_next_draw(
        len(rows),  # next index
        last_set,
        current_regime_r
    )

    top4_hit_rate_any = (1.0 - dist[0] / total_preds) if total_preds else None
    top4_hit_rate_2plus = ((dist[2] + dist[3] + dist[4]) / total_preds) if total_preds else None

    summary = {
        "total_predictions": total_preds,
        "top4_hit_rate_any": top4_hit_rate_any,
        "top4_hit_rate_2plus": top4_hit_rate_2plus,
        "distribution_next_draw_hits_in_top4": dist,
        "hit_count_mean": (
            (0*dist[0] + 1*dist[1] + 2*dist[2] + 3*dist[3] + 4*dist[4]) / total_preds
        ) if total_preds else None,
        "digit_event_definition": "Top4 evaluated vs presence in 4-digit draw (Pick3 digits + Fireball digit)",
        "objective": "Top4 selection optimized for P(2+ hits) (approx Binomial over 4 slots), scaled by repeat-regime multiplier",
        "structural_conditioning": {
            "enabled": True,
            "regime_definition": repeat_model.get("regime_definition"),
            "overall_repeat_given_present": repeat_model.get("overall_repeat_given_present"),
            "per_regime_multiplier": repeat_mult,
            "current_prev_repeat_regime": current_regime_r,
            "current_prev_transition_overlap_digits": sorted(list(prev_set.intersection(last_set))),
            "current_prev_transition_overlap_count": len(prev_set.intersection(last_set)),
        }
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
        "structural_context": {
            "prev_draw_digits": sorted(list(prev_set)),
            "last_draw_digits": sorted(list(last_set)),
            "prev_to_last_overlap_digits": sorted(list(prev_set.intersection(last_set))),
            "prev_to_last_overlap_count": len(prev_set.intersection(last_set)),
            "prev_repeat_regime": current_regime_r,
            "regime_multiplier": repeat_mult.get(current_regime_r, 1.0),
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
                    "in_last_draw": 1 if d in last_set else 0,
                }
                for d in range(10)
            ],
            "top4": latest_board["top4"],
            "top4_2plus_score": round(float(latest_board["top4_2plus_score"]), 6),
            "top4_2plus_score_base": round(float(latest_board["top4_2plus_score_base"]), 6),
            "order_by_prob": latest_board["order"],
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
    repeat_model = learn_repeat_regime_multipliers(rows)
    summary, latest = pass2_backtest_and_latest(rows, drought_model, repeat_model)

    # Persist models + latest board
    with open(os.path.join(OUT_DIR, "model_drought_lift.json"), "w", encoding="utf-8") as f:
        json.dump(drought_model, f, indent=2)

    with open(os.path.join(OUT_DIR, "model_repeat_regime.json"), "w", encoding="utf-8") as f:
        json.dump(repeat_model, f, indent=2)

    with open(os.path.join(OUT_DIR, "model_backtest_top4_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    with open(os.path.join(OUT_DIR, "odds_board_latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, indent=2)

    print("[model] wrote outputs to data/")
    print(f"[model] backtest total_predictions={summary['total_predictions']}, any_hit_rate={summary['top4_hit_rate_any']}, hit2plus_rate={summary['top4_hit_rate_2plus']}")
    print(f"[model] structural conditioning enabled: per-regime multipliers saved to data/model_repeat_regime.json")

if __name__ == "__main__":
    main()
