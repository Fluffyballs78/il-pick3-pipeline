import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

LIVE_LOG_PATH = os.getenv("LIVE_LOG_PATH", "data/live_performance_log.csv")
ODDS_PATH = os.getenv("ODDS_PATH", "data/odds_board_latest.json")
OUT_PATH = os.getenv("PREV_RECAP_PATH", "data/prev_draw_recap.json")


def _read_last_nonempty_lines(path: Path, max_lines: int = 2000) -> list[str]:
    if not path.exists():
        return []
    data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = data[-max_lines:] if len(data) > max_lines else data
    return [ln for ln in tail if ln.strip()]


def _parse_csv_line(line: str) -> list[str]:
    out, cur, in_q = [], "", False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_q:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    cur += '"'
                    i += 1
                else:
                    in_q = False
            else:
                cur += ch
        else:
            if ch == '"':
                in_q = True
            elif ch == ",":
                out.append(cur)
                cur = ""
            else:
                cur += ch
        i += 1
    out.append(cur)
    return out


def _load_odds(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _find_latest_prediction_with_outcome(lines: list[str]) -> Optional[Dict[str, str]]:
    if not lines or len(lines) < 2:
        return None

    header = _parse_csv_line(lines[0])
    idx = {name: i for i, name in enumerate(header)}

    def get(cols: list[str], col: str) -> str:
        j = idx.get(col, -1)
        return cols[j] if 0 <= j < len(cols) else ""

    for ln in reversed(lines[1:]):
        cols = _parse_csv_line(ln)
        hit_count = get(cols, "outcome_hit_count").strip()
        any_hit = get(cols, "outcome_any_hit").strip()
        if hit_count != "" or any_hit != "":
            return {
                "next_draw_date": get(cols, "next_draw_date").strip(),
                "next_draw_time": get(cols, "next_draw_time").strip(),
                "top4": get(cols, "top4").strip(),
                "outcome_any_hit": any_hit,
                "outcome_hit_count": hit_count,
                "outcome_digits_hit": get(cols, "outcome_digits_hit").strip(),
                "logged_at_utc": get(cols, "logged_at_utc").strip(),
                "model": get(cols, "model").strip(),
            }
    return None


def main():
    live_path = Path(LIVE_LOG_PATH)
    odds_path = Path(ODDS_PATH)
    out_path = Path(OUT_PATH)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    odds = _load_odds(odds_path)
    last_draw = odds.get("last_observed_draw", {}) if isinstance(odds, dict) else {}

    tail = _read_last_nonempty_lines(live_path)
    pred = _find_latest_prediction_with_outcome(tail)

    recap: Dict[str, Any] = {
        "generated_at_utc": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_observed_draw": last_draw,
        "previous_draw_prediction": pred or {},
    }

    out_path.write_text(json.dumps(recap, indent=2), encoding="utf-8")
    print(f"[prev_recap] wrote {out_path}")


if __name__ == "__main__":
    main()
