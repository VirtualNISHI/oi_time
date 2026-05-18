"""Grok (xAI) provider: one-line JP market commentary.

Uses the OpenAI-compatible xAI endpoint (https://api.x.ai/v1). Requires the
`openai` Python SDK and XAI_API_KEY.

Model: grok-3-mini (override via XAI_MODEL).
"""

from __future__ import annotations

import logging
import os

from commentary import SYSTEM_PROMPT, clean_output, format_user_prompt

log = logging.getLogger(__name__)

DEFAULT_MODEL = "grok-3-mini"
BASE_URL = "https://api.x.ai/v1"


def generate_commentary(meta: dict, *, api_key: str | None = None) -> str | None:
    api_key = api_key or os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")
    if not api_key:
        log.info("grok: no XAI_API_KEY, skipping")
        return None

    try:
        from openai import OpenAI  # type: ignore
    except ImportError:
        log.warning("grok: openai SDK not installed, skipping")
        return None

    model = os.getenv("XAI_MODEL", DEFAULT_MODEL)
    client = OpenAI(api_key=api_key, base_url=BASE_URL)
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
