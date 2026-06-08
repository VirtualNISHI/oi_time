"""Build a TradingView-style BTC Volume Profile + Liquidation chart PNG.

Single-panel design (no OI sub-panel — that data lives in the post text).
Aesthetic reference: TradingView dark theme + standalone volume profile.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from exchange_clients import (  # noqa: E402
    aggregate_volume_profile,
    compute_oi_delta_profile,
    fetch_aggregated_oi,
    fetch_all_liquidations,
    get_mark_price,
)

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# ---- Palette: dark theme, long-vs-short color split --------------------------
# Long-favorable (right side) = green family
# Short-favorable (left side) = red family
# Layer is conveyed within each family by saturation (Trade=vivid, REKT=bright
# accent, OI delta=light hatched).
BG       = "#131722"   # dark canvas (X / TV dark theme)
FG       = "#D1D4DC"   # primary text  (near-white)
DIM      = "#787B86"   # secondary text / ticks
GRID     = "#2A2E39"   # grid / spines (subtle on dark)
MARK     = "#FFF176"   # mark price line — light yellow (pops on dark, neutral vs long/short)
POC      = "#FFFFFF"   # POC marker / current-price label header — white on dark

# Long side (drawn on the right)
C_BUY    = "#26A69A"   # Trade 買い      — teal-green (solid)
C_LIQ_S  = "#00E676"   # REKT ショート   — bright lime (long-favor accent)
C_OI_UP  = "#66BB6A"   # 推定OI 新規    — green (hatched, a bit brighter for dark bg)

# Short side (drawn on the left)
C_SELL   = "#EF5350"   # Trade 売り      — red (solid)
C_LIQ_L  = "#FF1744"   # REKT ロング     — bright red (short-favor accent)
C_OI_DN  = "#EF9A9A"   # 推定OI 解消    — rose-pink (hatched, visible on dark bg)

BAR_ALPHA = 0.92
OI_ALPHA  = 0.75


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


def _japanese_font() -> str:
    """First-available Japanese font, fall back to DejaVu Sans."""
    from matplotlib import font_manager
    candidates = [
        "Noto Sans CJK JP", "Noto Sans JP",
        "Yu Gothic UI", "Yu Gothic", "Meiryo", "MS Gothic",
        "Hiragino Sans", "IPAexGothic",
        "DejaVu Sans",
    ]
    available = {f.name for f in font_manager.fontManager.ttflist}
    for c in candidates:
        if c in available:
            return c
    return "DejaVu Sans"


def _fmt_btc(x: float) -> str:
    """Compact axis tick formatter: 1234 → '1.2K', 23000 → '23K'."""
    ax = abs(x)
    if ax >= 1000:
        return f"{x / 1000:,.1f}K"
    return f"{x:,.0f}"


def _pick_interval_min(hours: float) -> int:
    """Auto-pick a Coinalyze kline interval that keeps the per-call count
    reasonable (< ~700 candles) while preserving fidelity for the window.

    Coinalyze allowed intervals (in minutes): 1, 5, 15, 30, 60, 120, 240.
    """
    if hours <= 24:
        return 5     # 288 candles
    if hours <= 72:
        return 15    # 288 candles
    if hours <= 168:
        return 30    # 336 candles  (7d)
    return 60        # ≥7d, 1h candles


def _compute_oi_summary(hours: float, since_ms: int) -> tuple[float | None, float | None]:
    """Return (latest_total_oi_btc, pct_change_over_window).

    Returns (None, None) when the data is unavailable. Callers must format
    these distinctly from a real zero so users don't see "OI 0 BTC" on
    transient API failures (Codex review #1).
    """
    try:
        oi_data = fetch_aggregated_oi(hours=hours, period="1h")
    except Exception as e:
        log.warning("OI fetch failed (non-fatal, no chart impact): %s", e)
        return None, None
    totals: dict[int, float] = {}
    for pts in oi_data.values():
        for p in pts:
            if p.ts_ms >= since_ms:
                totals[p.ts_ms] = totals.get(p.ts_ms, 0.0) + p.oi_btc
    if not totals:
        return None, None
    series = [totals[t] for t in sorted(totals)]
    latest = series[-1]
    pct = (series[-1] - series[0]) / series[0] * 100 if series[0] > 0 else 0.0
    return latest, pct


def build_chart(
    output_path: Path,
    lookback_hours: float = 24.0,
    range_pct: float = 3.0,
    range_pct_up: float | None = None,
    range_pct_down: float | None = None,
    n_bins: int = 150,
) -> dict:
    """Generate the chart and write to output_path. Returns a metadata dict.

    range_pct      : symmetric % range around mark (legacy single-value API).
    range_pct_up   : upper-side % (overrides range_pct for the high bound).
    range_pct_down : lower-side % (overrides range_pct for the low bound).
    """

    # Resolve range with backward-compat fallback.
    up = range_pct_up if range_pct_up is not None else range_pct
    down = range_pct_down if range_pct_down is not None else range_pct
    asymmetric = abs(up - down) > 0.01
    # Skip auto-fit when the caller explicitly set either side — the user
    # picked a window on purpose, don't zoom them back into the tight view.
    explicit_range = (range_pct_up is not None) or (range_pct_down is not None)

    now = datetime.now(timezone.utc)
    since_ms = int((now - timedelta(hours=lookback_hours)).timestamp() * 1000)

    mark = get_mark_price()
    log.info("mark price: $%.2f  range -%.1f%% .. +%.1f%%", mark, down, up)

    p_low = mark * (1 - down / 100)
    p_high = mark * (1 + up / 100)
    bins = np.linspace(p_low, p_high, n_bins + 1)
    centers = (bins[:-1] + bins[1:]) / 2

    # ---- Volume profile (interval auto-picked from lookback) ----
    interval_min = _pick_interval_min(lookback_hours)
    log.info("kline interval: %dmin (for %.1fh lookback)", interval_min, lookback_hours)
    buckets = aggregate_volume_profile(hours=lookback_hours, interval_min=interval_min)
    buckets = [b for b in buckets if b.ts_ms >= since_ms and p_low <= b.price <= p_high]
    log.info("vol buckets in window+range: %d", len(buckets))

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

    liq_short = np.zeros(n_bins)  # short liquidated → buys at that price
    liq_long = np.zeros(n_bins)   # long  liquidated → sells at that price
    _liq_unknown = 0
    for l in liqs:
        idx = np.searchsorted(bins, l.price, side="right") - 1
        if 0 <= idx < n_bins:
            if l.side == "short":
                liq_short[idx] += l.qty_btc
            elif l.side == "long":
                liq_long[idx] += l.qty_btc
            else:
                # Unexpected side — log explicitly rather than silently misbucket (Codex review #2)
                _liq_unknown += 1
    if _liq_unknown:
        log.warning("dropped %d liquidations with unknown side", _liq_unknown)

    total_vol_buy = float(vol_buy.sum())
    total_vol_sell = float(vol_sell.sum())
    total_liq_short = float(liq_short.sum())
    total_liq_long = float(liq_long.sum())

    # ---- OI delta profile (M2 — heuristic estimate) ----
    try:
        oi_delta = compute_oi_delta_profile(hours=lookback_hours, fine_interval_min=interval_min)
    except Exception as e:
        log.warning("OI delta profile failed (non-fatal): %s", e)
        oi_delta = []
    oi_delta = [d for d in oi_delta if p_low <= d.price <= p_high]
    oi_up = np.zeros(n_bins)   # est. new OI (dOI > 0)
    oi_dn = np.zeros(n_bins)   # est. closed/liquidated OI (dOI < 0, stored as positive)
    for d in oi_delta:
        idx = np.searchsorted(bins, d.price, side="right") - 1
        if 0 <= idx < n_bins:
            if d.oi_delta_btc > 0:
                oi_up[idx] += d.oi_delta_btc
            else:
                oi_dn[idx] += -d.oi_delta_btc
    total_oi_up = float(oi_up.sum())
    total_oi_dn = float(oi_dn.sum())

    # ---- OI summary (text-only — never plotted) ----
    oi_total, oi_chg = _compute_oi_summary(lookback_hours, since_ms)

    # ---- Plot ----
    plt.rcParams.update({
        "font.family": _japanese_font(),
        "font.size": 10,
        "axes.unicode_minus": False,
    })

    fig = plt.figure(figsize=(8.8, 11.0), dpi=130)
    fig.patch.set_facecolor(BG)
    # Leave ~12% right margin so Mark / POC labels don't clip.
    # Top trimmed to make room for in-figure legend strip.
    ax = fig.add_axes([0.10, 0.07, 0.76, 0.78])
    ax.set_facecolor(BG)  # no contrast box — flat canvas like TV

    bar_h = (bins[1] - bins[0]) * 0.55  # slim bars, original-image style

    # ---- OI delta layer (estimated) — drawn first / behind, hatched ----
    # Scale: the dOI magnitudes are typically much smaller than trade volume
    # (a few hundred BTC vs tens of thousands). Show on a separate scale by
    # placing as a thin outer fringe BEYOND the vol bars in axes-fraction.
    # Approach: use a half-height bar to the OUTSIDE of buy/sell volume.
    oi_bar_h = bar_h * 0.55
    oi_offset_y = bar_h * 0.0   # no offset — keep slim bars aligned at the bin center
    if oi_up.max() > 0 or oi_dn.max() > 0:
        # Normalize OI delta to ~30% of the volume xlim for visual readability
        vol_max = max(float((vol_buy + liq_short).max() if len(vol_buy) else 1),
                      float((vol_sell + liq_long).max() if len(vol_sell) else 1), 1.0)
        oi_max = max(oi_up.max(), oi_dn.max(), 1e-9)
        oi_scale = (vol_max * 0.30) / oi_max
        # dOI > 0 (new OI) — pale green, hatched, RIGHT side
        ax.barh(centers + oi_offset_y, oi_up * oi_scale,
                height=oi_bar_h, left=vol_buy,
                color=C_OI_UP, alpha=OI_ALPHA, hatch="///",
                edgecolor=C_OI_UP, linewidth=0.0,
                label="推定OI 新規 (+)")
        # dOI < 0 (closed OI) — pale rose, hatched, LEFT side
        ax.barh(centers + oi_offset_y, -oi_dn * oi_scale,
                height=oi_bar_h, left=-vol_sell,
                color=C_OI_DN, alpha=OI_ALPHA, hatch="\\\\\\",
                edgecolor=C_OI_DN, linewidth=0.0,
                label="推定OI 解消 (−)")

    # Volume profile — sells left, buys right
    ax.barh(centers,  vol_buy,  height=bar_h, color=C_BUY,  alpha=BAR_ALPHA,
            edgecolor="none", label="Trade 買い")
    ax.barh(centers, -vol_sell, height=bar_h, color=C_SELL, alpha=BAR_ALPHA,
            edgecolor="none", label="Trade 売り")

    # Liquidations — inner marker thin bars overlaid on top of vol bars
    inner_h = bar_h * 0.55
    ax.barh(centers,  liq_short, height=inner_h, left=vol_buy,
            color=C_LIQ_S, alpha=0.95, edgecolor="none", label="REKT ショート")
    ax.barh(centers, -liq_long,  height=inner_h, left=-vol_sell,
            color=C_LIQ_L, alpha=0.95, edgecolor="none", label="REKT ロング")

    # ---- Mark price line (thick solid yellow) ----
    ax.axhline(mark, color=MARK, linewidth=2.4, linestyle="-", alpha=1.0,
               zorder=5)

    # ---- POC (Point of Control = bin with highest total volume) ----
    total_vol_per_bin = vol_buy + vol_sell
    if total_vol_per_bin.max() > 0:
        poc_idx = int(np.argmax(total_vol_per_bin))
        poc_price = float(centers[poc_idx])
        # Subtle marker line on the right side
        ax.plot([0], [poc_price], marker="<", markersize=7,
                color=POC, alpha=0.85)

    # ---- Center vertical line ----
    ax.axvline(0, color=GRID, linewidth=0.8)

    # ---- Y limits ----
    # Crop to where OI delta has been detected (primary), with fallback to
    # Vol+REKT activity if OI data is absent, and full configured range as
    # the last resort. User request: only show the band where OI is detected.
    oi_activity = oi_up + oi_dn
    nonzero = np.where(oi_activity > 0)[0]
    if not len(nonzero):
        # Fallback: any trade or liq activity
        fallback = total_vol_per_bin + liq_short + liq_long
        nonzero = np.where(fallback > 0)[0]
    if len(nonzero):
        data_lo = float(centers[nonzero[0]])
        data_hi = float(centers[nonzero[-1]])
        span = max(data_hi - data_lo, mark * 0.005)
        pad = span * 0.04
        # Always include mark price in the visible window
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
    ax.set_xlim(-max_abs * 1.05, max_abs * 1.05)

    # ---- Mark price label (just outside the data area, axes-relative) ----
    ax.annotate(
        f"{mark:,.0f}",
        xy=(1.0, mark), xycoords=("axes fraction", "data"),
        xytext=(6, 0), textcoords="offset points",
        color=MARK, fontsize=12, fontweight="bold", va="center", ha="left",
    )
    # ---- "現在BTC価格" header text — fixed offset just above the mark number ----
    # Stays attached to the mark line regardless of where POC lands, so the
    # 2-line "label + value" group never collides when the y-range stretches
    # (e.g. asymmetric +10/-3 trial).
    ax.annotate(
        "現在BTC価格",
        xy=(1.0, mark), xycoords=("axes fraction", "data"),
        xytext=(6, 14), textcoords="offset points",
        color=POC, fontsize=9, alpha=0.85, va="bottom", ha="left",
    )

    # ---- Minimal grid: horizontal lines only, very faint ----
    ax.grid(True, axis="y", color=GRID, linewidth=0.6, alpha=0.7)
    ax.grid(False, axis="x")

    # ---- Spines: remove top/right, keep bottom/left thin ----
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("bottom", "left"):
        ax.spines[side].set_color(GRID)
        ax.spines[side].set_linewidth(0.8)

    # ---- Ticks ----
    ax.tick_params(colors=DIM, labelsize=9, length=0)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _p: f"{y:,.0f}"))
    ax.xaxis.set_major_formatter(plt.FuncFormatter(lambda x, _p: _fmt_btc(x)))

    # No axis labels (the title carries the meaning, TV doesn't label volume axis)
    ax.set_xlabel("")
    ax.set_ylabel("")

    # ---- Title block (top-left, two lines) ----
    now_jst = now.astimezone(JST)
    # Window label: days when ≥24h, otherwise hours
    if lookback_hours >= 24 and lookback_hours % 24 == 0:
        window_label = f"Last {int(lookback_hours / 24)}d"
    else:
        window_label = f"Last {int(lookback_hours)}h"
    fig.text(
        0.10, 0.955, "BTC価格帯別OIマップ",
        color=FG, fontsize=14, fontweight="bold", ha="left",
    )
    fig.text(
        0.10, 0.932,
        f"{window_label}  ·  Binance + Bybit + OKX (via Coinalyze)  ·  "
        f"{now_jst.strftime('%Y-%m-%d %H:%M')} JST",
        color=DIM, fontsize=9, ha="left",
    )
    fig.text(
        0.10, 0.913,
        "Hatched layers = estimated OI delta (dOI distributed across price by intra-hour volume share).",
        color=DIM, fontsize=8, ha="left", style="italic",
    )

    # ---- Stats (top-right corner) ----
    oi_total_str = f"{oi_total:>10,.0f} BTC" if oi_total is not None else "         n/a"
    oi_chg_str = (
        f"{('+' if oi_chg >= 0 else '')}{oi_chg:>5.2f}%"
        if oi_chg is not None else "    n/a"
    )
    oi_window_label = f"Δ{int(lookback_hours)}h" if lookback_hours != 24 else "Δ24h"
    stats_lines = [
        f"Mark      {mark:>10,.0f}",
        f"OI        {oi_total_str}",
        f"OI {oi_window_label:<6}{oi_chg_str:>11}",
    ]
    fig.text(
        0.96, 0.944, "\n".join(stats_lines),
        color=FG, fontsize=9, ha="right", va="top",
        family="monospace",
    )

    # ---- Footer: volume + liquidation + OI delta totals ----
    vb_pct = (total_vol_buy / (total_vol_buy + total_vol_sell) * 100
              if (total_vol_buy + total_vol_sell) > 0 else 50)
    footer = (
        f"Vol  {total_vol_buy:,.0f}b / {total_vol_sell:,.0f}s  ({vb_pct:.1f}% buy)   "
        f"Liq  {total_liq_short:,.0f}s / {total_liq_long:,.0f}l   "
        f"est.OI  +{total_oi_up:,.0f} / -{total_oi_dn:,.0f}"
    )
    fig.text(0.5, 0.025, footer, color=DIM, fontsize=9, ha="center", family="monospace")

    # ---- Legend: single row strip above the axes, in figure space ----
    # Placed between subtitle and chart top so it never overlaps bars.
    leg = ax.legend(
        loc="lower left",
        bbox_to_anchor=(0.0, 1.005),    # just above the axes
        bbox_transform=ax.transAxes,
        ncol=6, fontsize=8, frameon=False,
        labelcolor=DIM, handlelength=1.2, handletextpad=0.4, columnspacing=1.4,
    )
    for txt in leg.get_texts():
        txt.set_color(DIM)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, facecolor=BG, dpi=130)
    plt.close(fig)
    log.info("wrote %s", output_path)

    return {
        "mark": mark,
        "lookback_hours": lookback_hours,
        "range_pct": range_pct,
        "range_pct_up": up,
        "range_pct_down": down,
        "n_vol_buckets": len(buckets),
        "n_liqs": len(liqs),
        "total_vol_buy_btc": total_vol_buy,
        "total_vol_sell_btc": total_vol_sell,
        "total_liq_short_btc": total_liq_short,
        "total_liq_long_btc": total_liq_long,
        "oi_total_btc": oi_total,           # None if API unavailable
        "oi_change_pct_24h": oi_chg,        # None if API unavailable
        "oi_lookback_hours": lookback_hours,  # window the oi_change actually covers
        "est_oi_new_btc": total_oi_up,      # heuristic: positive dOI distributed by vol share
        "est_oi_close_btc": total_oi_dn,    # heuristic: negative dOI distributed by vol share
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
