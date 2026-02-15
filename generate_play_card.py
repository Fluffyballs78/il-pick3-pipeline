import json
import os
import textwrap
import time
import requests
from typing import Any, Dict, Optional

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

ODDS_PATH = os.getenv("ODDS_PATH", "data/odds_board_latest.json")
OUT_PATH = os.getenv("OUT_PATH", "data/next_draw_report.md")
LIVE_SUMMARY_PATH = os.getenv("LIVE_SUMMARY_PATH", "data/live_summary.json")


def load_odds() -> Dict[str, Any]:
    with open(ODDS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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


def load_live_summary() -> Optional[Dict[str, Any]]:
    if not os.path.exists(LIVE_SUMMARY_PATH):
        print("[play_card] live_summary.json not found — skipping live section.")
        return None

    try:
        with open(LIVE_SUMMARY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[play_card] failed to load live_summary.json: {e}")
        return None


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


def render_live_summary_md(summary: Dict[str, Any]) -> str:
    """Render a compact, readable markdown summary.
    Tries to be resilient if fields evolve.
    """
    window = summary.get("window", {}) if isinstance(summary, dict) else {}
    overall = summary.get("overall", {}) if isinstance(summary, dict) else {}
    models = summary.get("models", {}) if isinstance(summary, dict) else {}

    draws = window.get("draws", "—")
    start = window.get("start_date", "—")
    end = window.get("end_date", "—")
    updated = summary.get("last_updated_utc") or summary.get("updated_utc") or "—"

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
    lines.append(f"- Window: **{draws}** draws (`{start}` → `{end}`)\n")

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

    has_expected = any(
        k in overall for k in [
            "hit_rate_any_top4", "avg_hits_in_top4",
            "zero_hit_rate", "one_hit_rate", "two_hit_rate", "three_hit_rate", "four_hit_rate"
        ]
    )
    if not has_expected:
        lines.append("\n<details><summary>Raw live_summary.json (for debugging)</summary>\n\n```json")
        lines.append(json.dumps(summary, indent=2))
        lines.append("```\n</details>\n")

    return "\n".join(lines)


def render_live_prompt_block(summary: Dict[str, Any]) -> str:
    """Very small prompt block to keep tokens low."""
    window = summary.get("window", {}) if isinstance(summary, dict) else {}
    overall = summary.get("overall", {}) if isinstance(summary, dict) else {}
    updated = summary.get("last_updated_utc") or summary.get("updated_utc") or "—"

    draws = window.get("draws", "—")
    start = window.get("start_date", "—")
    end = window.get("end_date", "—")

    any_hit = overall.get("hit_rate_any_top4")
    avg_hits = overall.get("avg_hits_in_top4")

    guidance = ""
    try:
        h = float(any_hit)
        if h >= 0.75:
            guidance = "Live performance is strong: keep chaos list tighter (1 digit)."
        elif h >= 0.60:
            guidance = "Live performance is decent: normal chaos (1–2 digits)."
        else:
            guidance = "Live performance is weak: widen chaos (2 digits) and emphasize uncertainty."
    except Exception:
        guidance = "Use live performance as context if available; keep uncertainty language appropriate."

    return textwrap.dedent(f"""
    Live performance context (rolling):
    - last updated: {updated}
    - window: {draws} draws ({start} → {end})
    - Top-4 any-hit rate (≥1): {_fmt_pct(any_hit)}
    - Avg Top-4 hits per draw: {_fmt_num(avg_hits, 2) if avg_hits is not None else '—'}
    - guidance: {guidance}
    """).strip()


def main():
    odds = load_odds()
    live_summary = load_live_summary()

    last_draw = odds.get("last_observed_draw", {})
    next_expected = odds.get("next_expected_draw", {})
    next_odds = odds.get("next_draw_odds", {})
    top4 = next_odds.get("top4", [])
    digits = next_odds.get("digits", [])

    lines = []
    for d in digits:
        lines.append(
            f"{d['digit']}: prob={d['prob']}, drought={d['drought_draws']}, "
            f"bucket={d['drought_bucket']}, r10={d['rate10']}, r25={d['rate25']}, r50={d['rate50']}"
        )

    live_prompt = ""
    if live_summary:
        live_prompt = "\n\n" + render_live_prompt_block(live_summary) + "\n"

    prompt = textwrap.dedent(f"""
    You are my Illinois Pick 3 assistant. I play the draw as FOUR digits:
    the 3 Pick-3 digits PLUS the Fireball digit. Treat it like a "pick-4-like" 4-digit event for digit coverage.

    There are two draws per day: MIDDAY then EVENING.
    Use the combined-model odds board below to produce a concise “Play Card” for the NEXT draw slot in sequence.

    Requirements:
    - Start with: Last draw (4-digit-like) + Next expected draw slot (date/time).
    - Show the Top 4 digits and a 1–2 sentence rationale (momentum + drought).
    - Provide:
      * Lean play: Top 4 digits
      * Chaos add-on: 1–2 digits NOT in top4 (high drought or undervalued momentum) with rationale
      * Fade list: 1–2 digits to avoid (lowest combined signal) with rationale
    - Keep it mobile-friendly. Use bullets. No long essay.
    - Do NOT claim certainty or guaranteed wins. This is probability-based.
    - If live performance context is provided, use it to calibrate tone and the size of the chaos add-on list.

    Last observed draw:
    - date: {last_draw.get('draw_date')}
    - time: {last_draw.get('draw_time')}
    - pick4-like: {last_draw.get('pick4_like')}   (Pick3 + Fireball)
    - pick3: {last_draw.get('pick3')}
    - fireball: {last_draw.get('fireball')}

    Next expected draw slot:
    - date: {next_expected.get('draw_date')}
    - time: {next_expected.get('draw_time')}

    Top 4 digits (by model):
    {top4}

    Digit board (0–9):
    {chr(10).join(lines)}{live_prompt}
    """).strip()

    report = call_openai(prompt)

    final_output = report

    if live_summary:
        final_output += render_live_summary_md(live_summary)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(final_output + "\n")

    print(f"[play_card] wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
