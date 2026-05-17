"""Fetch BTC perp data via Coinalyze API (single endpoint, multi-exchange).

Why Coinalyze instead of direct exchange APIs:
  GitHub Actions runners use US-based Azure IPs, which are blocked by
  Binance (451) and Bybit (403). Coinalyze is a paid aggregator that
  fetches & normalizes data from all major exchanges, accessible from
  anywhere via a single API key. The free tier (40 req/min) is plenty
  for our 6-hour cron.

  Pattern + key reused from sister project Perp-oi-chart.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE = "https://api.coinalyze.net/v1"
TIMEOUT = 25

# BTC perpetuals (USDT-margined) on the 3 target exchanges.
# All have has_buy_sell_data=true and oi_lq_vol_denominated_in=BASE_ASSET (BTC).
SYMBOLS = {
    "binance": "BTCUSDT_PERP.A",
    "bybit":   "BTCUSDT.6",
    "okx":     "BTCUSDT_PERP.3",
}


class CoinalyzeError(RuntimeError):
    pass


def _key() -> str:
    k = os.environ.get("COINALYZE_API_KEY", "").strip()
    if not k:
        raise CoinalyzeError("COINALYZE_API_KEY is not set")
    return k


def _get(path: str, params: dict[str, Any] | None = None, *, retries: int = 3) -> Any:
    params = dict(params or {})
    headers = {"api_key": _key()}
    url = f"{BASE}{path}"
    last_err = "no attempts"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:
            last_err = f"request error: {e}"
            time.sleep(1 + attempt)
            continue
        if r.status_code == 429:
            try:
                wait = float(r.headers.get("Retry-After", "0") or "0")
            except ValueError:
                wait = 0.0
            wait = max(wait, 5.0 * (attempt + 1))
            log.warning("coinalyze 429, waiting %.1fs", wait)
            time.sleep(wait + 0.5)
            continue
        if r.status_code >= 400:
            raise CoinalyzeError(f"GET {path} returned {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError as e:
            last_err = f"non-JSON: {e}: {r.text[:200]}"
            time.sleep(1 + attempt)
    raise CoinalyzeError(f"GET {path} failed after {retries} attempts: {last_err}")


# ---------------- domain dataclasses ----------------

@dataclass
class VolBucket:
    """One time bucket aggregated across exchanges."""
    ts_ms: int
    price: float          # typical (HLC/3), volume-weighted across exchanges
    buy_btc: float
    sell_btc: float


@dataclass
class Liquidation:
    """One time-bucket aggregated liquidation entry (not individual fill)."""
    ts_ms: int
    price: float
    qty_btc: float
    side: str             # "long" or "short"
    exchange: str


@dataclass
class OIPoint:
    ts_ms: int
    oi_btc: float
    exchange: str


# ---------------- mark price (current) ----------------

def get_mark_price() -> float:
    """Most-recent close across our 3 symbols, median."""
    now_s = int(time.time())
    data = _get(
        "/ohlcv-history",
        {
            "symbols": ",".join(SYMBOLS.values()),
            "interval": "1min",
            "from": now_s - 600,
            "to": now_s,
        },
    )
    closes = []
    for series in data:
        hist = series.get("history") or []
        if hist:
            closes.append(float(hist[-1]["c"]))
    if not closes:
        raise CoinalyzeError("get_mark_price: no closes returned")
    closes.sort()
    return closes[len(closes) // 2]


# ---------------- volume profile ----------------

# Coinalyze allowed intervals: 1min, 5min, 15min, 30min, 1hour, 2hour, 4hour, 6hour, 12hour, daily
COINALYZE_INTERVAL_MAP = {
    1: "1min", 5: "5min", 15: "15min", 30: "30min",
    60: "1hour", 120: "2hour", 240: "4hour",
}


def aggregate_volume_profile(hours: float = 24.0, interval_min: int = 5) -> list[VolBucket]:
    """Aggregate OHLCV across all 3 exchanges into per-bucket VolBuckets.

    Buy/sell split uses Coinalyze's real `bv` (taker buy volume) per candle.
    """
    interval = COINALYZE_INTERVAL_MAP.get(interval_min, "5min")
    now_s = int(time.time())
    since_s = now_s - int(hours * 3600)

    data = _get(
        "/ohlcv-history",
        {
            "symbols": ",".join(SYMBOLS.values()),
            "interval": interval,
            "from": since_s,
            "to": now_s,
        },
    )
    log.info("coinalyze ohlcv: %d series", len(data))

    # Index by ts (in seconds, Coinalyze uses seconds)
    by_ts: dict[int, dict[str, dict]] = {}
    for series in data:
        sym = series["symbol"]
        # Map back to our exchange label
        ex_label = next((k for k, v in SYMBOLS.items() if v == sym), sym)
        for c in series.get("history") or []:
            ts = int(c["t"])
            by_ts.setdefault(ts, {})[ex_label] = c

    buckets: list[VolBucket] = []
    for ts, ex_map in sorted(by_ts.items()):
        total_v = sum(float(c.get("v", 0) or 0) for c in ex_map.values())
        if total_v <= 0:
            continue
        total_bv = sum(float(c.get("bv", 0) or 0) for c in ex_map.values())
        buy_pct = max(0.0, min(1.0, total_bv / total_v))

        # Typical price: volume-weighted average of per-exchange typical prices
        def typical(c):
            return (float(c["h"]) + float(c["l"]) + float(c["c"])) / 3

        weighted_price = sum(typical(c) * float(c["v"] or 0) for c in ex_map.values()) / total_v

        buckets.append(VolBucket(
            ts_ms=ts * 1000,
            price=weighted_price,
            buy_btc=total_v * buy_pct,
            sell_btc=total_v * (1.0 - buy_pct),
        ))
    return buckets


# ---------------- open interest history ----------------

def fetch_aggregated_oi(hours: float = 24.0, period: str = "1h") -> dict[str, list[OIPoint]]:
    """Per-exchange OI history (BTC denominated)."""
    interval_min = {"5m":5,"15m":15,"30m":30,"1h":60,"4h":240}.get(period, 60)
    interval = COINALYZE_INTERVAL_MAP.get(interval_min, "1hour")
    now_s = int(time.time())
    since_s = now_s - int(hours * 3600)

    data = _get(
        "/open-interest-history",
        {
            "symbols": ",".join(SYMBOLS.values()),
            "interval": interval,
            "from": since_s,
            "to": now_s,
            "convert_to_usd": "false",   # all 3 symbols are denominated in BASE_ASSET (BTC)
        },
    )

    result: dict[str, list[OIPoint]] = {ex: [] for ex in SYMBOLS}
    for series in data:
        sym = series["symbol"]
        ex_label = next((k for k, v in SYMBOLS.items() if v == sym), None)
        if ex_label is None:
            continue
        for p in series.get("history") or []:
            # OHLC of OI; use close
            result[ex_label].append(OIPoint(
                ts_ms=int(p["t"]) * 1000,
                oi_btc=float(p["c"]),
                exchange=ex_label,
            ))
        log.info("OI %s: %d points", ex_label, len(result[ex_label]))
    return result


# ---------------- estimated OI delta by price ----------------

@dataclass
class OIDeltaBucket:
    """Estimated OI change attributed to a price bucket within a sub-window.

    `oi_delta_btc` is positive when net OI grew during that sub-window
    (new positions opened), negative when net OI shrank (positions closed
    or liquidated). Sign-by-price is an *estimate*: it assumes the dOI in
    the parent 1h bucket distributes across price proportionally to the
    intra-window taker-aware volume share.
    """
    ts_ms: int            # mid-timestamp of the underlying fine bucket
    price: float          # typical price of the fine bucket
    oi_delta_btc: float


def compute_oi_delta_profile(
    hours: float = 24.0,
    fine_interval_min: int = 5,
    oi_interval_min: int = 60,
) -> list[OIDeltaBucket]:
    """Return per-(time × price) estimated OI delta for the lookback window.

    Strategy:
      1. Pull OI history at `oi_interval_min` (default 1h) — gives dOI per hour
         summed across 3 exchanges.
      2. Pull aggregate 5min volume buckets — gives price + volume per 5min.
      3. For each 1h parent bucket, attribute its dOI to the 12 child 5min
         buckets proportionally to each child's share of the hour's volume.

    This is a heuristic. We do NOT claim accuracy; the chart layer must
    label it as estimated.
    """
    # 1. fetch hourly OI per exchange and sum to a single total series
    oi_data = fetch_aggregated_oi(hours=hours + 1, period="1h")
    by_ts: dict[int, float] = {}
    for pts in oi_data.values():
        for p in pts:
            by_ts[p.ts_ms] = by_ts.get(p.ts_ms, 0.0) + p.oi_btc
    if len(by_ts) < 2:
        log.warning("OI delta: insufficient OI samples (%d), skipping", len(by_ts))
        return []
    oi_sorted_ts = sorted(by_ts)
    # dOI[ts] = OI[ts] - OI[prev]; assigned to the hour ENDING at ts.
    doi_at_hour: dict[int, float] = {}
    for i in range(1, len(oi_sorted_ts)):
        doi_at_hour[oi_sorted_ts[i]] = by_ts[oi_sorted_ts[i]] - by_ts[oi_sorted_ts[i - 1]]

    # 2. fetch fine-grained volume buckets across the window
    fine = aggregate_volume_profile(hours=hours, interval_min=fine_interval_min)
    if not fine:
        return []

    # 3. attribute dOI to each fine bucket.
    # Parent hour = first OI sample whose ts >= fine_bucket.ts_ms_end.
    hour_ms = oi_interval_min * 60 * 1000
    # Group fine buckets by which parent-hour they fall into.
    fine_by_parent: dict[int, list] = {}
    for b in fine:
        # Pick the smallest oi-hour-end timestamp >= bucket end
        end = b.ts_ms + fine_interval_min * 60 * 1000
        parent = next((t for t in oi_sorted_ts if t >= end), None)
        if parent is None:
            continue
        fine_by_parent.setdefault(parent, []).append(b)

    out: list[OIDeltaBucket] = []
    for parent_ts, children in fine_by_parent.items():
        doi = doi_at_hour.get(parent_ts)
        if doi is None or doi == 0:
            continue
        total_vol = sum(c.buy_btc + c.sell_btc for c in children)
        if total_vol <= 0:
            continue
        for c in children:
            share = (c.buy_btc + c.sell_btc) / total_vol
            out.append(OIDeltaBucket(
                ts_ms=c.ts_ms,
                price=c.price,
                oi_delta_btc=doi * share,
            ))

    log.info("OI-delta estimate: %d fine buckets across %d parent hours",
             len(out), len(fine_by_parent))
    return out


# ---------------- liquidations ----------------

def fetch_all_liquidations(since_ms: int, interval_min: int = 60) -> list[Liquidation]:
    """Liquidation history bucketed per interval, per exchange.

    Coinalyze returns aggregated per-bucket {l: long-liq-BTC, s: short-liq-BTC},
    NOT individual fills. For the price-bucket chart, we use the close price of
    the parent OHLCV candle as the price level — fetched in parallel below.
    """
    interval = COINALYZE_INTERVAL_MAP.get(interval_min, "1hour")
    now_s = int(time.time())
    since_s = since_ms // 1000

    # Fetch liquidations + OHLCV (for price labels) in two parallel calls.
    liq_data = _get(
        "/liquidation-history",
        {
            "symbols": ",".join(SYMBOLS.values()),
            "interval": interval,
            "from": since_s,
            "to": now_s,
            "convert_to_usd": "false",
        },
    )
    ohlcv_data = _get(
        "/ohlcv-history",
        {
            "symbols": ",".join(SYMBOLS.values()),
            "interval": interval,
            "from": since_s,
            "to": now_s,
        },
    )

    # Index OHLCV closes for price lookup: (symbol, ts) -> close
    price_idx: dict[tuple[str, int], float] = {}
    for series in ohlcv_data:
        sym = series["symbol"]
        for c in series.get("history") or []:
            price_idx[(sym, int(c["t"]))] = float(c["c"])

    out: list[Liquidation] = []
    for series in liq_data:
        sym = series["symbol"]
        ex_label = next((k for k, v in SYMBOLS.items() if v == sym), sym)
        for row in series.get("history") or []:
            ts_s = int(row["t"])
            price = price_idx.get((sym, ts_s))
            if price is None:
                continue
            long_btc = float(row.get("l", 0) or 0)
            short_btc = float(row.get("s", 0) or 0)
            if long_btc > 0:
                out.append(Liquidation(
                    ts_ms=ts_s * 1000, price=price, qty_btc=long_btc,
                    side="long", exchange=ex_label,
                ))
            if short_btc > 0:
                out.append(Liquidation(
                    ts_ms=ts_s * 1000, price=price, qty_btc=short_btc,
                    side="short", exchange=ex_label,
                ))
    log.info("liquidations: %d entries across %d exchanges",
             len(out), len({l.exchange for l in out}))
    return out
