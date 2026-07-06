"""Generate a BTC profile chart and post it to Discord and X."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import tweepy
from dotenv import load_dotenv

JST = timezone(timedelta(hours=9))
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

log = logging.getLogger("post_chart")


def setup_logging() -> None:
    # Force UTF-8 on Windows consoles that default to cp932/Shift-JIS
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconf = getattr(stream, "reconfigure", None)
        if reconf is not None:
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(Path(__file__).parent / "post_chart.log", encoding="utf-8"),
        ],
    )


def latest_image(folder: Path) -> Path | None:
    if not folder.exists():
        return None
    candidates = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_state(state_file: Path) -> dict:
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state_file: Path, state: dict) -> None:
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def post_to_discord(webhook_url: str, image_path: Path, text: str) -> None:
    with image_path.open("rb") as f:
        files = {"file": (image_path.name, f, "image/png")}
        data = {"content": text}
        r = requests.post(webhook_url, data=data, files=files, timeout=30)
    r.raise_for_status()
    log.info("Discord posted: status=%s", r.status_code)


class _TimeoutSession(requests.Session):
    """requests.Session that enforces a default timeout on every call.

    tweepy.Client (X API v2) builds a plain requests.Session internally and
    never passes a timeout to it, so a stalled connection hangs forever —
    the only thing that eventually kills it is the GitHub Actions job
    timeout. Observed 2026-06-17: a run sat for the full 15-minute job
    timeout, and because the workflow's concurrency group serializes runs,
    every later-queued FOMC dense-window trigger that day was delayed
    behind it. tweepy.API already defaults to timeout=60; this makes both
    clients bounded.
    """

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", 30)
        return super().request(*args, **kwargs)


def post_to_x(creds: dict, image_path: Path, text: str) -> None:
    auth = tweepy.OAuth1UserHandler(
        creds["api_key"], creds["api_secret"],
        creds["access_token"], creds["access_token_secret"],
    )
    api_v1 = tweepy.API(auth, timeout=30)
    media = api_v1.media_upload(filename=str(image_path))

    client = tweepy.Client(
        consumer_key=creds["api_key"],
        consumer_secret=creds["api_secret"],
        access_token=creds["access_token"],
        access_token_secret=creds["access_token_secret"],
    )
    client.session = _TimeoutSession()
    resp = client.create_tweet(text=text, media_ids=[media.media_id])
    tweet_id = resp.data.get("id") if resp.data else None
    log.info("X posted: tweet_id=%s", tweet_id)


def format_text(template: str, meta: dict | None, commentary: str | None = None) -> str:
    import re
    timestamp = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    ctx = {
        "timestamp": timestamp,
        "mark": f"${meta['mark']:,.0f}" if meta and meta.get("mark") else "",
        "lookback_h": int(meta.get("lookback_hours", 24)) if meta else 24,
        "vol_buy": f"{meta.get('total_vol_buy_btc', 0):,.0f}" if meta else "",
        "vol_sell": f"{meta.get('total_vol_sell_btc', 0):,.0f}" if meta else "",
        "liq_short": f"{meta.get('total_liq_short_btc', 0):,.0f}" if meta else "",
        "liq_long": f"{meta.get('total_liq_long_btc', 0):,.0f}" if meta else "",
        "oi": (
            f"{meta['oi_total_btc']:,.0f}"
            if meta and meta.get("oi_total_btc") is not None else "n/a"
        ),
        "oi_chg": (
            f"{'+' if meta['oi_change_pct_24h'] >= 0 else ''}{meta['oi_change_pct_24h']:.2f}%"
            if meta and meta.get("oi_change_pct_24h") is not None else "n/a"
        ),
        # Prefix bullet only when we have something to say; empty string when not,
        # so the surrounding blank lines collapse via the regex below.
        "commentary": f"▸ {commentary}" if commentary else "",
    }
    try:
        text = template.format(**ctx)
    except KeyError as e:
        log.warning("template key missing: %s — falling back to {timestamp} only", e)
        text = template.format(timestamp=timestamp)
    # Preserve intentional blank lines (paragraph breaks) but collapse runs of 3+
    # newlines (= 2+ empty lines, which happen when {commentary} is empty between
    # already-blank lines) down to a single blank line. Then strip surrounding ws.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate + post a BTC profile chart")
    parser.add_argument("--image", type=Path, help="Skip chart generation; post this file")
    parser.add_argument("--no-generate", action="store_true",
                        help="Use the latest existing image in IMAGES_DIR instead of generating")
    parser.add_argument("--text", type=str, help="Override the post text")
    parser.add_argument("--force", action="store_true", help="Post even if dedup would skip")
    parser.add_argument("--dry-run", action="store_true", help="Generate but don't post")
    parser.add_argument("--skip-discord", action="store_true")
    parser.add_argument("--skip-x", action="store_true",
                        help="Skip X. Note: X is also gated by ENABLE_X=true in .env.")
    parser.add_argument("--enable-x", action="store_true",
                        help="One-shot override of ENABLE_X gate (must still pass --skip-x absence).")
    parser.add_argument("--require-fomc-window", action="store_true",
                        help="Only proceed if now is inside a FOMC announcement window "
                             "(see fomc_schedule.py). Used by the dense FOMC cron so the "
                             "date-targeted schedule can't post outside the window or in a "
                             "later year. Also settable via REQUIRE_FOMC_WINDOW=true.")
    # Chart params (override .env)
    parser.add_argument("--lookback-hours", type=float)
    parser.add_argument("--range-pct", type=float)
    args = parser.parse_args()

    setup_logging()
    project_dir = Path(__file__).parent
    load_dotenv(project_dir / ".env")

    # FOMC dense-window gate. When enabled, bail out (success, no post) unless
    # now is inside a FOMC announcement window. This lets the workflow schedule
    # a 30-min cron on the FOMC calendar dates while the authoritative window
    # check lives in fomc_schedule.py — which also stops the date-targeted cron
    # from posting in a later year. Checked before any API calls so skipped
    # runs are free.
    require_fomc = args.require_fomc_window or os.getenv(
        "REQUIRE_FOMC_WINDOW", "false").lower() in ("1", "true", "yes")
    if require_fomc:
        try:
            from fomc_schedule import active_announcement
        except Exception as e:
            log.error("FOMC gate requested but fomc_schedule import failed: %s", e)
            return 1
        ann = active_announcement(datetime.now(timezone.utc))
        if ann is None:
            log.info("REQUIRE_FOMC_WINDOW set and now is outside every FOMC window - skipping")
            return 0
        log.info("FOMC window active (announcement %s) - dense post proceeding", ann.isoformat())

    images_dir = Path(os.getenv("IMAGES_DIR", project_dir / "images")).resolve()
    state_file = Path(os.getenv("STATE_FILE", project_dir / ".last_posted.json")).resolve()
    template = os.getenv(
        "POST_TEMPLATE",
        "【BTC価格帯別OIマップ】 Mark {mark}\n"
        "\n"
        "Vol  買 {vol_buy} / 売 {vol_sell} BTC\n"
        "Liq  S {liq_short} / L {liq_long} BTC\n"
        "OI   {oi} BTC ({oi_chg})\n"
        "\n"
        "{commentary}\n"
        "\n"
        "{timestamp} JST  #BTC #Bitcoin\n"
        "※OIはAIが収集したもので推定となります",
    )
    lookback_hours = args.lookback_hours if args.lookback_hours is not None else float(
        os.getenv("CHART_LOOKBACK_HOURS", "24"))
    range_pct = args.range_pct if args.range_pct is not None else float(
        os.getenv("CHART_RANGE_PCT", "3"))
    # Optional asymmetric overrides (CHART_RANGE_PCT_UP / _DOWN). When unset
    # the symmetric range_pct is used for both sides.
    range_pct_up_env = os.getenv("CHART_RANGE_PCT_UP")
    range_pct_dn_env = os.getenv("CHART_RANGE_PCT_DOWN")
    range_pct_up = float(range_pct_up_env) if range_pct_up_env else None
    range_pct_down = float(range_pct_dn_env) if range_pct_dn_env else None

    # --- resolve image (generate or pick existing) ---
    meta: dict | None = None
    if args.image:
        image = args.image.resolve()
        if not image.exists():
            log.error("Image not found: %s", image)
            return 1
    elif args.no_generate:
        image = latest_image(images_dir)
        if image is None:
            log.error("No image found in %s", images_dir)
            return 1
    else:
        # Generate fresh chart
        try:
            from chart_builder import build_chart  # imported lazily
        except Exception as e:
            log.exception("chart_builder import failed: %s", e)
            return 1
        out = images_dir / "btc_profile.png"
        try:
            meta = build_chart(
                out,
                lookback_hours=lookback_hours,
                range_pct=range_pct,
                range_pct_up=range_pct_up,
                range_pct_down=range_pct_down,
            )
        except Exception as e:
            log.exception("chart generation failed: %s", e)
            return 1
        image = out

    state = load_state(state_file)
    fingerprint = f"{image}:{int(image.stat().st_mtime)}"
    if not args.force and state.get("last") == fingerprint:
        log.info("No new image since last post (%s) - skipping", image.name)
        return 0

    # Generate commentary via provider fallback chain
    # (gemini → grok → openai → deepl by default). Graceful: None on failure.
    commentary: str | None = None
    if meta:
        try:
            from commentary import generate_commentary
            commentary = generate_commentary(meta)
        except Exception as e:
            log.warning("commentary generation failed (non-fatal): %s", e)

    text = args.text if args.text else format_text(template, meta, commentary)

    log.info("Image: %s", image)
    log.info("Text:  %s", text.replace("\n", " / "))

    if args.dry_run:
        log.info("Dry run - not posting")
        return 0

    errors: list[str] = []

    if not args.skip_discord:
        webhook = os.getenv("DISCORD_WEBHOOK_URL")
        if not webhook:
            errors.append("DISCORD_WEBHOOK_URL not set")
        else:
            try:
                post_to_discord(webhook, image, text)
            except Exception as e:
                log.exception("Discord post failed")
                errors.append(f"discord: {e}")

    # X is double-gated: explicit ENABLE_X=true env (set ON the workflow run only)
    # AND no --skip-x. Local default is OFF so accidental local runs never tweet.
    x_enabled = args.enable_x or os.getenv("ENABLE_X", "false").lower() in ("1", "true", "yes")
    if args.skip_x:
        log.info("X: --skip-x passed, skipping")
    elif not x_enabled:
        log.info("X: ENABLE_X is not 'true' (and --enable-x not passed), skipping")
    else:
        x_creds = {
            "api_key": os.getenv("X_API_KEY"),
            "api_secret": os.getenv("X_API_SECRET"),
            "access_token": os.getenv("X_ACCESS_TOKEN"),
            # Accept either name; polymarket-smart-money uses X_ACCESS_SECRET
            "access_token_secret": os.getenv("X_ACCESS_SECRET") or os.getenv("X_ACCESS_TOKEN_SECRET"),
        }
        if not all(x_creds.values()):
            errors.append("X API credentials incomplete")
        else:
            try:
                post_to_x(x_creds, image, text)
            except Exception as e:
                log.exception("X post failed")
                errors.append(f"x: {e}")

    if errors:
        log.error("Completed with errors: %s", "; ".join(errors))
        return 2

    state["last"] = fingerprint
    state["posted_at"] = datetime.now(JST).isoformat()
    if meta:
        state["last_meta"] = meta
    save_state(state_file, state)
    log.info("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
