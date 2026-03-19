"""
AlgoBot -- Combined ORB + FHB Backtest
========================================
Script:  scripts/run_combined_backtest.py
Phase:   5E -- Combined Strategy Performance
Purpose: Run ORB (5-min) and FHB (1-hour) simultaneously on ES + NQ,
         show combined trade frequency, daily P&L, and PF.

How it works:
  - ORB  fires at 9:30-9:45 range breakout, entry ~9:45-10:00 ET
  - FHB  fires at 9:30-10:30 range breakout, entry ~10:30 ET
  - Both strategies run independently on the same market each day
  - A day can generate 0-2 signals per market (1 ORB + 1 FHB)
  - Position sizes are independent; stops are independent
  - This is the target architecture for Phase 6 live trading

Trade frequency target:
  ES:  ORB 0.45/day + FHB 0.35/day = ~0.80/day
  NQ:  ORB 0.45/day + FHB 0.34/day = ~0.79/day
  Combined 2 markets: ~1.6 trades/day
  Target with 4+ markets (after real delta data): 4-6 trades/day

Run:
    cd AlgoBot
    conda run -n algobot_env python scripts/run_combined_backtest.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ORB imports
from src.utils.yf_intraday import download_all_intraday
from src.strategy.orb_signal import compute_orb_signals

# FHB imports (reuse functions from run_fhb_backtest)
sys.path.insert(0, str(Path(__file__).parent))
from run_fhb_backtest import (
    download_1h_intraday, compute_1h_atr, get_htf_data,
    compute_fhb_signals, simulate_fhb_trades, compute_metrics as fhb_metrics,
    FHB_ATR_PERIOD, FHB_ATR_STOP_MULT, FHB_GLS_HALF_SCORE, FHB_GLS_MIN_SCORE,
    FHB_OVERNIGHT_CARRY,
)

from src.utils.econ_calendar import EconCalendar
from src.utils.vix_filter import VIXFilter
from src.utils.trade_readiness import GreenLightScore
from src.utils.trade_db import TradeDB
from src.utils.data_downloader import download_market
from src.strategy.indicators import calculate_indicators, add_atr_baseline
from src.strategy.regime_classifier import classify_regimes
from src.strategy.htf_bias import add_htf_bias
from src.utils.logger import get_logger

log = get_logger(__name__)


def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


# ============================================================
# ORB SIMULATION (adapted from run_orb_backtest)
# ============================================================

def get_htf_bias_series(market: str, config: dict) -> pd.Series:
    raw = download_market(market, "2022-01-01", "2026-12-31")
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


def simulate_orb_trades(df_5min: pd.DataFrame, market: str, config: dict) -> list[dict]:
    orb_cfg     = config.get("intraday", {}).get("orb", {})
    partial_r   = float(orb_cfg.get("partial_exit_r",   1.0))
    full_r      = float(orb_cfg.get("profit_target_r",  2.0))
    partial_pct = float(orb_cfg.get("partial_exit_pct", 0.5))
    max_bars    = int(orb_cfg.get("max_hold_bars",      24))
    markets_cfg = config.get("markets", {})
    point_value = float(markets_cfg.get(market, {}).get("point_value",  50.0))
    commission  = float(markets_cfg.get(market, {}).get("commission",    5.0))
    slippage_tks = int( markets_cfg.get(market, {}).get("slippage_ticks", 1))
    tick_size   = float(markets_cfg.get(market, {}).get("tick_size",    0.25))
    slippage_pts = slippage_tks * tick_size

    trades = []
    bars   = df_5min.reset_index()

    for i, row in bars.iterrows():
        is_long  = bool(row.get("orb_long_signal",  False))
        is_short = bool(row.get("orb_short_signal", False))
        blocked  = bool(row.get("orb_htf_blocked",  False))
        if not (is_long or is_short) or blocked:
            continue
        if i + 1 >= len(bars):
            continue

        next_bar   = bars.iloc[i + 1]
        entry_raw  = float(next_bar["Open"])
        range_high = float(row["orb_range_high"])
        range_low  = float(row["orb_range_low"])
        range_size = range_high - range_low
        if range_size <= 0:
            continue

        entry = entry_raw + slippage_pts if is_long else entry_raw - slippage_pts
        stop  = range_low  - slippage_pts if is_long else range_high + slippage_pts
        risk_pts = abs(entry - stop)
        if risk_pts <= 0:
            continue

        target1 = entry + partial_r * risk_pts * (1 if is_long else -1)
        target2 = entry + full_r    * risk_pts * (1 if is_long else -1)

        partial_taken    = False
        final_exit_price = None
        exit_reason      = "time"
        exit_bar_offset  = max_bars
        stop_be          = stop   # start at initial stop

        for j in range(1, max_bars + 1):
            bar_idx = i + 1 + j
            if bar_idx >= len(bars):
                final_exit_price = float(bars.iloc[bar_idx - 1]["Close"])
                exit_reason      = "eod"
                exit_bar_offset  = j
                break
            bar      = bars.iloc[bar_idx]
            bar_high = float(bar["High"])
            bar_low  = float(bar["Low"])

            if is_long:
                if bar_low <= stop_be:
                    final_exit_price = stop_be if partial_taken else stop_be
                    exit_reason      = "stop_partial" if partial_taken else "stop_full"
                    exit_bar_offset  = j
                    break
                if not partial_taken and bar_high >= target1:
                    partial_taken = True
                    stop_be       = entry   # trail to breakeven
                if bar_high >= target2:
                    final_exit_price = target2
                    exit_reason      = "target_full"
                    exit_bar_offset  = j
                    break
            else:
                if bar_high >= stop_be:
                    final_exit_price = stop_be
                    exit_reason      = "stop_partial" if partial_taken else "stop_full"
                    exit_bar_offset  = j
                    break
                if not partial_taken and bar_low <= target1:
                    partial_taken = True
                    stop_be       = entry
                if bar_low <= target2:
                    final_exit_price = target2
                    exit_reason      = "target_full"
                    exit_bar_offset  = j
                    break

        if final_exit_price is None:
            final_exit_price = float(bars.iloc[min(i + 1 + max_bars, len(bars)-1)]["Close"])
            exit_reason      = "time"

        if partial_taken and exit_reason == "target_full":
            pnl_pts = ((target1 - entry) * partial_pct +
                       (target2 - entry) * (1 - partial_pct)) * (1 if is_long else -1)
        elif partial_taken and "stop" in exit_reason:
            pnl_pts = ((target1 - entry) * partial_pct +
                       (final_exit_price - entry) * (1 - partial_pct)) * (1 if is_long else -1)
        else:
            pnl_pts = (final_exit_price - entry) * (1 if is_long else -1)

        pnl_gross  = pnl_pts * point_value
        pnl_net    = pnl_gross - (2 * commission)
        r_multiple = pnl_pts / risk_pts if risk_pts > 0 else 0.0

        trades.append({
            "strategy":      "ORB",
            "date":          pd.Timestamp(row["Timestamp"]).date(),
            "market":        market,
            "direction":     "LONG" if is_long else "SHORT",
            "entry":         round(entry, 4),
            "exit_price":    round(final_exit_price, 4),
            "exit_reason":   exit_reason,
            "risk_pts":      round(risk_pts, 4),
            "pnl_pts":       round(pnl_pts, 4),
            "pnl_gross":     round(pnl_gross, 2),
            "pnl_net":       round(pnl_net, 2),
            "r_multiple":    round(r_multiple, 3),
            "partial_taken": partial_taken,
            "exit_bars":     exit_bar_offset,
            "is_win":        pnl_net > 0,
        })
    return trades


# ============================================================
# COMBINED METRICS
# ============================================================

def combined_metrics(all_trades: list[dict], label: str = "COMBINED") -> dict:
    if not all_trades:
        return {"label": label, "total_trades": 0}
    df    = pd.DataFrame(all_trades)
    total = len(df)
    wins  = int(df["is_win"].sum())
    gw    = df.loc[df["pnl_net"] > 0,  "pnl_net"].sum()
    gl    = abs(df.loc[df["pnl_net"] <= 0, "pnl_net"].sum())
    pf    = gw / gl if gl > 0 else float("inf")
    daily = df.groupby("date")["pnl_net"].sum()
    cumul = daily.cumsum()
    max_dd = float((cumul - cumul.cummax()).min())
    # per-strategy breakdown
    strat_summary = {}
    for strat, grp in df.groupby("strategy"):
        sg  = grp.loc[grp["pnl_net"] > 0,  "pnl_net"].sum()
        sl  = abs(grp.loc[grp["pnl_net"] <= 0, "pnl_net"].sum())
        strat_summary[strat] = {
            "trades":  len(grp),
            "win_pct": round(grp["is_win"].mean() * 100, 1),
            "pf":      round(sg / sl if sl > 0 else float("inf"), 2),
            "total_pnl": round(grp["pnl_net"].sum(), 2),
        }
    # per-market breakdown
    market_daily_days = df.groupby("market")["date"].nunique()
    return {
        "label":          label,
        "total_trades":   total,
        "win_rate_pct":   round(wins / total * 100, 1),
        "profit_factor":  round(pf, 2),
        "total_net_pnl":  round(df["pnl_net"].sum(), 2),
        "trading_days":   int(daily.shape[0]),
        "avg_daily_pnl":  round(daily.mean(), 2),
        "best_day":       round(daily.max(), 2),
        "worst_day":      round(daily.min(), 2),
        "max_drawdown":   round(max_dd, 2),
        "trades_per_day": round(total / max(daily.shape[0], 1), 2),
        "by_strategy":    strat_summary,
        "market_days":    market_daily_days.to_dict(),
    }


# ============================================================
# MAIN
# ============================================================

def main():
    print("\n" + "=" * 70)
    print("  AlgoBot -- Combined ORB + FHB Backtest (Phase 5E)")
    print("=" * 70)

    config  = load_config()
    markets = ["ES", "NQ"]

    # ── Phase 5D filters ──────────────────────────────────────────────────────
    print("\nStep 1: Initialising filters...")
    econ_cal   = EconCalendar()
    vix_filter = VIXFilter.from_yahoo(start="2023-01-01", end="2026-12-31")
    gls_engine = GreenLightScore(
        full_size_threshold=FHB_GLS_HALF_SCORE,
        half_size_threshold=FHB_GLS_MIN_SCORE,
    )
    db_path = PROJECT_ROOT / "data" / "trades.db"
    db      = TradeDB(str(db_path))

    # ── ORB: 5-min data (~60 days) ────────────────────────────────────────────
    print("\nStep 2: ORB -- Downloading 5-min data (~60 days)...")
    intraday_5m = download_all_intraday(
        markets=markets, interval="5m",
    )
    orb_trades_all: list[dict] = []
    orb_days_total = 0

    for market in markets:
        df_5m = intraday_5m.get(market, pd.DataFrame())
        if df_5m.empty:
            print(f"  {market}: No 5-min data available")
            continue
        htf_bias = get_htf_bias_series(market, config)
        df_orb   = compute_orb_signals(df_5m, market, config, htf_bias)
        trades   = simulate_orb_trades(df_orb, market, config)
        orb_trades_all.extend(trades)
        days     = df_5m.index.normalize().nunique()
        orb_days_total = max(orb_days_total, days)
        n = len(trades)
        wt = sum(1 for t in trades if t["is_win"])
        gw_ = sum(t["pnl_net"] for t in trades if t["pnl_net"] > 0)
        gl_ = abs(sum(t["pnl_net"] for t in trades if t["pnl_net"] <= 0))
        pf_ = gw_ / gl_ if gl_ > 0 else float("inf")
        print(f"  ORB {market}: {n} trades | Win={wt/n*100:.1f}% | PF={pf_:.2f} | "
              f"P&L=${sum(t['pnl_net'] for t in trades):,.0f} | {n/days:.2f}/day")

    # ── FHB: 1-hour data (~730 days) ─────────────────────────────────────────
    print("\nStep 3: FHB -- Downloading 1-hour data (~730 days)...")
    fhb_1h: dict  = {}
    fhb_atr: dict = {}
    fhb_htf: dict = {}
    fhb_reg: dict = {}

    for market in markets:
        df = download_1h_intraday(market)
        if not df.empty:
            fhb_1h[market]  = df
            fhb_atr[market] = compute_1h_atr(df, FHB_ATR_PERIOD)
            bias, regime, _ = get_htf_data(market, config)
            fhb_htf[market] = bias
            fhb_reg[market] = regime

    fhb_trades_all: list[dict] = []
    fhb_days_total = 0

    for market in markets:
        if market not in fhb_1h:
            continue
        df_sig = compute_fhb_signals(
            fhb_1h[market], market, fhb_htf[market], fhb_reg[market], config,
            econ_cal=econ_cal, vix_filter=vix_filter, gls_engine=gls_engine,
        )
        trades = simulate_fhb_trades(
            df_sig, fhb_atr[market], market, config,
            use_atr_stop=True, trail_be=True,
            overnight_carry=FHB_OVERNIGHT_CARRY,
            label="5E", db=db,
        )
        # Tag with strategy name
        for t in trades:
            t["strategy"] = "FHB"
        fhb_trades_all.extend(trades)
        days = fhb_1h[market].index.normalize().nunique()
        fhb_days_total = max(fhb_days_total, days)
        n  = len(trades)
        wt = sum(1 for t in trades if t["is_win"])
        gw_= sum(t["pnl_net"] for t in trades if t["pnl_net"] > 0)
        gl_= abs(sum(t["pnl_net"] for t in trades if t["pnl_net"] <= 0))
        pf_= gw_ / gl_ if gl_ > 0 else float("inf")
        print(f"  FHB {market}: {n} trades | Win={wt/n*100:.1f}% | PF={pf_:.2f} | "
              f"P&L=${sum(t['pnl_net'] for t in trades):,.0f} | {n/days:.2f}/day")

    # ── Combined report ───────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  COMBINED STRATEGY PERFORMANCE REPORT")
    print("=" * 70)

    # ORB standalone
    if orb_trades_all:
        om = combined_metrics(orb_trades_all, "ORB")
        print(f"\n  ORB (5-min, ~{orb_days_total} days):")
        print(f"    Trades          : {om['total_trades']}  "
              f"({om['trades_per_day']:.2f}/day)")
        print(f"    Win Rate        : {om['win_rate_pct']:.1f}%")
        print(f"    Profit Factor   : {om['profit_factor']:.2f}")
        print(f"    Total P&L       : ${om['total_net_pnl']:>10,.2f}")
        print(f"    Avg Daily P&L   : ${om['avg_daily_pnl']:>10,.2f}  "
              f"(annualised ~${om['avg_daily_pnl']*252:,.0f})")
        print(f"    Max Drawdown    : ${om['max_drawdown']:>10,.2f}")

    # FHB standalone
    if fhb_trades_all:
        fm = combined_metrics(fhb_trades_all, "FHB")
        print(f"\n  FHB (1-hour, ~{fhb_days_total} days):")
        print(f"    Trades          : {fm['total_trades']}  "
              f"({fm['trades_per_day']:.2f}/day)")
        print(f"    Win Rate        : {fm['win_rate_pct']:.1f}%")
        print(f"    Profit Factor   : {fm['profit_factor']:.2f}")
        print(f"    Total P&L       : ${fm['total_net_pnl']:>10,.2f}")
        print(f"    Avg Daily P&L   : ${fm['avg_daily_pnl']:>10,.2f}  "
              f"(annualised ~${fm['avg_daily_pnl']*252:,.0f})")
        print(f"    Max Drawdown    : ${fm['max_drawdown']:>10,.2f}")

    # Combined projection (ORB $/day + FHB $/day)
    orb_daily = combined_metrics(orb_trades_all).get("avg_daily_pnl", 0) if orb_trades_all else 0
    fhb_daily = combined_metrics(fhb_trades_all).get("avg_daily_pnl", 0) if fhb_trades_all else 0
    orb_tpd   = combined_metrics(orb_trades_all).get("trades_per_day", 0) if orb_trades_all else 0
    fhb_tpd   = combined_metrics(fhb_trades_all).get("trades_per_day", 0) if fhb_trades_all else 0

    print(f"\n  COMBINED PROJECTION (ES + NQ, 2 strategies):")
    print(f"  {'Metric':<30} {'ORB':>12} {'FHB':>12} {'Combined':>12}")
    print("  " + "-" * 68)
    print(f"  {'Avg trades/day':<30} {orb_tpd:>12.2f} {fhb_tpd:>12.2f} "
          f"{orb_tpd+fhb_tpd:>12.2f}")
    print(f"  {'Avg daily P&L':<30} ${orb_daily:>10,.0f} ${fhb_daily:>10,.0f} "
          f"${orb_daily+fhb_daily:>10,.0f}")
    print(f"  {'Annualised P&L':<30} ${orb_daily*252:>10,.0f} ${fhb_daily*252:>10,.0f} "
          f"${(orb_daily+fhb_daily)*252:>10,.0f}")

    print(f"\n  NOTE: ORB uses 60-day sample; FHB uses 730-day sample.")
    print(f"  ORB daily P&L figure has high variance -- treat as directional.")
    print(f"\n  PATH TO 8-10 TRADES/DAY:")
    print(f"    Current (ES+NQ, ORB+FHB)  : {orb_tpd+fhb_tpd:.1f}/day")
    print(f"    + GC, RTY (with real delta): +~1.2/day  -> ~{orb_tpd+fhb_tpd+1.2:.1f}/day")
    print(f"    + ORB on GC, RTY           : +~0.8/day  -> ~{orb_tpd+fhb_tpd+2.0:.1f}/day")
    print(f"    + 30-min continuation setup: +~2.0/day  -> ~{orb_tpd+fhb_tpd+4.0:.1f}/day")
    print(f"    Sierra Chart real delta    : quality improves, count stays")
    print("=" * 70 + "\n")

    # Save combined CSV
    all_trades = orb_trades_all + fhb_trades_all
    if all_trades:
        reports_dir = PROJECT_ROOT / "reports" / "backtests"
        reports_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out = reports_dir / f"combined_5E_{ts}.csv"
        pd.DataFrame(all_trades).to_csv(out, index=False)
        print(f"  Combined trade log saved: {out.name}")

    db.close()


if __name__ == "__main__":
    main()
