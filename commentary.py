"""Japanese market-commentary using the shared jp_translator chain.

歴史的経緯で gemini/grok/openai/deepl 各 commentary.py を独自に持っていたが、
共有モジュール ``jp_translator`` に統合 (Gemini → OpenAI → Grok → DeepL の
フォールバックチェーンと reasoning モデル自動補正を一括で享受)。

このファイルは BOT 固有の SYSTEM_PROMPT / format_user_prompt / clean_output
だけを保持し、provider 呼び出しは共有モジュールに委譲する。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# oi_time は `python -m commentary` / `python commentary.py` の双方で動くため、
# vendored ``jp_translator/`` がプロジェクトルートに置かれている前提で path を通す。
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from jp_translator import generate  # noqa: E402
from jp_translator.providers import deepl_translate  # noqa: E402

log = logging.getLogger(__name__)

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
    """Deterministic English summary used as the DeepL translate source.

    LLM 3 段 (Gemini/OpenAI/Grok) が全部失敗したときの最終フォールバック用に、
    決定的な英文を組み立てて DeepL で日本語化する。
    """
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
    for left, right in (('"', '"'), ("'", "'"), ("「", "」"), ("『", "』")):
        if text.startswith(left) and text.endswith(right):
            text = text[len(left):-len(right)].strip()
            break
    return text or None


def generate_commentary(meta: dict) -> str | None:
    """共有モジュール経由でコメントを生成。失敗時 None。

    試行順:
      1. ``jp_translator.generate`` (Gemini → OpenAI → Grok)
      2. それでもダメなら ``format_english_summary`` を DeepL で訳す
      3. 全段失敗 → None
    """
    # Tier 1〜3: LLM チェーン (Gemini → OpenAI → Grok)
    text = generate(
        system=SYSTEM_PROMPT,
        user=format_user_prompt(meta),
        max_tokens=400,
        temperature=0.3,
    )
    cleaned = clean_output(text)
    if cleaned:
        log.info("commentary: LLM chain succeeded (%d chars)", len(cleaned))
        return cleaned

    # Tier 4: DeepL on deterministic English summary
    deepl_key = os.getenv("DEEPL_API_KEY", "").strip()
    if deepl_key:
        en = format_english_summary(meta)
        translated = deepl_translate(en, api_key=deepl_key, source_lang="EN", target_lang="JA")
        cleaned = clean_output(translated)
        if cleaned:
            log.info("commentary: DeepL fallback (%d chars)", len(cleaned))
            return cleaned

    log.info("commentary: all providers exhausted, returning None")
    return None


if __name__ == "__main__":
    import json

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
