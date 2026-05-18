"""DeepL fallback provider.

DeepL is translation-only, not a generative LLM, so this provider produces a
deterministic English summary from the chart `meta` dict and then asks DeepL to
translate it into Japanese. Result will be plainer than the LLM providers but
keeps the post bilingual-friendly when every LLM is unavailable.

Auth key auto-routes to the free endpoint when it ends with `:fx`, matching
DeepL's convention. Override base URL with DEEPL_API_URL if you need to.
"""

from __future__ import annotations

import logging
import os

import requests

from commentary import clean_output, format_english_summary

log = logging.getLogger(__name__)

FREE_URL = "https://api-free.deepl.com/v2/translate"
PRO_URL = "https://api.deepl.com/v2/translate"


def _resolve_url(api_key: str) -> str:
    explicit = os.getenv("DEEPL_API_URL")
    if explicit:
        return explicit
    return FREE_URL if api_key.endswith(":fx") else PRO_URL


def generate_commentary(meta: dict, *, api_key: str | None = None) -> str | None:
    api_key = api_key or os.getenv("DEEPL_API_KEY") or os.getenv("DEEPL_AUTH_KEY")
    if not api_key:
        log.info("deepl: no DEEPL_API_KEY, skipping")
        return None

    source = format_english_summary(meta)
    url = _resolve_url(api_key)
    try:
        r = requests.post(
            url,
            data={
                "auth_key": api_key,
                "text": source,
                "source_lang": "EN",
                "target_lang": "JA",
            },
            timeout=15,
        )
        r.raise_for_status()
        translations = r.json().get("translations") or []
        if not translations:
            log.warning("deepl: empty translations response")
            return None
        return clean_output(translations[0].get("text"))
    except requests.RequestException as e:
        log.warning("deepl: request failed: %s", e)
        return None


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
