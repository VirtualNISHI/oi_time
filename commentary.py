"""Japanese market-commentary orchestrator with provider fallback.

Provider order (default): gemini → grok → openai → deepl.
Override with COMMENTARY_PROVIDERS env var (comma-separated, in priority order).

Each provider module must expose `generate_commentary(meta) -> str | None`.
A None / empty return is treated as "skip and try the next provider".

Shared helpers (system prompt, user prompt formatter, post-processing) live here
so each provider module is just an SDK adapter.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_ORDER: tuple[str, ...] = ("gemini", "grok", "openai", "deepl")

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


def format_user_prompt(meta: dict) -> str:
    """Format meta dict into the standard JP-LLM user prompt."""
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


def format_english_summary(meta: dict) -> str:
    """Deterministic English summary used as the DeepL translate source."""
    mark = meta.get("mark", 0)
    lookback = int(meta.get("lookback_hours", 24))
    vb = meta.get("total_vol_buy_btc", 0)
    vs = meta.get("total_vol_sell_btc", 0)
    ls = meta.get("total_liq_short_btc", 0)
    ll = meta.get("total_liq_long_btc", 0)
    oi_chg = meta.get("oi_change_pct_24h", 0)

    total = vb + vs
    buy_share = (vb / total * 100) if total > 0 else 50
    if buy_share >= 55:
        vol_bias = "buy-side dominant"
    elif buy_share <= 45:
        vol_bias = "sell-side dominant"
    else:
        vol_bias = "roughly balanced"
    liq_bias = "long liquidations leading" if ll > ls else "short liquidations leading"
    oi_sign = "+" if oi_chg >= 0 else ""

    return (
        f"Over the last {lookback}h, BTC mark is around ${mark:,.0f}; "
        f"volume profile is {vol_bias} (buy {vb:,.0f} / sell {vs:,.0f} BTC), "
        f"with {liq_bias} (long {ll:,.0f} / short {ls:,.0f} BTC) "
        f"and open interest at {oi_sign}{oi_chg:.2f}%."
    )


def clean_output(text: str | None) -> str | None:
    """Normalise model output: single line, strip quotes/brackets."""
    if not text:
        return None
    text = text.replace("\n", " ").strip()
    # Strip a single layer of ASCII / JP quotes if model wrapped the line
    for left, right in (('"', '"'), ("'", "'"), ("「", "」"), ("『", "』")):
        if text.startswith(left) and text.endswith(right):
            text = text[len(left):-len(right)].strip()
            break
    return text or None


def _resolve_order() -> tuple[str, ...]:
    raw = (os.getenv("COMMENTARY_PROVIDERS") or "").strip()
    if not raw:
        return DEFAULT_ORDER
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def _load_provider(name: str) -> Callable[[dict], str | None] | None:
    """Lazy-import a provider's generate_commentary, or None if module missing."""
    modules = {
        "gemini": "gemini_commentary",
        "grok": "grok_commentary",
        "openai": "openai_commentary",
        "deepl": "deepl_commentary",
    }
    mod_name = modules.get(name)
    if mod_name is None:
        log.warning("commentary: unknown provider %r", name)
        return None
    try:
        mod = __import__(mod_name)
    except ImportError as e:
        log.warning("commentary: import %s failed: %s", mod_name, e)
        return None
    fn = getattr(mod, "generate_commentary", None)
    if fn is None:
        log.warning("commentary: %s has no generate_commentary()", mod_name)
        return None
    return fn


def generate_commentary(meta: dict) -> str | None:
    """Run providers in priority order, return the first non-empty result."""
    for name in _resolve_order():
        fn = _load_provider(name)
        if fn is None:
            continue
        try:
            text = fn(meta)
        except Exception as e:  # noqa: BLE001
            log.warning("commentary: %s raised: %s", name, e)
            continue
        text = clean_output(text)
        if text:
            log.info("commentary: %s succeeded (%d chars)", name, len(text))
            return text
        log.info("commentary: %s returned empty, trying next", name)
    log.info("commentary: all providers exhausted, returning None")
    return None


if __name__ == "__main__":
    import json
    import sys

    from dotenv import load_dotenv

    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    if not sys.stdin.isatty():
        sample = json.load(sys.stdin)
    else:
        sample = {
            "mark": 77963, "lookback_hours": 24,
            "total_vol_buy_btc": 59239, "total_vol_sell_btc": 59559,
            "total_liq_short_btc": 940, "total_liq_long_btc": 857,
            "oi_total_btc": 200770, "oi_change_pct_24h": 0.6,
        }
    print(generate_commentary(sample))
