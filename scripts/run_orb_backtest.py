"""
AlgoBot — ORB Backtest Script
================================
Script:  scripts/run_orb_backtest.py
Phase:   5B — ORB Proof-of-Concept Backtest
Purpose: Download 5-min ES and NQ data from Yahoo Finance, compute Opening
         Range Breakout signals with HTF bias filter, simulate all trades,
         and report full performance metrics.

Data source: Yahoo Finance free intraday — last ~60 days of 5-min bars.

IMPORTANT CONTEXT ON SAMPLE SIZE:
  60 trading days = ~60 potential ORB setups per market.
  This is a proof-of-concept window, not a definitive backtest.
  A robust backtest requires 3-5 years of intraday data.
  These results validate the MECHANISM, not the final profitability.
  If the strategy shows positive expectancy here, it is worth pursuing
  with a proper multi-year dataset.

Run:
    cd AlgoBot
    conda run -n algobot_env python scripts/run_orb_backtest.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

# ── Add project root to path ──────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.yf_intraday import download_all_intraday, summarize_intraday
from src.utils.data_downloader import download_market
from src.strategy.indicators import calculate_indicators, add_atr_baseline
from src.strategy.regime_classifier import classify_regimes
from src.strategy.htf_bias import add_htf_bias
from src.strategy.orb_signal import compute_orb_signals, summarize_orb_signals
from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Config ─────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


# ── HTF bias from daily data ───────────────────────────────────────────────────

def get_htf_bias_series(market: str, config: dict) -> pd.Series:
    """
    Download daily bars and compute HTF bias for each trading date.
    Returns a Series indexed by date with BULL/BEAR/NEUTRAL values.
    """
    print(f"  Downloading daily bars for {market} HTF bias...")

    raw = download_market(market, "2022-01-01", "2025-12-31")
    if raw is None or raw.empty:
        print(f"  WARNING: Could not download daily data for {market}. Using NEUTRAL bias.")
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


# ── Trade simulation ───────────────────────────────────────────────────────────

def simulate_orb_trades(
    df_5min: pd.DataFrame,
    market: str,
    config: dict,
) -> list[dict]:
    """
    Simulate all ORB trades in the 5-min DataFrame.

    For each signal bar:
      Entry:   Open of the NEXT bar (realistic — can't enter at signal close)
      Stop:    Opposite side of the opening range
      Target1: 1R (50% of position — partial exit)
      Target2: 2R (remaining 50%)
      Time:    Exit at close of bar 24 after entry (2-hour max hold)

    Returns list of trade dicts with: entry, stop, target, outcome, R_multiple.
    """
    orb_cfg    = config.get("intraday", {}).get("orb", {})
    partial_r  = float(orb_cfg.get("partial_exit_r",   1.0))
    full_r     = float(orb_cfg.get("profit_target_r",  2.0))
    partial_pct = float(orb_cfg.get("partial_exit_pct", 0.5))
    max_bars   = int(orb_cfg.get("max_hold_bars",      24))

    # Contract specs for dollar P&L calculation
    markets_cfg  = config.get("markets", {})
    point_value  = float(markets_cfg.get(market, {}).get("point_value",  50.0))
    commission   = float(markets_cfg.get(market, {}).get("commission",    5.0))
    slippage_tks = int(  markets_cfg.get(market, {}).get("slippage_ticks", 1))
    tick_size    = float(markets_cfg.get(market, {}).get("tick_size",    0.25))
    slippage_pts = slippage_tks * tick_size

    trades = []
    bars   = df_5min.reset_index()  # integer index for easy lookahead

    for i, row in bars.iterrows():
        is_long  = bool(row.get("orb_long_signal",  False))
        is_short = bool(row.get("orb_short_signal", False))
        blocked  = bool(row.get("orb_htf_blocked",  False))

        if not (is_long or is_short) or blocked:
            continue

        # Entry is on the OPEN of the next bar
        if i + 1 >= len(bars):
            continue

        next_bar   = bars.iloc[i + 1]
        entry_raw  = float(next_bar["Open"])
        range_high = float(row["orb_range_high"])
        range_low  = float(row["orb_range_low"])
        range_size = range_high - range_low

        if range_size <= 0:
            continue

        # Adjust entry for slippage
        if is_long:
            entry = entry_raw + slippage_pts
            stop  = range_low  - slippage_pts
        else:
            entry = entry_raw - slippage_pts
            stop  = range_high + slippage_pts

        risk_pts = abs(entry - stop)
        if risk_pts <= 0:
            continue

        target1 = entry + partial_r * risk_pts * (1 if is_long else -1)
        target2 = entry + full_r    * risk_pts * (1 if is_long else -1)

        # Simulate bar-by-bar forward
        partial_taken = False
        final_exit_price = None
        exit_reason      = "time"
        exit_bar_offset  = max_bars

        for j in range(1, max_bars + 1):
            bar_idx = i + 1 + j
            if bar_idx >= len(bars):
                # End of data
                final_exit_price = float(bars.iloc[bar_idx - 1]["Close"])
                exit_reason      = "eod"
                exit_bar_offset  = j
                break

            bar = bars.iloc[bar_idx]
            bar_high = float(bar["High"])
            bar_low  = float(bar["Low"])

            if is_long:
                # Check stop first (conservative: stop can be hit intrabar)
                if bar_low <= stop:
                    if not partial_taken:
                        final_exit_price = stop
                        exit_reason      = "stop_full"
                    else:
                        final_exit_price = stop
                        exit_reason      = "stop_partial"
                    exit_bar_offset = j
                    break

                # Check partial target
                if not partial_taken and bar_high >= target1:
                    partial_taken = True
                    # 50% off at target1, continue for rest

                # Check full target
                if partial_taken and bar_high >= target2:
                    final_exit_price = target2
                    exit_reason      = "target_full"
                    exit_bar_offset  = j
                    break

            else:  # SHORT
                if bar_high >= stop:
                    if not partial_taken:
                        final_exit_price = stop
                        exit_reason      = "stop_full"
                    else:
                        final_exit_price = stop
                        exit_reason      = "stop_partial"
                    exit_bar_offset = j
                    break

                if not partial_taken and bar_low <= target1:
                    partial_taken = True

                if partial_taken and bar_low <= target2:
                    final_exit_price = target2
                    exit_reason      = "target_full"
                    exit_bar_offset  = j
                    break

        # Time exit: close of bar max_bars
        if final_exit_price is None:
            exit_bar_final_idx = i + 1 + max_bars
            if exit_bar_final_idx < len(bars):
                final_exit_price = float(bars.iloc[exit_bar_final_idx]["Close"])
            else:
                final_exit_price = float(bars.iloc[-1]["Close"])
            exit_reason = "time"

        # ── Calculate P&L ────────────────────────────────────────────────────
        # For partial exit (50% at partial_r, 50% at final):
        if partial_taken and exit_reason == "target_full":
            # Both partials hit their targets
            pnl_pts_1 = (target1 - entry) * (1 if is_long else -1) * partial_pct
            pnl_pts_2 = (target2 - entry) * (1 if is_long else -1) * (1.0 - partial_pct)
            pnl_pts   = pnl_pts_1 + pnl_pts_2
        elif partial_taken and "stop" in exit_reason:
            # Partial taken at 1R, then stopped on remainder
            pnl_pts_1 = (target1 - entry) * (1 if is_long else -1) * partial_pct
            pnl_pts_2 = (final_exit_price - entry) * (1 if is_long else -1) * (1.0 - partial_pct)
            pnl_pts   = pnl_pts_1 + pnl_pts_2
        else:
            # Full position exit (stop, time, or eod before partial)
            pnl_pts = (final_exit_price - entry) * (1 if is_long else -1)

        pnl_gross = pnl_pts * point_value
        pnl_net   = pnl_gross - (2 * commission)  # round turn

        # R-multiple (normalized to initial risk)
        r_multiple = pnl_pts / risk_pts if risk_pts > 0 else 0.0

        # Win/loss definition: positive net P&L
        is_win = pnl_net > 0

        trades.append({
            "date":          pd.Timestamp(row["Timestamp"]).date(),
            "market":        market,
            "direction":     "LONG" if is_long else "SHORT",
            "entry":         round(entry, 4),
            "stop":          round(stop, 4),
            "target1":       round(target1, 4),
            "target2":       round(target2, 4),
            "exit_price":    round(final_exit_price, 4),
            "exit_reason":   exit_reason,
            "range_size":    round(range_size, 4),
            "risk_pts":      round(risk_pts, 4),
            "pnl_pts":       round(pnl_pts, 4),
            "pnl_gross":     round(pnl_gross, 2),
            "pnl_net":       round(pnl_net, 2),
            "r_multiple":    round(r_multiple, 3),
            "partial_taken": partial_taken,
            "exit_bars":     exit_bar_offset,
            "is_win":        is_win,
        })

    return trades


# ── Performance metrics ────────────────────────────────────────────────────────

def compute_metrics(trades: list[dict], market: str) -> dict:
    """Compute summary performance metrics from a list of trade dicts."""
    if not trades:
        return {"market": market, "total_trades": 0, "error": "No trades"}

    df = pd.DataFrame(trades)
    total    = len(df)
    wins     = df["is_win"].sum()
    losses   = total - wins
    win_rate = wins / total if total > 0 else 0.0

    gross_wins  = df.loc[df["pnl_net"] > 0, "pnl_net"].sum()
    gross_losses = abs(df.loc[df["pnl_net"] <= 0, "pnl_net"].sum())
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    avg_win  = df.loc[df["is_win"],  "pnl_net"].mean() if wins > 0   else 0.0
    avg_loss = df.loc[~df["is_win"], "pnl_net"].mean() if losses > 0 else 0.0
    avg_r    = df["r_multiple"].mean()

    total_net    = df["pnl_net"].sum()
    daily_pnl    = df.groupby("date")["pnl_net"].sum()
    avg_daily    = daily_pnl.mean()
    best_day     = daily_pnl.max()
    worst_day    = daily_pnl.min()

    exit_counts  = df["exit_reason"].value_counts().to_dict()
    partial_pct  = df["partial_taken"].mean() * 100

    return {
        "market":          market,
        "total_trades":    int(total),
        "win_rate_pct":    round(win_rate * 100, 1),
        "profit_factor":   round(pf, 2),
        "total_net_pnl":   round(total_net, 2),
        "avg_daily_pnl":   round(avg_daily, 2),
        "best_day":        round(best_day, 2),
        "worst_day":       round(worst_day, 2),
        "avg_win_usd":     round(avg_win, 2),
        "avg_loss_usd":    round(avg_loss, 2),
        "avg_r_multiple":  round(avg_r, 3),
        "partial_rate_pct": round(partial_pct, 1),
        "exit_breakdown":  exit_counts,
        "trading_days":    int(daily_pnl.shape[0]),
    }


# ── Print report ───────────────────────────────────────────────────────────────

def print_report(metrics_list: list[dict], signal_summaries: list[dict]) -> None:
    """Print a clean performance report to the console."""
    print("\n" + "=" * 65)
    print("  AlgoBot ORB Backtest Results — 60-Day Window")
    print("  Data: Yahoo Finance 5-min | Markets: ES + NQ")
    print("  Note: 60-day sample = proof-of-concept, not definitive")
    print("=" * 65)

    for sig in signal_summaries:
        m = sig.get("market", "?")
        print(f"\n  {m} Signal Summary:")
        print(f"    Trading days analyzed : {sig.get('trading_days', 0)}")
        print(f"    ORB long  signals     : {sig.get('orb_long_signals', 0)}")
        print(f"    ORB short signals     : {sig.get('orb_short_signals', 0)}")
        print(f"    HTF blocked           : {sig.get('htf_blocked', 0)}")
        print(f"    Signals per day       : {sig.get('signals_per_day', 0):.2f}")
        print(f"    HTF block rate        : {sig.get('htf_block_rate_pct', 0):.1f}%")

    print()
    print("-" * 65)

    total_pnl_all = 0.0
    for m in metrics_list:
        mkt = m.get("market", "?")
        if m.get("total_trades", 0) == 0:
            print(f"\n  {mkt}: No trades generated")
            continue

        total_pnl_all += m.get("total_net_pnl", 0)

        print(f"\n  {mkt} Trade Performance:")
        print(f"    Total trades       : {m['total_trades']}")
        print(f"    Win rate           : {m['win_rate_pct']:.1f}%")
        print(f"    Profit Factor      : {m['profit_factor']:.2f}")
        print(f"    Total net P&L      : ${m['total_net_pnl']:>10,.2f}")
        print(f"    Avg daily P&L      : ${m['avg_daily_pnl']:>10,.2f}")
        print(f"    Best day           : ${m['best_day']:>10,.2f}")
        print(f"    Worst day          : ${m['worst_day']:>10,.2f}")
        print(f"    Avg win            : ${m['avg_win_usd']:>10,.2f}")
        print(f"    Avg loss           : ${m['avg_loss_usd']:>10,.2f}")
        print(f"    Avg R per trade    : {m['avg_r_multiple']:>10.3f}R")
        print(f"    Partial exit rate  : {m['partial_rate_pct']:.1f}%")
        print(f"    Exit breakdown     : {m['exit_breakdown']}")

    if len(metrics_list) > 1:
        print(f"\n  COMBINED (ES + NQ):")
        print(f"    Total net P&L      : ${total_pnl_all:>10,.2f}")
        avg_daily_combined = total_pnl_all / max(
            max(m.get("trading_days", 1) for m in metrics_list), 1
        )
        print(f"    Avg daily P&L      : ${avg_daily_combined:>10,.2f}")

    print("\n" + "-" * 65)
    print("  INTERPRETATION GUIDE:")
    print("  Win rate > 55%  AND  PF > 1.5  -> Strategy has edge, pursue")
    print("  Win rate 45-55% AND  PF > 1.3  -> Marginal, needs improvement")
    print("  Win rate < 45%  OR   PF < 1.0  -> No edge in this sample")
    print("  Remember: 60-day sample has high variance. Direction matters")
    print("  more than exact numbers at this stage.")
    print("=" * 65 + "\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 65)
    print("  AlgoBot ORB Backtest — Starting")
    print("  Downloading 5-min ES + NQ data from Yahoo Finance...")
    print("=" * 65)

    config = load_config()
    orb_markets = config.get("intraday", {}).get("markets", ["ES", "NQ"])

    # ── Step 1: Download intraday data ────────────────────────────────────────
    intraday_data = download_all_intraday(
        markets=orb_markets,
        interval="5m",
        use_cache=True,
        force_refresh=False,
    )

    if not intraday_data:
        print("\nERROR: No intraday data downloaded. Check internet connection.")
        print("Yahoo Finance sometimes throttles — try again in a few minutes.\n")
        sys.exit(1)

    print("\nIntraday data downloaded:")
    summarize_intraday(intraday_data)

    # ── Step 2: Get HTF bias from daily data ──────────────────────────────────
    print("Computing HTF bias from daily bars...")
    htf_bias_by_market: dict[str, pd.Series] = {}
    for market in orb_markets:
        htf_bias_by_market[market] = get_htf_bias_series(market, config)

    # ── Step 3: Compute ORB signals ───────────────────────────────────────────
    print("\nComputing ORB signals...")
    orb_results: dict[str, pd.DataFrame] = {}
    signal_summaries = []

    for market in orb_markets:
        if market not in intraday_data:
            print(f"  {market}: No intraday data, skipping")
            continue

        df_5min = intraday_data[market]
        htf_bias = htf_bias_by_market.get(market, pd.Series(dtype=str))

        df_signals = compute_orb_signals(
            df_5min,
            market=market,
            config=config,
            htf_bias_series=htf_bias,
            default_htf_bias="NEUTRAL",
        )

        orb_results[market] = df_signals
        signal_summaries.append(summarize_orb_signals(df_signals, market))

    # ── Step 4: Simulate trades ───────────────────────────────────────────────
    print("Simulating trades...")
    all_trades  = []
    metrics_list = []

    for market, df_signals in orb_results.items():
        trades = simulate_orb_trades(df_signals, market, config)
        all_trades.extend(trades)
        metrics = compute_metrics(trades, market)
        metrics_list.append(metrics)
        print(f"  {market}: {len(trades)} trades simulated")

    # ── Step 5: Print report ──────────────────────────────────────────────────
    print_report(metrics_list, signal_summaries)

    # ── Step 6: Save trades to CSV ────────────────────────────────────────────
    if all_trades:
        reports_dir = PROJECT_ROOT / "reports" / "backtests"
        reports_dir.mkdir(parents=True, exist_ok=True)

        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        csv_path = reports_dir / f"orb_backtest_{ts}.csv"
        pd.DataFrame(all_trades).to_csv(csv_path, index=False)
        print(f"  Trade log saved: {csv_path.name}")
        print()


if __name__ == "__main__":
    main()
