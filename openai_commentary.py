"""OpenAI provider: one-line JP market commentary.

Returns None on any failure (missing key, SDK not installed, API issue) so the
orchestrator can fall through to the next provider.

Model: gpt-4o-mini (override via OPENAI_MODEL).
"""

from __future__ import annotations

import logging
import os

from commentary import SYSTEM_PROMPT, clean_output, format_user_prompt

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"


def generate_commentary(meta: dict, *, api_key: str | None = None) -> str | None:
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        log.info("openai: no OPENAI_API_KEY, skipping")
        return None

    try:
        from openai import OpenAI  # type: ignore
    except ImportError:
        log.warning("openai: openai SDK not installed, skipping")
        return None

    model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
    base_url = os.getenv("OPENAI_BASE_URL")  # optional, for Azure/proxy
    client = OpenAI(api_key=api_key, base_url=base_url) if base_url else OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": format_user_prompt(meta)},
        ],
        temperature=0.4,
        max_tokens=200,
    )
    text = resp.choices[0].message.content if resp.choices else None
    return clean_output(text)


if __name__ == "__main__":
    import json
    import sys

    from dotenv import load_dotenv

    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    sample = json.load(sys.stdin) if not sys.stdin.isatty() else {
        "mark": 77963, "lookback_hours": 24,
        "total_vol_buy_btc": 59239, "total_vol_sell_btc": 59559,
        "total_liq_short_btc": 940, "total_liq_long_btc": 857,
        "oi_total_btc": 200770, "oi_change_pct_24h": 0.6,
    }
    print(generate_commentary(sample))
