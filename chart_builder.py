"""Build a dark-mode BTC Volume Profile + Liquidation heatmap PNG."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from exchange_clients import (  # noqa: E402
    aggregate_volume_profile,
    fetch_aggregated_oi,
    fetch_all_liquidations,
    get_mark_price,
)

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# Color palette (X / Twitter dark theme)
BG = "#0F1419"
PANEL = "#15202B"
GRID = "#22303C"
TEXT = "#E7E9EA"
SUBTEXT = "#8B98A5"
ACCENT = "#1D9BF0"     # X blue (for mark price line)

COL_BUY_VOL = "#1F9D55"        # green (taker buy volume)
COL_SELL_VOL = "#D93644"       # red (taker sell volume)
COL_LIQ_SHORT = "#A78BFA"      # purple (short liquidations — buy-side pressure)
COL_LIQ_LONG = "#F59E0B"       # amber (long liquidations — sell-side pressure)


def setup_logging() -> None:
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
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _japanese_font():
    """Pick the first available Japanese font, fall back gracefully."""
    candidates = [
        "Yu Gothic", "Yu Gothic UI", "Meiryo", "MS Gothic",
        "Noto Sans CJK JP", "Noto Sans JP", "IPAexGothic", "Hiragino Sans",
        "DejaVu Sans",
    ]
    from matplotlib import font_manager
    available = {f.name for f in font_manager.fontManager.ttflist}
    for c in candidates:
        if c in available:
            return c
    return "DejaVu Sans"


def build_chart(
    output_path: Path,
    lookback_hours: float = 24.0,
    range_pct: float = 3.0,
    n_bins: int = 80,
) -> dict:
    """Generate the chart and write to output_path. Returns a metadata dict."""

    now = datetime.now(timezone.utc)
    since_ms = int((now - timedelta(hours=lookback_hours)).timestamp() * 1000)

    mark = get_mark_price()
    log.info("mark price: $%.2f", mark)

    p_low = mark * (1 - range_pct / 100)
    p_high = mark * (1 + range_pct / 100)
    bins = np.linspace(p_low, p_high, n_bins + 1)
    centers = (bins[:-1] + bins[1:]) / 2

    # ---- Volume profile from aggregated 5-min klines ----
    buckets = aggregate_volume_profile(hours=lookback_hours, interval_min=5)
    buckets = [b for b in buckets if b.ts_ms >= since_ms and p_low <= b.price <= p_high]
    n_trade_buckets = len(buckets)
    log.info("vol buckets in window+range: %d", n_trade_buckets)

    vol_buy = np.zeros(n_bins)
    vol_sell = np.zeros(n_bins)
    for b in buckets:
        idx = np.searchsorted(bins, b.price, side="right") - 1
        if 0 <= idx < n_bins:
            vol_buy[idx] += b.buy_btc
            vol_sell[idx] += b.sell_btc

    # ---- Liquidations ----
    liqs = fetch_all_liquidations(since_ms)
    liqs = [l for l in liqs if p_low <= l.price <= p_high]
    log.info("liquidations in range: %d", len(liqs))

    liq_short = np.zeros(n_bins)  # short liquidated → buy-side pressure → right
    liq_long = np.zeros(n_bins)   # long liquidated  → sell-side pressure → left
    for l in liqs:
        idx = np.searchsorted(bins, l.price, side="right") - 1
        if 0 <= idx < n_bins:
            if l.side == "short":
                liq_short[idx] += l.qty_btc
            else:
                liq_long[idx] += l.qty_btc

    total_liq_short = float(liq_short.sum())
    total_liq_long = float(liq_long.sum())
    total_vol_buy = float(vol_buy.sum())
    total_vol_sell = float(vol_sell.sum())

    # ---- Open Interest history (for right side panel) ----
    oi_data = fetch_aggregated_oi(hours=lookback_hours, period="1h")
    # Total OI per timestamp (interpolate across exchanges to common grid via simple sort+sum)
    oi_total_by_ts: dict[int, float] = {}
    for ex, pts in oi_data.items():
        for p in pts:
            if p.ts_ms >= since_ms:
                oi_total_by_ts[p.ts_ms] = oi_total_by_ts.get(p.ts_ms, 0.0) + p.oi_btc
    oi_ts_sorted = sorted(oi_total_by_ts.keys())
    oi_total_series = [oi_total_by_ts[t] for t in oi_ts_sorted]
    oi_latest = oi_total_series[-1] if oi_total_series else 0.0
    oi_change_pct = (
        (oi_total_series[-1] - oi_total_series[0]) / oi_total_series[0] * 100
        if len(oi_total_series) >= 2 and oi_total_series[0] > 0 else 0.0
    )

    # ---- Plot ----
    jp_font = _japanese_font()
    plt.rcParams.update({
        "font.family": jp_font,
        "axes.unicode_minus": False,
    })

    fig = plt.figure(figsize=(8, 11), dpi=130)
    fig.patch.set_facecolor(BG)
    gs = fig.add_gridspec(
        2, 1, height_ratios=[4, 1], hspace=0.22,
        left=0.10, right=0.96, top=0.89, bottom=0.07,
    )
    ax = fig.add_subplot(gs[0, 0])
    ax_oi = fig.add_subplot(gs[1, 0])
    ax.set_facecolor(PANEL)
    ax_oi.set_facecolor(PANEL)

    bar_h = (bins[1] - bins[0]) * 0.9

    # Volume profile (drawn first / behind)
    ax.barh(centers, vol_buy, height=bar_h, color=COL_BUY_VOL, alpha=0.85,
            label="Trade 買い約定")
    ax.barh(centers, -vol_sell, height=bar_h, color=COL_SELL_VOL, alpha=0.85,
            label="Trade 売り約定")

    # Liquidations stacked on top of volume bars (offset by current bar widths)
    ax.barh(centers, liq_short, height=bar_h * 0.55, left=vol_buy,
            color=COL_LIQ_SHORT, alpha=0.95, label="REKT ショート精算")
    ax.barh(centers, -liq_long, height=bar_h * 0.55, left=-vol_sell,
            color=COL_LIQ_LONG, alpha=0.95, label="REKT ロング精算")

    # Current mark price line
    ax.axhline(mark, color=ACCENT, linewidth=1.4, alpha=0.9)
    ax.text(
        ax.get_xlim()[1] if ax.get_xlim()[1] > 0 else 0,
        mark,
        f"  ${mark:,.0f}",
        color=ACCENT, fontsize=10, va="center", ha="left",
    )

    # Center axis line
    ax.axvline(0, color=GRID, linewidth=0.8)

    # Auto-fit ylim to where data actually lives (with padding), capped by ±range_pct.
    activity = (vol_buy + vol_sell + liq_short + liq_long)
    nonzero = np.where(activity > 0)[0]
    if len(nonzero):
        data_lo = float(centers[nonzero[0]])
        data_hi = float(centers[nonzero[-1]])
        # Always include the mark price + a small visual margin
        span = max(data_hi - data_lo, mark * 0.005)
        pad = span * 0.10
        ylim_lo = max(p_low, min(data_lo, mark) - pad)
        ylim_hi = min(p_high, max(data_hi, mark) + pad)
    else:
        ylim_lo, ylim_hi = p_low, p_high
    ax.set_ylim(ylim_lo, ylim_hi)
    max_abs = max(
        float((vol_buy + liq_short).max() if len(vol_buy) else 0),
        float((vol_sell + liq_long).max() if len(vol_sell) else 0),
        1.0,
    )
    ax.set_xlim(-max_abs * 1.15, max_abs * 1.15)

    ax.tick_params(colors=SUBTEXT)
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.grid(True, axis="y", color=GRID, linewidth=0.4, alpha=0.5)
    ax.grid(True, axis="x", color=GRID, linewidth=0.4, alpha=0.3)

    ax.set_ylabel("Price (USD)", color=SUBTEXT)
    ax.set_xlabel("BTC", color=SUBTEXT)

    # Title + subtitle
    now_jst = now.astimezone(JST)
    title = f"BTC Volume Profile & Liquidations — last {int(lookback_hours)}h"
    subtitle = (
        f"Binance + Bybit + OKX (via Coinalyze)  "
        f"|  {now_jst.strftime('%Y-%m-%d %H:%M')} JST"
    )
    fig.suptitle(title, color=TEXT, fontsize=14, fontweight="bold", y=0.96)
    fig.text(0.10, 0.918, subtitle, color=SUBTEXT, fontsize=9, ha="left")

    # Stats footer
    footer = (
        f"Mark ${mark:,.0f}   |   "
        f"Vol Buy {total_vol_buy:,.1f} BTC  /  Sell {total_vol_sell:,.1f} BTC   |   "
        f"Liq Short {total_liq_short:,.1f} BTC  /  Long {total_liq_long:,.1f} BTC"
    )
    fig.text(0.5, 0.012, footer, color=SUBTEXT, fontsize=8, ha="center")

    # Legend
    leg = ax.legend(
        loc="lower right", framealpha=0.85, fontsize=8,
        facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT,
    )
    for txt in leg.get_texts():
        txt.set_color(TEXT)

    # ---- OI bottom panel ----
    if oi_ts_sorted:
        from matplotlib.dates import DateFormatter, AutoDateLocator, num2date  # noqa: WPS433
        import matplotlib.dates as mdates  # noqa: WPS433

        x_times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc).astimezone(JST) for t in oi_ts_sorted]
        ax_oi.plot(x_times, oi_total_series, color=ACCENT, linewidth=1.6)
        ax_oi.fill_between(x_times, oi_total_series, color=ACCENT, alpha=0.15)

        ax_oi.set_ylabel("OI (BTC)", color=SUBTEXT, fontsize=9)
        # Per-exchange thin lines
        ex_colors = {"binance": "#F0B90B", "bybit": "#F7A600", "okx": "#A78BFA"}
        for ex, pts in oi_data.items():
            pts = [p for p in pts if p.ts_ms >= since_ms]
            if not pts:
                continue
            xs = [datetime.fromtimestamp(p.ts_ms / 1000, tz=timezone.utc).astimezone(JST) for p in pts]
            ys = [p.oi_btc for p in pts]
            ax_oi.plot(xs, ys, color=ex_colors.get(ex, SUBTEXT),
                       linewidth=0.8, alpha=0.7, label=ex)
        ax_oi.legend(
            loc="upper left", fontsize=7, framealpha=0.7,
            facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT, ncol=4,
        )
        ax_oi.xaxis.set_major_locator(AutoDateLocator())
        ax_oi.xaxis.set_major_formatter(DateFormatter("%H:%M", tz=JST))
        ax_oi.set_title(
            f"OI (BTC) — total {oi_latest:,.0f} BTC  ({'+' if oi_change_pct >= 0 else ''}{oi_change_pct:.2f}% / {int(lookback_hours)}h)",
            color=SUBTEXT, fontsize=9, pad=4, loc="left",
        )
    else:
        ax_oi.text(0.5, 0.5, "OI data unavailable", color=SUBTEXT,
                   ha="center", va="center", transform=ax_oi.transAxes)
    ax_oi.tick_params(colors=SUBTEXT, labelsize=8)
    for spine in ax_oi.spines.values():
        spine.set_color(GRID)
    ax_oi.grid(True, color=GRID, linewidth=0.4, alpha=0.4)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, facecolor=BG, dpi=130)
    plt.close(fig)
    log.info("wrote %s", output_path)

    return {
        "mark": mark,
        "lookback_hours": lookback_hours,
        "range_pct": range_pct,
        "n_vol_buckets": n_trade_buckets,
        "n_liqs": len(liqs),
        "total_vol_buy_btc": total_vol_buy,
        "total_vol_sell_btc": total_vol_sell,
        "total_liq_short_btc": total_liq_short,
        "total_liq_long_btc": total_liq_long,
        "oi_total_btc": oi_latest,
        "oi_change_pct_24h": oi_change_pct,
        "generated_at": now.isoformat(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("images/btc_profile.png"))
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument("--range-pct", type=float, default=3.0)
    parser.add_argument("--bins", type=int, default=80)
    args = parser.parse_args()

    setup_logging()
    # Load .env so COINALYZE_API_KEY etc. are available when run standalone
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env")
    except ImportError:
        pass
    meta = build_chart(
        args.output.resolve(),
        lookback_hours=args.lookback_hours,
        range_pct=args.range_pct,
        n_bins=args.bins,
    )
    log.info("done: %s", meta)
    return 0


if __name__ == "__main__":
    sys.exit(main())
