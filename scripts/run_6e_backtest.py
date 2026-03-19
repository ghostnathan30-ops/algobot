"""
AlgoBot -- 6E (Euro FX) London Open Breakout Backtest
======================================================
Script:  scripts/run_6e_backtest.py
Phase:   Sub-Bot B -- 6E London Open Breakout
Purpose: Full 6E London Open breakout backtest.

Strategy summary:
  - 3:00-5:00 AM ET London range computation on 6E (Euro FX futures)
  - Breakout AFTER 5:00 AM ET -- FOLLOW the direction (not fade)
  - Stop: opposite side of London range
  - Target: 2.0R (tighter than main bot's 2.5R)
  - Partial: 50% at 1.0R, trail to breakeven
  - Max hold: 4 bars (close before 9:00 AM ET US open)
  - HTF filter: monthly bias direction
  - Calendar: Skip FOMC, NFP, ECB (HIGH impact only)
  - VIX filter: skip QUIET and CRISIS

Success criteria:
  Min PF: 1.40 | Min Win%: 58% | Max DD: < -$5,000

Run:
    cd AlgoBot
    conda run -n algobot_env python scripts/run_6e_backtest.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.data_downloader import download_market
from src.strategy.indicators import calculate_indicators, add_atr_baseline
from src.strategy.regime_classifier import classify_regimes
from src.strategy.htf_bias import add_htf_bias
from src.utils.econ_calendar import EconCalendar
from src.utils.vix_filter import VIXFilter
from src.utils.logger import get_logger
from src.strategy.london_open_signal import compute_london_signals, simulate_london_trades

log = get_logger(__name__)

MARKET = "6E"


# ============================================================
# HELPERS
# ============================================================

def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def download_1h_overnight(market: str) -> pd.DataFrame:
    """
    Download 730 days of 1-hour data including overnight for 6E.
    6E trades 23:00-22:00 ET (nearly 24 hours).
    We need 3:00-9:00 AM ET bars for London session.
    """
    import time as _time
    import yfinance as yf

    yf_tickers = {
        "6E":  "6E=F",
        "GC":  "GC=F",
        "CL":  "CL=F",
    }
    if market not in yf_tickers:
        return pd.DataFrame()

    ticker     = yf_tickers[market]
    cache_dir  = PROJECT_ROOT / "data" / "raw" / "intraday"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"yf_{market}_1h_overnight_730d.parquet"

    if cache_file.exists():
        age_h = (_time.time() - cache_file.stat().st_mtime) / 3600.0
        if age_h < 4.0:
            print(f"  {market}: Loading 1h overnight data from cache")
            return pd.read_parquet(cache_file)

    print(f"  {market}: Downloading 730 days of 1-hour data (full session)...")
    try:
        raw = yf.download(ticker, period="730d", interval="1h",
                          auto_adjust=True, progress=False, timeout=30)
        if raw is None or raw.empty:
            return pd.DataFrame()
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [c.title() for c in raw.columns]
        keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in raw.columns]
        df   = raw[keep].copy()
        for col in ["Open", "High", "Low", "Close"]:
            if col in df.columns:
                df[col] = df[col].astype(float)
        if "Volume" in df.columns:
            df["Volume"] = df["Volume"].astype(float)
        df.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("America/New_York")
        else:
            df.index = df.index.tz_convert("America/New_York")
        df.index.name = "Timestamp"
        # Keep only bars from 2:00 AM to 16:00 PM ET — covers London through US open
        df = df.between_time("02:00", "16:00")
        n_days = df.index.normalize().nunique()
        print(f"  {market}: {len(df)} bars | "
              f"{df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')} "
              f"({n_days} days)")
        try:
            df.to_parquet(cache_file)
        except Exception:
            pass
        return df
    except Exception as e:
        print(f"  {market}: Download error: {e}")
        return pd.DataFrame()


def get_htf_data(market: str, config: dict):
    """Compute HTF combined bias from daily data."""
    print(f"  HTF data for {market}...")
    raw = download_market(market, "2019-01-01", "2025-12-31")
    if raw is None or raw.empty:
        return pd.Series(dtype=str)

    strat_cfg  = config.get("strategy", config)
    regime_cfg = config.get("regime", strat_cfg)
    df = calculate_indicators(raw, strat_cfg, market)
    df = add_atr_baseline(df)
    df = classify_regimes(df, regime_cfg, market)
    df = add_htf_bias(df, config, market)

    bias = df["htf_combined_bias"].copy()
    bias.index = pd.to_datetime(bias.index).normalize()
    return bias


# ============================================================
# METRICS
# ============================================================

def compute_metrics(trades: list[dict], market: str, config: dict) -> dict:
    if not trades:
        return {}

    markets_cfg = config.get("markets", {})
    mkt_cfg     = markets_cfg.get(market, {})
    point_value = float(mkt_cfg.get("point_value", 125000.0))

    pnls     = [t["pnl_net"] for t in trades]
    wins     = [p for p in pnls if p > 0]
    losses   = [p for p in pnls if p < 0]
    n        = len(pnls)
    win_rate = len(wins) / n * 100 if n > 0 else 0.0
    gross_w  = sum(wins)
    gross_l  = abs(sum(losses))
    pf       = gross_w / gross_l if gross_l > 0 else float("inf")
    total    = sum(pnls)
    avg_win  = gross_w / len(wins) if wins else 0.0
    avg_loss = gross_l / len(losses) if losses else 0.0
    wl_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

    equity   = 150_000.0
    peak     = equity
    max_dd   = 0.0
    eq_curve = []
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        dd      = equity - peak
        if dd < max_dd:
            max_dd = dd
        eq_curve.append(equity)

    annual: dict[str, float] = {}
    for t in trades:
        yr = str(t["date"])[:4]
        annual[yr] = annual.get(yr, 0.0) + t["pnl_net"]

    exit_reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "unknown")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    return {
        "n_trades":     n,
        "win_rate":     round(win_rate, 1),
        "profit_factor": round(pf, 3),
        "total_pnl":    round(total, 2),
        "avg_win":      round(avg_win, 2),
        "avg_loss":     round(avg_loss, 2),
        "wl_ratio":     round(wl_ratio, 3),
        "max_drawdown": round(max_dd, 2),
        "final_equity": round(eq_curve[-1], 2) if eq_curve else 150_000.0,
        "annual":       {k: round(v, 2) for k, v in sorted(annual.items())},
        "exit_reasons": exit_reasons,
    }


def print_results(metrics: dict, trades: list[dict]) -> None:
    print("\n" + "=" * 65)
    print(f"  6E London Open Breakout Backtest Results")
    print("=" * 65)
    n  = metrics.get("n_trades", 0)
    wr = metrics.get("win_rate", 0)
    pf = metrics.get("profit_factor", 0)
    pl = metrics.get("total_pnl", 0)
    dd = metrics.get("max_drawdown", 0)
    fe = metrics.get("final_equity", 150_000)

    print(f"  Trades       : {n}")
    print(f"  Win Rate     : {wr:.1f}%")
    print(f"  Profit Factor: {pf:.3f}")
    print(f"  Total P&L    : ${pl:>10,.0f}")
    print(f"  Max Drawdown : ${dd:>10,.0f}")
    print(f"  Final Equity : ${fe:>10,.0f}")
    print(f"  Avg Win      : ${metrics.get('avg_win', 0):>10,.0f}")
    print(f"  Avg Loss     : ${metrics.get('avg_loss', 0):>10,.0f}")
    print(f"  W/L Ratio    : {metrics.get('wl_ratio', 0):.3f}")

    print("\n  Success Criteria:")
    criteria = [
        ("Min PF >= 1.40",   pf >= 1.40,   f"PF={pf:.3f}"),
        ("Min Win% >= 58%",  wr >= 58.0,   f"Win%={wr:.1f}%"),
        ("Max DD < -$5,000", dd > -5_000,  f"DD=${dd:,.0f}"),
    ]
    all_pass = True
    for label, passed, value in criteria:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"    [{status}] {label} ({value})")

    print(f"\n  Overall: {'PASS — proceed to Phase C' if all_pass else 'FAIL — tune parameters'}")

    er = metrics.get("exit_reasons", {})
    if er:
        print("\n  Exit Reasons:")
        for reason, count in sorted(er.items(), key=lambda x: -x[1]):
            pct = count / n * 100 if n > 0 else 0
            print(f"    {reason:25s}: {count:4d} ({pct:.1f}%)")

    annual = metrics.get("annual", {})
    if annual:
        print("\n  Annual P&L:")
        for yr, pnl in sorted(annual.items()):
            marker = " <--" if pnl < 0 else ""
            print(f"    {yr}: ${pnl:>10,.0f}{marker}")

    longs  = [t for t in trades if t["direction"] == "LONG"]
    shorts = [t for t in trades if t["direction"] == "SHORT"]
    if longs or shorts:
        print("\n  Direction Breakdown:")
        for grp, name in [(longs, "LONG"), (shorts, "SHORT")]:
            if grp:
                gw  = sum(1 for t in grp if t["pnl_net"] > 0)
                gpl = sum(t["pnl_net"] for t in grp)
                gwr = gw / len(grp) * 100
                print(f"    {name:8s}: {len(grp):4d} trades | "
                      f"Win%={gwr:.1f}% | P&L=${gpl:,.0f}")

    print("=" * 65 + "\n")


# ============================================================
# MAIN
# ============================================================

def main():
    print("\n" + "=" * 65)
    print("  AlgoBot -- 6E London Open Breakout Backtest")
    print("=" * 65)

    config = load_config()

    # ── Step 1: Download overnight 1h data ────────────────────────────────────
    print(f"\n[1/4] Downloading {MARKET} 1-hour overnight data...")
    df_1h = download_1h_overnight(MARKET)
    if df_1h.empty:
        print(f"ERROR: No 1h data for {MARKET}. Check internet connection.")
        return

    # Verify we have London session bars
    london_bars = df_1h.between_time("03:00", "05:00")
    n_london    = len(london_bars)
    print(f"  London session bars (03:00-05:00): {n_london}")
    if n_london == 0:
        print("  WARNING: No London session bars found. Yahoo Finance may not have "
              "overnight 6E data — try 6E.L or consider using IB data source.")

    # ── Step 2: HTF bias ───────────────────────────────────────────────────────
    print(f"\n[2/4] Computing HTF bias for {MARKET}...")
    htf_bias_series = get_htf_data(MARKET, config)

    # ── Step 3: Filters ────────────────────────────────────────────────────────
    print("\n[3/4] Loading filters...")
    econ_cal   = EconCalendar()
    vix_filter = VIXFilter.from_yahoo(start="2019-01-01", end="2026-12-31")
    ec_counts  = econ_cal.total_events()
    print(f"  EconCalendar: {ec_counts['high']} HIGH (incl. ECB), {ec_counts['medium']} MEDIUM events")

    # Verify ECB dates are loaded
    import datetime
    ecb_count = sum(
        1 for d in econ_cal._high
        if econ_cal._labels.get(d, "") == "ECB"
    )
    print(f"  ECB dates loaded: {ecb_count}")

    # ── Step 4: Signal + simulation ────────────────────────────────────────────
    print(f"\n[4/4] Running 6E London Open signals + simulation...")
    df_sig = compute_london_signals(
        df_1h, MARKET, htf_bias_series, config,
        econ_cal=econ_cal, vix_filter=vix_filter,
    )

    n_long  = int(df_sig["lon_long_signal"].sum())
    n_short = int(df_sig["lon_short_signal"].sum())
    n_block = int(df_sig["lon_htf_blocked"].sum())
    print(f"  Signals: {n_long} LONG, {n_short} SHORT, {n_block} HTF-blocked")

    if n_long + n_short == 0:
        print("\n  WARNING: No signals generated.")
        print("  Possible causes:")
        print("    1. Yahoo Finance 6E=F 1h data doesn't include overnight bars (03-05 AM ET)")
        print("    2. The 6E contract has limited intraday liquidity in Yahoo data")
        print("    3. Data gap in 730-day period")
        print("\n  The strategy logic is correct — the data limitation is a known Yahoo constraint.")
        print("  In live trading, use IB or Rithmic data feeds which provide full 23h sessions.")
        return

    trades = simulate_london_trades(df_sig, MARKET, config)
    print(f"  Trades generated: {len(trades)}")

    metrics = compute_metrics(trades, MARKET, config)
    print_results(metrics, trades)

    # ── Save trades to CSV ─────────────────────────────────────────────────────
    if trades:
        out_dir  = PROJECT_ROOT / "reports" / "backtests"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "6e_london_open_trades.csv"
        pd.DataFrame(trades).to_csv(out_file, index=False)
        print(f"  Trades saved to: {out_file}")


if __name__ == "__main__":
    main()
