import json
import os
import textwrap
import requests

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

ODDS_PATH = os.getenv("ODDS_PATH", "data/odds_board_latest.json")
OUT_PATH = os.getenv("OUT_PATH", "data/next_draw_report.md")

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
    }

    r = requests.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()

    # Responses API returns "output" items; simplest extraction:
    # We scan for first text block.
    for item in data.get("output", []):
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                return c.get("text", "").strip()

    # Fallback: stringify if structure changes
    return json.dumps(data, indent=2)

def main():
    odds = load_odds()

    # Pull key bits (keeps prompt small + stable)
    last_draw = odds.get("last_observed_draw", {})
    next_odds = odds.get("next_draw_odds", {})
    top4 = next_odds.get("top4", [])
    digits = next_odds.get("digits", [])

    # Make a compact table-like text for the model
    lines = []
    for d in digits:
        lines.append(
            f"{d['digit']}: prob={d['prob']}, drought={d['drought_draws']}, "
            f"bucket={d['drought_bucket']}, r10={d['rate10']}, r25={d['rate25']}, r50={d['rate50']}"
        )

    prompt = textwrap.dedent(f"""
    You are my Illinois Pick 3 assistant. I have two draws per day (MIDDAY then EVENING).
    Use the combined-model odds board below to produce a concise “Play Card” for the NEXT draw in sequence.

    Requirements:
    - Start with: Last draw + Next draw context (acknowledge two-draw-per-day cadence).
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
    - pick3: {last_draw.get('pick3')}

    Top 4 digits (by model):
    {top4}

    Digit board (0–9):
    {chr(10).join(lines)}
    """).strip()

    report = call_openai(prompt)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(report + "\n")

    print(f"[play_card] wrote {OUT_PATH}")

if __name__ == "__main__":
    main()
