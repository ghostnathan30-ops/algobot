"""
AlgoBot -- GC (Gold) Mean Reversion Backtest
=============================================
Script:  scripts/run_gc_backtest.py
Phase:   Sub-Bot A -- GC Mean Reversion
Purpose: Full GC mean-reversion backtest using FHB signal inversion.

Strategy summary:
  - Detects 9:30-10:30 AM ET first-hour range on GC (Gold)
  - Inverts FHB breakout signal: breakout UP -> go SHORT (fade); DOWN -> go LONG
  - HTF filter: skip fade if HTF strongly confirms the breakout direction
  - Economic filter: skip HIGH + MEDIUM impact days (CPI/PPI/PCE move gold)
  - VIX filter: skip QUIET (<13) and CRISIS (>35) days
  - Stop: ATR-based, placed beyond breakout extreme
  - Target: VWAP or range midpoint (not fixed R)
  - Max hold: 3 hourly bars (no overnight carry)

Success criteria:
  Min PF: 1.30 | Min Win%: 55% | Max DD: < -$8,000

Run:
    cd AlgoBot
    conda run -n algobot_env python scripts/run_gc_backtest.py
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
from src.strategy.gc_signal import compute_gc_signals, simulate_gc_trades

log = get_logger(__name__)

MARKET = "GC"


# ============================================================
# HELPERS
# ============================================================

def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def download_1h_intraday(market: str) -> pd.DataFrame:
    """Download up to 730 days of 1-hour RTH data from Yahoo Finance."""
    import time as _time
    import yfinance as yf

    yf_tickers = {
        "GC":  "GC=F",
        "ES":  "ES=F",
        "NQ":  "NQ=F",
        "CL":  "CL=F",
        "6E":  "6E=F",
    }
    if market not in yf_tickers:
        return pd.DataFrame()

    ticker     = yf_tickers[market]
    cache_dir  = PROJECT_ROOT / "data" / "raw" / "intraday"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"yf_{market}_1h_730d.parquet"

    if cache_file.exists():
        age_h = (_time.time() - cache_file.stat().st_mtime) / 3600.0
        if age_h < 4.0:
            print(f"  {market}: Loading 1h data from cache")
            return pd.read_parquet(cache_file)

    print(f"  {market}: Downloading 730 days of 1-hour data...")
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
        # GC trades 23:00-17:30 ET — restrict to 9:00-17:00 for intraday analysis
        df = df.between_time("09:00", "17:00")
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
    """Compute HTF combined bias and daily regime from ~7 years of daily data."""
    print(f"  HTF data for {market}...")
    raw = download_market(market, "2019-01-01", "2025-12-31")
    if raw is None or raw.empty:
        return pd.Series(dtype=str), pd.Series(dtype=str)

    strat_cfg  = config.get("strategy", config)
    regime_cfg = config.get("regime", strat_cfg)
    df = calculate_indicators(raw, strat_cfg, market)
    df = add_atr_baseline(df)
    df = classify_regimes(df, regime_cfg, market)
    df = add_htf_bias(df, config, market)

    bias = df["htf_combined_bias"].copy()
    bias.index = pd.to_datetime(bias.index).normalize()

    regime = df["regime"].copy()
    regime.index = pd.to_datetime(regime.index).normalize()

    return bias, regime


# ============================================================
# METRICS
# ============================================================

def compute_metrics(trades: list[dict], market: str, config: dict) -> dict:
    """Compute standard backtest metrics from trade list."""
    if not trades:
        return {}

    markets_cfg = config.get("markets", {})
    mkt_cfg     = markets_cfg.get(market, {})
    point_value = float(mkt_cfg.get("point_value", 100.0))

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

    # Equity curve + drawdown
    equity    = 150_000.0
    peak      = equity
    max_dd    = 0.0
    eq_curve  = []
    for p in pnls:
        equity += p
        peak    = max(peak, equity)
        dd      = equity - peak
        if dd < max_dd:
            max_dd = dd
        eq_curve.append(equity)

    # Annual returns
    annual: dict[str, float] = {}
    for t in trades:
        yr = str(t["date"])[:4]
        annual[yr] = annual.get(yr, 0.0) + t["pnl_net"]

    # Exit reason breakdown
    exit_reasons: dict[str, int] = {}
    for t in trades:
        r = t.get("exit_reason", "unknown")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    return {
        "n_trades":    n,
        "win_rate":    round(win_rate, 1),
        "profit_factor": round(pf, 3),
        "total_pnl":   round(total, 2),
        "avg_win":     round(avg_win, 2),
        "avg_loss":    round(avg_loss, 2),
        "wl_ratio":    round(wl_ratio, 3),
        "max_drawdown": round(max_dd, 2),
        "final_equity": round(eq_curve[-1], 2) if eq_curve else 150_000.0,
        "annual":      {k: round(v, 2) for k, v in sorted(annual.items())},
        "exit_reasons": exit_reasons,
    }


def print_results(metrics: dict, trades: list[dict]) -> None:
    """Print formatted backtest results."""
    print("\n" + "=" * 65)
    print(f"  GC Mean Reversion Backtest Results")
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

    # Success criteria check
    print("\n  Success Criteria:")
    criteria = [
        ("Min PF >= 1.30",   pf >= 1.30,   f"PF={pf:.3f}"),
        ("Min Win% >= 55%",  wr >= 55.0,   f"Win%={wr:.1f}%"),
        ("Max DD < -$8,000", dd > -8_000,  f"DD=${dd:,.0f}"),
    ]
    all_pass = True
    for label, passed, value in criteria:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"    [{status}] {label} ({value})")

    print(f"\n  Overall: {'PASS — proceed to Phase B' if all_pass else 'FAIL — tune parameters'}")

    # Exit reason breakdown
    er = metrics.get("exit_reasons", {})
    if er:
        print("\n  Exit Reasons:")
        for reason, count in sorted(er.items(), key=lambda x: -x[1]):
            pct = count / n * 100 if n > 0 else 0
            print(f"    {reason:20s}: {count:4d} ({pct:.1f}%)")

    # Annual P&L
    annual = metrics.get("annual", {})
    if annual:
        print("\n  Annual P&L:")
        for yr, pnl in sorted(annual.items()):
            marker = " <--" if pnl < 0 else ""
            print(f"    {yr}: ${pnl:>10,.0f}{marker}")

    # Direction breakdown
    longs  = [t for t in trades if t["direction"] == "LONG"]
    shorts = [t for t in trades if t["direction"] == "SHORT"]
    if longs or shorts:
        print("\n  Direction Breakdown:")
        for grp, name in [(longs, "LONG (fade down)"), (shorts, "SHORT (fade up)")]:
            if grp:
                gw  = sum(1 for t in grp if t["pnl_net"] > 0)
                gpl = sum(t["pnl_net"] for t in grp)
                gwr = gw / len(grp) * 100
                print(f"    {name:22s}: {len(grp):4d} trades | "
                      f"Win%={gwr:.1f}% | P&L=${gpl:,.0f}")

    print("=" * 65 + "\n")


# ============================================================
# MAIN
# ============================================================

def main():
    print("\n" + "=" * 65)
    print("  AlgoBot -- GC Mean Reversion Backtest")
    print("=" * 65)

    config = load_config()

    # ── Step 1: Download data ──────────────────────────────────────────────────
    print(f"\n[1/4] Downloading {MARKET} 1-hour data...")
    df_1h = download_1h_intraday(MARKET)
    if df_1h.empty:
        print(f"ERROR: No 1h data for {MARKET}. Check internet connection.")
        return

    # ── Step 2: HTF bias ───────────────────────────────────────────────────────
    print(f"\n[2/4] Computing HTF bias + regime for {MARKET}...")
    htf_bias_series, regime_series = get_htf_data(MARKET, config)

    # ── Step 3: Filters ────────────────────────────────────────────────────────
    print("\n[3/4] Loading filters...")
    econ_cal   = EconCalendar()
    vix_filter = VIXFilter.from_yahoo(start="2019-01-01", end="2026-12-31")
    ec_counts  = econ_cal.total_events()
    print(f"  EconCalendar: {ec_counts['high']} HIGH, {ec_counts['medium']} MEDIUM events")

    # ── Step 4: Signal + simulation ────────────────────────────────────────────
    print(f"\n[4/4] Running GC mean-reversion signals + simulation...")
    df_sig = compute_gc_signals(
        df_1h, MARKET, htf_bias_series, regime_series, config,
        econ_cal=econ_cal, vix_filter=vix_filter,
    )

    n_long  = int(df_sig["gc_long_signal"].sum())
    n_short = int(df_sig["gc_short_signal"].sum())
    n_block = int(df_sig["gc_htf_blocked"].sum())
    print(f"  Signals: {n_long} LONG-fade, {n_short} SHORT-fade, {n_block} HTF-blocked")

    trades = simulate_gc_trades(df_sig, MARKET, config)
    print(f"  Trades generated: {len(trades)}")

    # ── Metrics + results ──────────────────────────────────────────────────────
    metrics = compute_metrics(trades, MARKET, config)
    print_results(metrics, trades)

    # ── Save trades to CSV ─────────────────────────────────────────────────────
    if trades:
        out_dir  = PROJECT_ROOT / "reports" / "backtests"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "gc_reversion_trades.csv"
        pd.DataFrame(trades).to_csv(out_file, index=False)
        print(f"  Trades saved to: {out_file}")


if __name__ == "__main__":
    main()
