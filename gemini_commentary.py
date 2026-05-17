"""Generate a one-line Japanese market comment from chart stats using Gemini.

Failure is graceful: returns None on any error (missing key, network, API issue)
so the caller can fall back to posting without commentary.

Model: gemini-2.5-flash-lite (matches polymarket-smart-money convention).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash-lite"

SYSTEM_PROMPT = """あなたはBTC無期限先物の市況コメンテーターです。
入力データ（直近24hのVolume Profile/清算/OI統計）を見て、日本語で30〜60文字の
1行コメントを返してください。

スタイル:
- 客観・簡潔・断定。予想ではなく現状の解説。
- BTC値や比率は本文に織り込んでよい。
- 文末は体言止めまたは「。」で終える。「？」「！」「絵文字」「ハッシュタグ」「引用符」は禁止。
- 「〜と思います」「〜でしょう」のような曖昧表現は禁止。
- ポジション推奨は禁止 (「買い場」「ショート狙い」等NG)。

良い例:
- 直近24hは$78k付近に出来高集中、ロング清算がやや優勢で下方向の薄さが目立つ。
- 価格はレンジ中央、ショート踏み上げ940 BTCに対しロング投げ857 BTCでほぼ拮抗。
- OIは24hで+0.6%と微増、出来高の左右バランスは均衡し方向感に乏しい展開。
"""


def generate_commentary(meta: dict, *, api_key: str | None = None) -> str | None:
    """Return a one-line JP commentary, or None on any failure."""
    api_key = api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.info("gemini: no GEMINI_API_KEY, skipping commentary")
        return None

    try:
        from google import genai  # type: ignore
        from google.genai import types as genai_types  # type: ignore
    except ImportError:
        log.warning("gemini: google-genai SDK not installed, skipping commentary")
        return None

    user_prompt = _format_user_prompt(meta)

    try:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=MODEL,
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.4,
                max_output_tokens=200,
            ),
        )
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            log.warning("gemini: empty response")
            return None
        # Single line; strip any incidental newlines / surrounding quotes
        text = text.replace("\n", " ").strip().strip('"').strip("「」")
        log.info("gemini commentary: %s", text)
        return text
    except Exception as e:  # noqa: BLE001
        log.warning("gemini commentary failed: %s", e)
        return None


def _format_user_prompt(meta: dict) -> str:
    mark = meta.get("mark", 0)
    lookback = meta.get("lookback_hours", 24)
    vb = meta.get("total_vol_buy_btc", 0)
    vs = meta.get("total_vol_sell_btc", 0)
    ls = meta.get("total_liq_short_btc", 0)
    ll = meta.get("total_liq_long_btc", 0)
    oi = meta.get("oi_total_btc", 0)
    oi_chg = meta.get("oi_change_pct_24h", 0)

    buy_pct = (vb / (vb + vs) * 100) if (vb + vs) > 0 else 50
    liq_dominant = "ロング" if ll > ls else "ショート"

    return (
        f"BTC perp 直近{int(lookback)}hの市況統計です。コメントを生成してください。\n\n"
        f"- マーク価格: ${mark:,.0f}\n"
        f"- Volume Profile: 買い約定 {vb:,.0f} BTC / 売り約定 {vs:,.0f} BTC (買いシェア {buy_pct:.1f}%)\n"
        f"- 清算: ロング {ll:,.0f} BTC / ショート {ls:,.0f} BTC ({liq_dominant}清算優勢)\n"
        f"- Open Interest: 合計 {oi:,.0f} BTC ({'+' if oi_chg >= 0 else ''}{oi_chg:.2f}% / {int(lookback)}h)\n"
    )


if __name__ == "__main__":
    import json
    import sys
    logging.basicConfig(level=logging.INFO)
    # Allow ad-hoc testing: pipe meta json or use sample
    if not sys.stdin.isatty():
        m = json.load(sys.stdin)
    else:
        m = {
            "mark": 77963, "lookback_hours": 24,
            "total_vol_buy_btc": 59239, "total_vol_sell_btc": 59559,
            "total_liq_short_btc": 940, "total_liq_long_btc": 857,
            "oi_total_btc": 200770, "oi_change_pct_24h": 0.6,
        }
    from dotenv import load_dotenv
    load_dotenv()
    print(generate_commentary(m))
