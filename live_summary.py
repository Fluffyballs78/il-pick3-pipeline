import os
import json
import csv
from datetime import datetime

OUT_DIR = os.getenv("OUT_DIR", "data")
LIVE_LOG_PATH = os.getenv("LIVE_LOG_PATH", os.path.join(OUT_DIR, "live_performance_log.csv"))
OUT_JSON = os.getenv("LIVE_SUMMARY_PATH", os.path.join(OUT_DIR, "live_summary.json"))

ROLLING_WINDOWS = [20, 50, 100, 200]

def safe_rate(num, den):
    return (num / den) if den else None

def read_log(path):
    with open(path, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        rows = list(r)
    for row in rows:
        row["hits_in_top4"] = int(row["hits_in_top4"])
        row["any_hit"] = int(row["any_hit"])
        row["hit_2plus"] = int(row["hit_2plus"])
        row["regime"] = int(row["regime"])
        try:
            row["struct_weight"] = float(row["struct_weight"])
        except:
            row["struct_weight"] = None
    return rows

def summarize(rows):
    n = len(rows)
    by_regime = {r: {"n": 0, "any_hit": 0, "hit_2plus": 0, "hits_sum": 0} for r in range(5)}
    any_hit = sum(r["any_hit"] for r in rows)
    hit_2plus = sum(r["hit_2plus"] for r in rows)
    hits_sum = sum(r["hits_in_top4"] for r in rows)

    for r in rows:
        rr = by_regime[r["regime"]]
        rr["n"] += 1
        rr["any_hit"] += r["any_hit"]
        rr["hit_2plus"] += r["hit_2plus"]
        rr["hits_sum"] += r["hits_in_top4"]

    by_regime_out = {}
    for k, v in by_regime.items():
        by_regime_out[str(k)] = {
            "n": v["n"],
            "any_hit_rate": safe_rate(v["any_hit"], v["n"]),
            "hit_2plus_rate": safe_rate(v["hit_2plus"], v["n"]),
            "mean_hits": safe_rate(v["hits_sum"], v["n"]),
        }

    return {
        "n": n,
        "any_hit_rate": safe_rate(any_hit, n),
        "hit_2plus_rate": safe_rate(hit_2plus, n),
        "mean_hits": safe_rate(hits_sum, n),
        "by_regime": by_regime_out,
    }

def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if not os.path.exists(LIVE_LOG_PATH):
        raise FileNotFoundError(f"Missing {LIVE_LOG_PATH}. Run live_performance_log.py first.")

    rows = read_log(LIVE_LOG_PATH)
    if not rows:
        raise RuntimeError("Live log is empty.")

    latest = rows[-1]
    overall = summarize(rows)

    rolling = {}
    for w in ROLLING_WINDOWS:
        if len(rows) >= w:
            rolling[str(w)] = summarize(rows[-w:])
        else:
            rolling[str(w)] = None

    out = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "source": {
            "live_log_path": LIVE_LOG_PATH,
            "rows": len(rows),
            "latest_draw": {
                "draw_date": latest["draw_date"],
                "draw_time": latest["draw_time"],
                "actual_pick3": latest["actual_pick3"],
                "actual_fireball": latest["actual_fireball"],
                "top4": latest["top4"],
                "hits_in_top4": latest["hits_in_top4"],
                "regime": latest["regime"],
                "struct_weight": latest["struct_weight"],
            }
        },
        "overall": overall,
        "rolling": rolling,
        "notes": "Rates computed from live_performance_log.csv (one row per draw).",
    }

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)

    print(f"[ok] wrote {OUT_JSON}")

if __name__ == "__main__":
    main()
