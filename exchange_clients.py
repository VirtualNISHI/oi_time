"""Fetch BTC perp klines + liquidations from Binance, Bybit, OKX (public REST)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

import requests

log = logging.getLogger(__name__)

TIMEOUT = 15

BINANCE_SYMBOL = "BTCUSDT"
BYBIT_SYMBOL = "BTCUSDT"
OKX_INST = "BTC-USDT-SWAP"
OKX_CONTRACT_SIZE_BTC = 0.01  # 1 contract = 0.01 BTC on BTC-USDT-SWAP


@dataclass
class Candle:
    ts_ms: int          # open time
    open: float
    high: float
    low: float
    close: float
    volume_btc: float
    taker_buy_btc: float | None = None   # taker buy base-asset volume (None if not provided)
    exchange: str = ""

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3


@dataclass
class VolBucket:
    """One time bucket (e.g. 5min) with synthesized buy/sell split."""
    ts_ms: int
    price: float          # typical price
    buy_btc: float
    sell_btc: float


@dataclass
class Liquidation:
    ts_ms: int
    price: float
    qty_btc: float
    side: str             # "long" or "short" — the side that was liquidated
    exchange: str


# ---------------- mark price (current) ----------------

def get_mark_price() -> float:
    """Median mark price across Binance/Bybit/OKX. Returns first successful if others fail."""
    prices: list[float] = []

    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": BINANCE_SYMBOL}, timeout=TIMEOUT,
        )
        r.raise_for_status()
        prices.append(float(r.json()["markPrice"]))
    except Exception as e:
        log.warning("binance markPrice failed: %s", e)

    try:
        r = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "linear", "symbol": BYBIT_SYMBOL}, timeout=TIMEOUT,
        )
        r.raise_for_status()
        prices.append(float(r.json()["result"]["list"][0]["markPrice"]))
    except Exception as e:
        log.warning("bybit markPrice failed: %s", e)

    try:
        r = requests.get(
            "https://www.okx.com/api/v5/public/mark-price",
            params={"instId": OKX_INST, "instType": "SWAP"}, timeout=TIMEOUT,
        )
        r.raise_for_status()
        prices.append(float(r.json()["data"][0]["markPx"]))
    except Exception as e:
        log.warning("okx markPrice failed: %s", e)

    if not prices:
        raise RuntimeError("All mark-price sources failed")
    prices.sort()
    return prices[len(prices) // 2]


# ---------------- klines (per-exchange) ----------------

def fetch_binance_klines(interval: str = "5m", hours: float = 24.0) -> list[Candle]:
    """Binance fapi klines. interval = '1m'|'3m'|'5m'|'15m'|'1h'... limit max 1500."""
    minutes_per = int(interval.rstrip("mh")) * (60 if interval.endswith("h") else 1)
    limit = min(1500, max(1, int((hours * 60) / minutes_per) + 1))
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/klines",
        params={"symbol": BINANCE_SYMBOL, "interval": interval, "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out: list[Candle] = []
    for k in r.json():
        out.append(Candle(
            ts_ms=int(k[0]),
            open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4]),
            volume_btc=float(k[5]),
            taker_buy_btc=float(k[9]),  # takerBuyBaseAssetVolume
            exchange="binance",
        ))
    return out


def fetch_bybit_klines(interval: str = "5", hours: float = 24.0) -> list[Candle]:
    """Bybit V5 kline. interval in minutes as string: '1','3','5','15','30','60','120','240','D'."""
    try:
        per = int(interval)
    except ValueError:
        per = 60  # fallback for 'D' etc.
    limit = min(1000, max(1, int((hours * 60) / per) + 1))
    r = requests.get(
        "https://api.bybit.com/v5/market/kline",
        params={"category": "linear", "symbol": BYBIT_SYMBOL,
                "interval": interval, "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out: list[Candle] = []
    for k in r.json().get("result", {}).get("list", []):
        # bybit returns newest first
        out.append(Candle(
            ts_ms=int(k[0]),
            open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4]),
            volume_btc=float(k[5]),
            exchange="bybit",
        ))
    out.sort(key=lambda c: c.ts_ms)
    return out


def fetch_okx_klines(bar: str = "5m", hours: float = 24.0) -> list[Candle]:
    """OKX candles. bar = '1m'|'3m'|'5m'|'15m'... limit max 300."""
    per = int(bar.rstrip("mh")) * (60 if bar.endswith("h") else 1)
    limit = min(300, max(1, int((hours * 60) / per) + 1))
    r = requests.get(
        "https://www.okx.com/api/v5/market/candles",
        params={"instId": OKX_INST, "bar": bar, "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out: list[Candle] = []
    for k in r.json().get("data", []):
        # OKX: [ts, o, h, l, c, vol(contracts), volCcy(BTC), volCcyQuote(USDT), confirm]
        out.append(Candle(
            ts_ms=int(k[0]),
            open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4]),
            volume_btc=float(k[6]),  # volCcy = base ccy = BTC
            exchange="okx",
        ))
    out.sort(key=lambda c: c.ts_ms)
    return out


def aggregate_volume_profile(hours: float = 24.0, interval_min: int = 5) -> list[VolBucket]:
    """Returns one VolBucket per time bucket, summed across exchanges.

    Buy/sell split is derived from Binance's real taker_buy ratio when available
    for that minute bucket; otherwise from candle direction (close>open ⇒ 55/45 buy bias).
    """
    binance = fetch_binance_klines(f"{interval_min}m", hours)
    bybit = fetch_bybit_klines(str(interval_min), hours)
    okx = fetch_okx_klines(f"{interval_min}m", hours)

    log.info("klines fetched: binance=%d bybit=%d okx=%d", len(binance), len(bybit), len(okx))

    # Index by ts_ms (klines naturally align on the same bucket boundary)
    by_ts: dict[int, dict[str, Candle]] = {}
    for c in binance:
        by_ts.setdefault(c.ts_ms, {})["binance"] = c
    for c in bybit:
        by_ts.setdefault(c.ts_ms, {})["bybit"] = c
    for c in okx:
        by_ts.setdefault(c.ts_ms, {})["okx"] = c

    buckets: list[VolBucket] = []
    for ts, ex_map in sorted(by_ts.items()):
        total = sum(c.volume_btc for c in ex_map.values())
        if total <= 0:
            continue

        b = ex_map.get("binance")
        if b and b.volume_btc > 0 and b.taker_buy_btc is not None:
            buy_pct = max(0.0, min(1.0, b.taker_buy_btc / b.volume_btc))
        else:
            # Heuristic from any available candle's direction
            ref = b or ex_map.get("bybit") or ex_map.get("okx")
            if ref is None:
                buy_pct = 0.5
            elif ref.close > ref.open:
                buy_pct = 0.55
            elif ref.close < ref.open:
                buy_pct = 0.45
            else:
                buy_pct = 0.5

        # Typical price: weighted by per-exchange volume
        weighted_price = sum(c.typical * c.volume_btc for c in ex_map.values()) / total

        buckets.append(VolBucket(
            ts_ms=ts,
            price=weighted_price,
            buy_btc=total * buy_pct,
            sell_btc=total * (1.0 - buy_pct),
        ))

    return buckets


# ---------------- open interest history ----------------

@dataclass
class OIPoint:
    ts_ms: int
    oi_btc: float       # in base asset (BTC); USD-equivalent OI / mark price
    exchange: str


def fetch_binance_oi_hist(period: str = "1h", hours: float = 24.0) -> list[OIPoint]:
    """Binance futures/data/openInterestHist. period in {5m,15m,30m,1h,2h,4h,6h,12h,1d}."""
    minutes_per = {"5m":5,"15m":15,"30m":30,"1h":60,"2h":120,"4h":240,"6h":360,"12h":720,"1d":1440}.get(period, 60)
    limit = min(500, max(1, int((hours * 60) / minutes_per) + 1))
    r = requests.get(
        "https://fapi.binance.com/futures/data/openInterestHist",
        params={"symbol": BINANCE_SYMBOL, "period": period, "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out: list[OIPoint] = []
    for d in r.json():
        # sumOpenInterest is in base asset (BTC) directly
        out.append(OIPoint(
            ts_ms=int(d["timestamp"]),
            oi_btc=float(d["sumOpenInterest"]),
            exchange="binance",
        ))
    return out


def fetch_bybit_oi_hist(interval: str = "1h", hours: float = 24.0) -> list[OIPoint]:
    """Bybit V5 open-interest. intervalTime in {5min,15min,30min,1h,4h,1d}."""
    interval_map = {"5m":"5min","15m":"15min","30m":"30min","1h":"1h","4h":"4h","1d":"1d"}
    iv = interval_map.get(interval, "1h")
    minutes_per = {"5min":5,"15min":15,"30min":30,"1h":60,"4h":240,"1d":1440}.get(iv, 60)
    limit = min(200, max(1, int((hours * 60) / minutes_per) + 1))
    r = requests.get(
        "https://api.bybit.com/v5/market/open-interest",
        params={"category": "linear", "symbol": BYBIT_SYMBOL,
                "intervalTime": iv, "limit": limit},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out: list[OIPoint] = []
    # Bybit returns OI in *contract* count for linear perp where 1 contract = 1 USDT-worth?
    # Actually for BTCUSDT linear, openInterest is in BTC. Confirmed by docs.
    for d in r.json().get("result", {}).get("list", []):
        out.append(OIPoint(
            ts_ms=int(d["timestamp"]),
            oi_btc=float(d["openInterest"]),
            exchange="bybit",
        ))
    out.sort(key=lambda p: p.ts_ms)
    return out


def fetch_okx_oi_hist(period: str = "1H", hours: float = 24.0) -> list[OIPoint]:
    """OKX rubik/stat/contracts/open-interest-volume. period in {5m,1H,4H,1D}."""
    minutes_per = {"5m":5,"1H":60,"4H":240,"1D":1440}.get(period, 60)
    limit_hint = int((hours * 60) / minutes_per) + 1  # informational, OKX returns all available
    r = requests.get(
        "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
        params={"ccy": "BTC", "period": period},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    out: list[OIPoint] = []
    # Returns: data = [[ts, oiUsd, volUsd], ...] (USD denominated)
    rows = r.json().get("data", [])
    # We need the mark price near each point to convert USD OI to BTC; for a 24h panel
    # use current mark as a passable approximation (OI in BTC changes faster than mark).
    mark = None
    try:
        mark = get_mark_price()
    except Exception:
        pass
    for row in rows[:limit_hint]:
        if len(row) < 2:
            continue
        ts = int(row[0])
        oi_usd = float(row[1])
        oi_btc = (oi_usd / mark) if mark else 0.0
        out.append(OIPoint(ts_ms=ts, oi_btc=oi_btc, exchange="okx"))
    out.sort(key=lambda p: p.ts_ms)
    return out


def fetch_aggregated_oi(hours: float = 24.0, period: str = "1h") -> dict[str, list[OIPoint]]:
    """Return {exchange: [OIPoint, ...]} for the lookback window."""
    result: dict[str, list[OIPoint]] = {}
    for name, fn in (
        ("binance", lambda: fetch_binance_oi_hist(period, hours)),
        ("bybit",   lambda: fetch_bybit_oi_hist(period, hours)),
        ("okx",     lambda: fetch_okx_oi_hist({"1h":"1H","4h":"4H","1d":"1D"}.get(period,"1H"), hours)),
    ):
        try:
            pts = fn()
            log.info("OI %s: %d points", name, len(pts))
            result[name] = pts
        except Exception as e:
            log.warning("OI %s failed: %s", name, e)
            result[name] = []
    return result


# ---------------- liquidations ----------------

def fetch_okx_liquidations(since_ms: int) -> list[Liquidation]:
    """OKX filled liquidation orders (public, free).
    Uses instFamily=BTC-USDT, state=filled.
    posSide tells us which side was liquidated.
    Pagination: 'before' = ts of oldest item in previous page (to go further back).
    """
    out: list[Liquidation] = []
    before = ""  # cursor for older pages

    for page in range(40):  # safety cap
        params = {
            "instType": "SWAP",
            "instFamily": "BTC-USDT",
            "state": "filled",
            "limit": "100",
        }
        if before:
            params["before"] = before

        try:
            r = requests.get(
                "https://www.okx.com/api/v5/public/liquidation-orders",
                params=params, timeout=TIMEOUT,
            )
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            log.warning("okx liquidations page %d failed: %s", page, e)
            break

        data = payload.get("data", [])
        if not data:
            break

        # Each top-level item is per (instType, instFamily); details has the actual fills
        page_oldest_ts = None
        page_count_kept = 0
        for inst in data:
            details = inst.get("details", [])
            for d in details:
                ts = int(d["ts"])
                page_oldest_ts = ts if page_oldest_ts is None else min(page_oldest_ts, ts)
                if ts < since_ms:
                    continue
                pos_side = d.get("posSide", "").lower()
                if pos_side not in ("long", "short"):
                    # fallback to 'side' (buy-side liq order = short was liquidated)
                    side_raw = d.get("side", "").lower()
                    pos_side = "short" if side_raw == "buy" else "long"
                out.append(Liquidation(
                    ts_ms=ts,
                    price=float(d["bkPx"]),
                    qty_btc=float(d["sz"]) * OKX_CONTRACT_SIZE_BTC,
                    side=pos_side,
                    exchange="okx",
                ))
                page_count_kept += 1

        # Stop paginating if we've already gone past the window
        if page_oldest_ts is None or page_oldest_ts < since_ms:
            break
        before = str(page_oldest_ts)
        # be polite
        time.sleep(0.05)

    log.info("okx liquidations collected: %d (since %s)",
             len(out), time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(since_ms / 1000)))
    return out


def fetch_all_liquidations(since_ms: int) -> list[Liquidation]:
    # OKX is the only exchange with a free REST endpoint for historical liquidations.
    # Binance/Bybit only expose them via WebSocket which isn't compatible with stateless
    # GHA cron. OKX serves as a reasonable proxy for visualization purposes.
    return fetch_okx_liquidations(since_ms)
