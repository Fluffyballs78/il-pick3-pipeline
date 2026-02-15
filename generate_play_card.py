import json
import os
import textwrap
import time
import requests

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

ODDS_PATH = os.getenv("ODDS_PATH", "data/odds_board_latest.json")
OUT_PATH = os.getenv("OUT_PATH", "data/next_draw_report.md")
LIVE_SUMMARY_PATH = os.getenv("LIVE_SUMMARY_PATH", "data/live_summary.json")


def load_odds():
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


def load_live_summary():
    if not os.path.exists(LIVE_SUMMARY_PATH):
        print("[play_card] live_summary.json not found — skipping live section.")
        return None

    try:
        with open(LIVE_SUMMARY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[play_card] failed to load live_summary.json: {e}")
        return None


def main():
    odds = load_odds()

    last_draw = odds.get("last_observed_draw", {})
    next_expected = odds.get("next_expected_draw", {})
    next_odds = odds.get("next_draw_odds", {})
    top4 = next_odds.get("top4", [])
    digits = next_odds.get("digits", [])

    lines = []
    for d in digits:
        lines.append(
            f"{d['digit']}: prob={d['prob']}, drought={d['drought_draws']}, "
            f"bucket={d['drought_bucket']}, r10={d['rate10']}, "
            f"r25={d['rate25']}, r50={d['rate50']}"
        )

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
    {chr(10).join(lines)}
    """).strip()

    report = call_openai(prompt)

    final_output = report

    # Append live summary JSON if it exists
    live_summary = load_live_summary()
    if live_summary:
        final_output += "\n\n---\n\n## Live Performance (Rolling)\n\n"
        final_output += "```json\n"
        final_output += json.dumps(live_summary, indent=2)
        final_output += "\n```\n"

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(final_output + "\n")

    print(f"[play_card] wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
