import json
import os
import textwrap
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.getenv("OPENAI_API_KEY_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))

ODDS_PATH = os.getenv("ODDS_PATH", "data/odds_board_latest.json")
OUT_PATH = os.getenv("OUT_PATH", "data/next_draw_report.md")
LIVE_SUMMARY_PATH = os.getenv("LIVE_SUMMARY_PATH", "data/live_summary.json")
LIVE_LOG_PATH = os.getenv("LIVE_LOG_PATH", "data/live_performance_log.csv")


# ----------------------------- IO helpers -----------------------------

def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_read_live_summary() -> Optional[Dict[str, Any]]:
    if not os.path.exists(LIVE_SUMMARY_PATH):
        print("[play_card] live_summary.json not found — skipping live context/section.")
        return None
    try:
        return load_json(LIVE_SUMMARY_PATH)
    except Exception as e:
        print(f"[play_card] failed to load live_summary.json: {e}")
        return None


def ensure_dir_for_file(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


# -------------------------- Formatting helpers --------------------------

def _fmt_pct(x: Any) -> str:
    try:
        return f"{float(x) * 100:.1f}%"
    except Exception:
        return "—"


def _fmt_num(x: Any, digits: int = 2) -> str:
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return "—"


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------- Confidence (simple rules) ----------------------

def compute_confidence(summary: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """Stable rules based on rolling Top-4 any-hit rate.
    High: >= 75%
    Medium: 60%–74.9%
    Low: < 60%
    Unknown: missing
    """
    if not summary or not isinstance(summary, dict):
        return ("Unknown", "No live summary available.")

    overall = summary.get("overall", {})
    any_hit = overall.get("hit_rate_any_top4", None)

    try:
        h = float(any_hit)
    except Exception:
        return ("Unknown", "Live hit-rate missing.")

    if h >= 0.75:
        return ("High", f"Rolling Top-4 any-hit rate is {_fmt_pct(h)}.")
    if h >= 0.60:
        return ("Medium", f"Rolling Top-4 any-hit rate is {_fmt_pct(h)}.")
    return ("Low", f"Rolling Top-4 any-hit rate is {_fmt_pct(h)}.")


# ----------------------- Live summary renderers ------------------------

def render_live_prompt_block(summary: Dict[str, Any]) -> str:
    window = summary.get("window", {}) if isinstance(summary, dict) else {}
    overall = summary.get("overall", {}) if isinstance(summary, dict) else {}
    updated = summary.get("last_updated_utc") or summary.get("updated_utc") or "—"

    draws = window.get("draws", "—")
    start = window.get("start_date", "—")
    end = window.get("end_date", "—")

    any_hit = overall.get("hit_rate_any_top4")
    avg_hits = overall.get("avg_hits_in_top4")

    conf_label, conf_reason = compute_confidence(summary)

    guidance = ""
    try:
        h = float(any_hit)
        if h >= 0.75:
            guidance = "Keep chaos list tighter (1 digit)."
        elif h >= 0.60:
            guidance = "Normal chaos (1–2 digits)."
        else:
            guidance = "Widen chaos (2 digits) and emphasize uncertainty."
    except Exception:
        guidance = "Use live performance as context if available; keep uncertainty language appropriate."

    return textwrap.dedent(f"""
    Live performance context (rolling):
    - last updated: {updated}
    - window: {draws} draws ({start} → {end})
    - Top-4 any-hit rate (≥1): {_fmt_pct(any_hit)}
    - Avg Top-4 hits per draw: {_fmt_num(avg_hits, 2) if avg_hits is not None else '—'}
    - Model confidence (simple): {conf_label} ({conf_reason})
    - guidance: {guidance}
    """).strip()


def render_live_summary_md(summary: Dict[str, Any]) -> str:
    window = summary.get("window", {}) if isinstance(summary, dict) else {}
    overall = summary.get("overall", {}) if isinstance(summary, dict) else {}
    models = summary.get("models", {}) if isinstance(summary, dict) else {}

    draws = window.get("draws", "—")
    start = window.get("start_date", "—")
    end = window.get("end_date", "—")
    updated = summary.get("last_updated_utc") or summary.get("updated_utc") or "—"

    conf_label, conf_reason = compute_confidence(summary)

    any_hit = overall.get("hit_rate_any_top4")
    avg_hits = overall.get("avg_hits_in_top4")
    zero = overall.get("zero_hit_rate")
    one = overall.get("one_hit_rate")
    two = overall.get("two_hit_rate")
    three = overall.get("three_hit_rate")
    four = overall.get("four_hit_rate")

    lines = []
    lines.append("\n\n---\n\n## 📊 Live Performance (Rolling)\n")
    lines.append(f"- Last updated: `{updated}`")
    lines.append(f"- Window: **{draws}** draws (`{start}` → `{end}`)")
    lines.append(f"- Confidence (simple): **{conf_label}** — {conf_reason}\n")

    lines.append("### Top-4 Coverage\n")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Any hit (≥1 of Top-4) | **{_fmt_pct(any_hit)}** |")
    lines.append(f"| Avg hits per draw (0–4) | **{_fmt_num(avg_hits, 2) if avg_hits is not None else '—'}** |")
    lines.append(f"| 0 hits | {_fmt_pct(zero)} |")
    lines.append(f"| 1 hit | {_fmt_pct(one)} |")
    lines.append(f"| 2 hits | {_fmt_pct(two)} |")
    lines.append(f"| 3 hits | {_fmt_pct(three)} |")
    lines.append(f"| 4 hits | {_fmt_pct(four)} |")

    if isinstance(models, dict) and models:
        lines.append("\n### Model Breakdown\n")
        lines.append("| Model | Any hit | Avg hits | Notes |")
        lines.append("|---|---:|---:|---|")
        for name, m in models.items():
            if not isinstance(m, dict):
                lines.append(f"| `{name}` | — | — | — |")
                continue
            mh = _fmt_pct(m.get("hit_rate_any_top4"))
            ma = m.get("avg_hits_in_top4")
            ma_fmt = _fmt_num(ma, 2) if ma is not None else "—"
            notes = m.get("notes", "")
            if isinstance(notes, str) and len(notes) > 90:
                notes = notes[:87] + "..."
            lines.append(f"| `{name}` | {mh} | {ma_fmt} | {notes} |")

    return "\n".join(lines)


# ----------------------------- OpenAI call ------------------------------

def call_openai(prompt: str) -> str:
    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MODEL,
        "input": prompt,
        "max_output_tokens": 450,
    }

    for attempt in range(3):
        r = requests.post(url, headers=headers, json=payload, timeout=60)

        if r.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"[play_card] Rate limited. Waiting {wait}s...")
            time.sleep(wait)
            continue

        if r.status_code >= 400:
            print(f"[play_card] OpenAI error: {r.status_code}")
            print(r.text)
            return "⚠️ OpenAI API error. See workflow logs."

        data = r.json()
        for item in data.get("output", []):
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    return c.get("text", "").strip()

        return "⚠️ Unexpected OpenAI response format."

    return "⚠️ Rate limit exceeded after retries."


# ----------------------------- Logging ---------------------------------

def append_live_log_row(
    next_date: str,
    next_time: str,
    top4: Any,
    confidence_label: str,
    confidence_reason: str,
) -> None:
    """Append a lightweight row to LIVE_LOG_PATH.
    This is a *prediction log* row (not outcome). Outcome fields remain blank for later processes.
    """
    ensure_dir_for_file(LIVE_LOG_PATH)

    header = [
        "logged_at_utc",
        "next_draw_date",
        "next_draw_time",
        "top4",
        "confidence_label",
        "confidence_reason",
        "model",
        "outcome_any_hit",      # blank now; fill later
        "outcome_hit_count",    # blank now; fill later (0-4)
        "outcome_digits_hit",   # blank now; fill later
    ]

    row = [
        now_utc_iso(),
        next_date or "",
        next_time or "",
        json.dumps(top4, ensure_ascii=False),
        confidence_label or "",
        confidence_reason or "",
        MODEL,
        "",
        "",
        "",
    ]

    # Write header if file doesn't exist or is empty
    write_header = True
    if os.path.exists(LIVE_LOG_PATH):
        try:
            if os.path.getsize(LIVE_LOG_PATH) > 0:
                write_header = False
        except Exception:
            write_header = True

    try:
        with open(LIVE_LOG_PATH, "a", encoding="utf-8") as f:
            if write_header:
                f.write(",".join(header) + "\n")

            # CSV-safe minimal quoting
            def q(v: str) -> str:
                v = v or ""
                if any(ch in v for ch in [",", "\n", "\""]):
                    v = v.replace("\"", "\"\"")
                    return f'"{v}"'
                return v

            f.write(",".join(q(str(v)) for v in row) + "\n")

        print(f"[play_card] logged prediction -> {LIVE_LOG_PATH}")
    except Exception as e:
        print(f"[play_card] failed to write live log: {e}")


# -------------------------- Output guardrail ----------------------------

REQUIRED_SECTIONS = [
    "### Rationale",
    "### Lean play",
    "### Chaos add-on",
    "### Fade list",
]


def is_structured(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    return all(s in t for s in REQUIRED_SECTIONS)


def build_fallback_play_card(
    last_draw: Dict[str, Any],
    next_expected: Dict[str, Any],
    top4: Any,
    chaos_candidates: Any,
    fade_candidates: Any,
    confidence_label: str,
    confidence_reason: str,
    raw_ai: str,
) -> str:
    """If the model output is malformed, we still produce a usable report."""
    last_pick4 = last_draw.get("pick4_like") or "—"
    last_dt = f"{last_draw.get('draw_date', '—')} {last_draw.get('draw_time', '—')}".strip()
    next_dt = f"{next_expected.get('draw_date', '—')} {next_expected.get('draw_time', '—')}".strip()

    lines = []
    lines.append("# Illinois Pick 3 Play Card (NEXT DRAW)")
    lines.append("")
    lines.append(f"- Last draw: **{last_pick4}** ({last_dt})")
    lines.append(f"- Next expected: **{next_dt}**")
    lines.append(f"- Model confidence (simple): **{confidence_label}** — {confidence_reason}")
    lines.append("")
    lines.append("## Official digits (deterministic)")
    lines.append(f"- Top 4: **{top4}**")
    lines.append("")
    lines.append("### Rationale")
    lines.append("- (Fallback) AI output did not match the required format. Using deterministic Top-4 list.")
    lines.append("")
    lines.append("### Lean play")
    lines.append(f"- **{top4}**")
    lines.append("")
    lines.append("### Chaos add-on")
    lines.append(f"- {chaos_candidates}")
    lines.append("")
    lines.append("### Fade list")
    lines.append(f"- {fade_candidates}")
    lines.append("")
    lines.append("<details><summary>Raw AI output (debug)</summary>")
    lines.append("")
    lines.append(raw_ai or "")
    lines.append("")
    lines.append("</details>")
    return "\n".join(lines)


# ------------------------------- Main -----------------------------------

def main():
    odds = load_json(ODDS_PATH)

    last_draw = odds.get("last_observed_draw", {})
    next_expected = odds.get("next_expected_draw", {})
    next_odds = odds.get("next_draw_odds", {})
    top4 = next_odds.get("top4", [])
    digits = next_odds.get("digits", [])

    # Deterministic candidates to help fallback + prompt grounding
    # Chaos: choose up to 2 digits not in top4 with highest drought_draws
    # Fade: choose up to 2 digits not in top4 with lowest prob
    top4_set = set(str(x) for x in top4)

    not_in_top4 = [d for d in digits if str(d.get("digit")) not in top4_set]

    chaos_sorted = sorted(
        not_in_top4,
        key=lambda d: (float(d.get("drought_draws", 0)) if str(d.get("drought_draws", "")).replace(".", "", 1).isdigit() else 0.0),
        reverse=True,
    )
    chaos_candidates = [str(d.get("digit")) for d in chaos_sorted[:2]] or []
    chaos_candidates_str = ", ".join(chaos_candidates) if chaos_candidates else "—"

    fade_sorted = sorted(
        not_in_top4,
        key=lambda d: (float(d.get("prob", 0)) if str(d.get("prob", "")).replace(".", "", 1).isdigit() else 0.0),
    )
    fade_candidates = [str(d.get("digit")) for d in fade_sorted[:2]] or []
    fade_candidates_str = ", ".join(fade_candidates) if fade_candidates else "—"

    live_summary = safe_read_live_summary()
    confidence_label, confidence_reason = compute_confidence(live_summary)

    # Build digit-board lines for prompt
    digit_lines = []
    for d in digits:
        digit_lines.append(
            f"{d.get('digit')}: prob={d.get('prob')}, drought={d.get('drought_draws')}, "
            f"bucket={d.get('drought_bucket')}, r10={d.get('rate10')}, r25={d.get('rate25')}, r50={d.get('rate50')}"
        )

    live_prompt = ""
    if live_summary:
        live_prompt = "\n\n" + render_live_prompt_block(live_summary) + "\n"

    last_pick4 = last_draw.get("pick4_like")
    last_date = last_draw.get("draw_date")
    last_time = last_draw.get("draw_time")

    next_date = next_expected.get("draw_date")
    next_time = next_expected.get("draw_time")

    # Deterministic header (always printed) + structured sections (AI fills)
    prompt = textwrap.dedent(f"""
    You are my Illinois Pick 3 assistant. I play the draw as FOUR digits:
    the 3 Pick-3 digits PLUS the Fireball digit. Treat it like a "pick-4-like" 4-digit event for digit coverage.

    There are two draws per day: MIDDAY then EVENING.
    Use the combined-model odds board below to produce a concise “Play Card” for the NEXT draw slot in sequence.

    HARD FORMAT REQUIREMENT:
    - Output MUST include these headings exactly (markdown):
      ### Rationale
      ### Lean play
      ### Chaos add-on
      ### Fade list

    Additional requirements:
    - Start with 3 bullets (in this exact order):
      - Last draw: <pick4-like> (<date time>)
      - Next expected: <date time>
      - Model confidence (simple): {confidence_label} — {confidence_reason}
    - In “Lean play” show ONLY the Top 4 digits: {top4}
    - Chaos add-on: choose 1–2 digits NOT in Top 4, and explain why (drought or undervalued momentum).
    - Fade list: choose 1–2 digits to avoid, and explain why (lowest combined signal).
    - Keep it mobile-friendly. Bullets. No long essay.
    - Do NOT claim certainty or guaranteed wins. This is probability-based.
    - If live performance context is provided, calibrate tone and chaos sizing accordingly.

    Last observed draw:
    - date: {last_date}
    - time: {last_time}
    - pick4-like: {last_pick4}   (Pick3 + Fireball)
    - pick3: {last_draw.get('pick3')}
    - fireball: {last_draw.get('fireball')}

    Next expected draw slot:
    - date: {next_date}
    - time: {next_time}

    Top 4 digits (by model) — MUST use for Lean play:
    {top4}

    Digit board (0–9):
    {chr(10).join(digit_lines)}{live_prompt}
    """).strip()

    ai_text = call_openai(prompt)

    # Build deterministic report wrapper
    report_lines = []
    report_lines.append("# Illinois Pick 3 Play Card (NEXT DRAW)")
    report_lines.append("")
    report_lines.append(f"- Last draw: **{last_pick4}** ({(str(last_date) + ' ' + str(last_time)).strip()})")
    report_lines.append(f"- Next expected: **{(str(next_date) + ' ' + str(next_time)).strip()}**")
    report_lines.append(f"- Model confidence (simple): **{confidence_label}** — {confidence_reason}")
    report_lines.append("")
    report_lines.append("## Official digits (deterministic)")
    report_lines.append(f"- Top 4: **{top4}**")
    report_lines.append("")
    report_header = "\n".join(report_lines)

    # If AI formatted correctly, append it. Otherwise fallback.
    if is_structured(ai_text):
        final_output = report_header + "\n\n" + ai_text
    else:
        final_output = build_fallback_play_card(
            last_draw=last_draw,
            next_expected=next_expected,
            top4=top4,
            chaos_candidates=chaos_candidates_str,
            fade_candidates=fade_candidates_str,
            confidence_label=confidence_label,
            confidence_reason=confidence_reason,
            raw_ai=ai_text,
        )

    # Append pretty live section at bottom
    if live_summary:
        final_output += render_live_summary_md(live_summary)

    ensure_dir_for_file(OUT_PATH)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(final_output + "\n")

    print(f"[play_card] wrote {OUT_PATH}")

    # Log prediction row (best-effort)
    append_live_log_row(
        next_date=str(next_date or ""),
        next_time=str(next_time or ""),
        top4=top4,
        confidence_label=confidence_label,
        confidence_reason=confidence_reason,
    )


if __name__ == "__main__":
    main()
