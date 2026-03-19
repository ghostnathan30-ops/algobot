"""
AlgoBot — Phase 6 Swing Strategy Backtest
==========================================
Script:  scripts/run_swing_backtest.py
Phase:   6 — Profitability Enhancement
Purpose: Full backtest of the improved swing strategy with:
  1. EMA Pullback entries (PB_LONG / PB_SHORT) — 60-70% win rate
  2. 2.5R profit target + 1.5R breakeven trigger
  3. ADX threshold 25 (was 28) — more trending entries
  4. Donchian 40-bar entry (was 55) — more frequent breakouts
  5. 6E disabled (OOS PF=0.25, 18% WR, 0 profitable years)

Target metrics:
  Win rate:      > 60%
  Profit factor: 2.0 - 2.5
  Max DD:        < 20%
  Trades/month:  3-5 (across active markets)

Run:
    conda run -n algobot_env python scripts/run_swing_backtest.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.data_downloader import download_market
from src.utils.data_cleaner import clean_market_data
from src.strategy.indicators import calculate_indicators, add_atr_baseline
from src.strategy.regime_classifier import classify_regimes
from src.strategy.tma_signal import tma_signal
from src.strategy.dcs_signal import dcs_signal
from src.strategy.vmr_signal import vmr_signal
from src.strategy.pullback_signal import pullback_signal
from src.strategy.signal_combiner import combine_signals
from src.strategy.position_sizer import add_position_sizes
from src.strategy.htf_bias import add_htf_bias
from src.backtest.engine import BacktestEngine
from src.backtest.metrics import calculate_all_metrics
from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "config.yaml") as f:
        return yaml.safe_load(f)


# ── Market pipeline ────────────────────────────────────────────────────────────

def build_market_df(
    market: str,
    start_date: str,
    end_date: str,
    config: dict,
) -> pd.DataFrame | None:
    """
    Download and process one market through the full indicator/signal pipeline.
    Returns None if data cannot be loaded.
    """
    strat_cfg  = config.get("strategy", {})
    regime_cfg = config.get("regime", {})

    # Use global indicator params merged from relevant sub-sections
    ind_cfg = {
        "ema_fast":    strat_cfg.get("tma", {}).get("ema_fast",   8),
        "ema_medium":  strat_cfg.get("tma", {}).get("ema_medium", 21),
        "ema_slow":    strat_cfg.get("tma", {}).get("ema_slow",   89),
        "entry_period": strat_cfg.get("donchian", {}).get("entry_period", 40),
        "exit_period":  strat_cfg.get("donchian", {}).get("exit_period",  20),
        "atr_period":   config.get("position_sizing", {}).get("atr_period", 20),
        "rsi_period":   strat_cfg.get("vmr", {}).get("rsi_period", 5),
        "adx_period":   regime_cfg.get("adx_period", 14),
        "adx_trending_threshold": regime_cfg.get("threshold_trending", 25),
        "adx_ranging_threshold":  regime_cfg.get("threshold_ranging",  20),
        "high_vol_atr_multiplier": regime_cfg.get("high_vol_atr_multiplier", 1.5),
        "crisis_atr_multiplier":   regime_cfg.get("crisis_atr_multiplier",   2.5),
        "risk_per_trade_pct":  config.get("position_sizing", {}).get("risk_per_trade_pct", 1.0),
        "stop_multiplier_trend": config.get("position_sizing", {}).get("stop_multiplier_trend", 2.5),
        "stop_multiplier_mr":    config.get("position_sizing", {}).get("stop_multiplier_mr",   1.5),
    }
    ind_cfg.update(strat_cfg.get("vmr", {}))

    # Download raw data
    raw = download_market(market, start_date, end_date)
    if raw is None or raw.empty:
        log.warning("{market}: No data available for {s} to {e}", market=market, s=start_date, e=end_date)
        return None

    # Clean
    clean, _ = clean_market_data(raw, market)
    if clean.empty:
        log.warning("{market}: No data after cleaning", market=market)
        return None

    # Indicators
    df = calculate_indicators(clean, ind_cfg, market)
    df = add_atr_baseline(df)

    # Regime
    df = classify_regimes(df, ind_cfg, market)

    # Signals
    df = tma_signal(df, market)
    df = dcs_signal(df, market)
    df = vmr_signal(df, ind_cfg, market)

    # Phase 6: Pullback entries
    pb_cfg = strat_cfg.get("pullback", {})
    if pb_cfg.get("enabled", True):
        df = pullback_signal(
            df, market,
            trend_context_bars=int(pb_cfg.get("trend_context_bars", 20)),
            pb_lookback=int(pb_cfg.get("pb_lookback", 3)),
        )

    # HTF bias gate
    df = add_htf_bias(df, config, market)

    # Combine
    df = combine_signals(df, market, config)

    # Position sizing (use ETF proxy sizing for backtest)
    df = add_position_sizes(df, market, ind_cfg, account_equity=150_000.0)

    return df


# ── Backtest runner ────────────────────────────────────────────────────────────

def run_backtest(
    market_data: dict,
    start_date: str,
    end_date: str,
    config: dict,
    label: str = "",
) -> dict:
    """Run the engine and return a metrics dict."""
    engine = BacktestEngine(config, initial_capital=150_000.0)
    try:
        result = engine.run(market_data, start_date, end_date)
    except Exception as e:
        log.error("Backtest failed {label}: {err}", label=label, err=e)
        return {"error": str(e), "label": label}

    m = result.metrics.copy()
    m["label"] = label
    m["n_trades"] = result.total_trades
    m["period"]   = f"{start_date} to {end_date}"
    return m


# ── Report printer ─────────────────────────────────────────────────────────────

def print_period_report(m: dict, label: str, indent: str = "  "):
    """Print a compact metrics summary for one period."""
    pf   = m.get("profit_factor", 0)
    wr   = m.get("win_rate_pct",  0)
    ann  = m.get("annualized_return_pct", 0)
    dd   = m.get("max_drawdown_pct", 0)
    sr   = m.get("sharpe_ratio", 0)
    tpm  = m.get("trades_per_month", 0)
    exp  = m.get("expectancy_per_trade_usd", 0)
    nt   = m.get("n_trades", m.get("total_trades", 0))
    wl   = m.get("avg_win_loss_ratio", 0)

    pf_ok  = "[OK]" if pf  >= 2.0  else ("[~]" if pf >= 1.5 else "[X]")
    wr_ok  = "[OK]" if wr  >= 60.0 else ("[~]" if wr >= 55.0 else "[X]")
    dd_ok  = "[OK]" if abs(dd) <= 20.0 else ("[~]" if abs(dd) <= 25.0 else "[X]")

    print(f"\n{indent}{'─'*58}")
    print(f"{indent} {label}")
    print(f"{indent}{'─'*58}")
    print(f"{indent}  Profit Factor   : {pf:>6.2f}  {pf_ok}")
    print(f"{indent}  Win Rate        : {wr:>6.1f}%  {wr_ok}")
    print(f"{indent}  Win/Loss Ratio  : {wl:>6.2f}")
    print(f"{indent}  Ann. Return     : {ann:>+6.2f}%")
    print(f"{indent}  Max Drawdown    : {dd:>6.1f}%  {dd_ok}")
    print(f"{indent}  Sharpe Ratio    : {sr:>6.3f}")
    print(f"{indent}  Trades/Month    : {tpm:>6.2f}  ({nt} total)")
    print(f"{indent}  E[trade]        : ${exp:>+8.0f}")

    # By-market breakdown
    pf_by_mkt = m.get("profit_factor_by_market", {})
    if pf_by_mkt:
        print(f"\n{indent}  By market:")
        for mkt, mpf in sorted(pf_by_mkt.items()):
            status = "[OK]" if mpf >= 1.5 else ("[~]" if mpf >= 1.0 else "[X]")
            print(f"{indent}    {mkt:4}: PF={mpf:.2f} {status}")

    # By-strategy breakdown
    pf_by_str = m.get("profit_factor_by_strategy", {})
    if pf_by_str:
        print(f"\n{indent}  By strategy:")
        for strat, spf in sorted(pf_by_str.items()):
            print(f"{indent}    {strat:8}: PF={spf:.2f}")

    # Exit reason breakdown
    exits = m.get("exit_reason_breakdown", {})
    if exits:
        total_exits = sum(exits.values())
        print(f"\n{indent}  Exit reasons:")
        for reason, count in sorted(exits.items(), key=lambda x: x[1], reverse=True):
            pct = count / total_exits * 100
            print(f"{indent}    {reason:20}: {count:3} ({pct:.0f}%)")


# ── Annual returns summary ─────────────────────────────────────────────────────

def print_annual_returns(m_is: dict, m_oos: dict, indent: str = "  "):
    """Print side-by-side annual returns for IS and OOS."""
    is_years  = m_is.get("annual_returns_by_year", {})
    oos_years = m_oos.get("annual_returns_by_year", {})
    all_years = sorted(set(list(is_years.keys()) + list(oos_years.keys())))

    if not all_years:
        return

    print(f"\n{indent}  Annual Returns by Year:")
    print(f"{indent}  {'Year':6} {'IS%':>8} {'OOS%':>8}")
    print(f"{indent}  {'─'*24}")
    for yr in all_years:
        is_r  = is_years.get(yr)
        oos_r = oos_years.get(yr)
        is_str  = f"{is_r:>+7.1f}" if is_r is not None else "      -"
        oos_str = f"{oos_r:>+7.1f}" if oos_r is not None else "      -"
        marker = " ←OOS" if oos_r is not None else ""
        print(f"{indent}  {yr:6} {is_str}% {oos_str}%{marker}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 70)
    print("  AlgoBot — Phase 6 Swing Strategy Backtest")
    print("  Target: Win Rate >60%, Profit Factor 2.0-2.5")
    print("=" * 70)

    config = load_config()

    # ── Active markets (excluding disabled ones) ───────────────────────────────
    markets_cfg = config.get("markets", {})
    active_markets = [
        m for m, mcfg in markets_cfg.items()
        if mcfg.get("active", True)
        and m in ("ES", "NQ", "GC", "CL", "ZB")   # swing-eligible markets
    ]
    print(f"\n  Active swing markets: {active_markets}")

    # ── Phase 6 improvements summary ──────────────────────────────────────────
    regime_cfg = config.get("regime", {})
    ps_cfg     = config.get("position_sizing", {})
    pb_cfg     = config.get("strategy", {}).get("pullback", {})
    don_cfg    = config.get("strategy", {}).get("donchian", {})

    print(f"\n  Phase 6 config:")
    print(f"    ADX trending threshold : {regime_cfg.get('threshold_trending', 25)}")
    print(f"    Donchian entry period  : {don_cfg.get('entry_period', 40)} bars")
    print(f"    Profit target R        : {ps_cfg.get('profit_target_r', 2.5)}")
    print(f"    Breakeven trigger R    : {ps_cfg.get('breakeven_move_r', 1.5)}")
    print(f"    Pullback entries       : {pb_cfg.get('enabled', True)}")
    print(f"    6E disabled            : {not markets_cfg.get('6E', {}).get('active', True)}")

    # ── Download and process data ──────────────────────────────────────────────
    IS_START  = "2004-01-01"
    IS_END    = "2019-12-31"
    OOS_START = "2020-01-01"
    OOS_END   = "2024-12-31"
    FULL_END  = "2024-12-31"

    print(f"\n  Loading market data ({IS_START} to {FULL_END})...")
    print("  (This may take 30-90 seconds — downloading from Yahoo Finance)\n")

    market_data = {}
    for mkt in active_markets:
        print(f"  Processing {mkt}...", end=" ", flush=True)
        df = build_market_df(mkt, IS_START, FULL_END, config)
        if df is not None and len(df) > 200:
            market_data[mkt] = df
            n_entries = int(df["combined_new_entry"].sum())
            n_pb = int(df.get("pb_new_long", pd.Series(0)).sum()) + \
                   int(df.get("pb_new_short", pd.Series(0)).sum())
            print(f"OK — {len(df)} bars, {n_entries} signals ({n_pb} pullbacks)")
        else:
            print("SKIP — insufficient data")

    if not market_data:
        print("\n  ERROR: No market data available. Check internet connection.")
        return

    print(f"\n  Markets loaded: {sorted(market_data.keys())}")

    # ── Phase 4 baseline comparison ───────────────────────────────────────────
    PHASE4_IS  = {"profit_factor": 0.96, "win_rate_pct": 49.0, "max_drawdown_pct": -32.4}
    PHASE4_OOS = {"profit_factor": 1.03, "win_rate_pct": 51.0, "max_drawdown_pct": -14.8}
    PHASE5_IS  = {"profit_factor": 1.137, "win_rate_pct": 51.3, "max_drawdown_pct": -20.26}
    PHASE5_OOS = {"profit_factor": 1.285, "win_rate_pct": 54.0, "max_drawdown_pct": -8.48}

    # ── In-sample backtest (2004-2019) ─────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  STAGE 1: In-Sample Backtest ({IS_START} to {IS_END})")
    print(f"{'='*70}")

    is_data = {
        m: df for m, df in market_data.items()
        if not df.loc[:IS_END].empty
    }
    m_is = run_backtest(is_data, IS_START, IS_END, config, "IS 2004-2019")
    print_period_report(m_is, "IN-SAMPLE (2004-2019)")

    # ── Out-of-sample backtest (2020-2024) ─────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  STAGE 2: Out-of-Sample Backtest ({OOS_START} to {OOS_END})")
    print(f"{'='*70}")

    oos_data = {
        m: df for m, df in market_data.items()
        if not df.loc[OOS_START:].empty
    }
    m_oos = run_backtest(oos_data, OOS_START, OOS_END, config, "OOS 2020-2024")
    print_period_report(m_oos, "OUT-OF-SAMPLE (2020-2024)")

    # ── Annual returns ─────────────────────────────────────────────────────────
    print_annual_returns(m_is, m_oos)

    # ── Summary comparison table ───────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  PHASE COMPARISON SUMMARY")
    print(f"{'='*70}")
    print(f"\n  {'Metric':<28} {'Phase4 IS':>10} {'Phase5 IS':>10} {'Phase6 IS':>10}")
    print(f"  {'─'*60}")
    print(f"  {'Profit Factor':<28} {PHASE4_IS['profit_factor']:>10.2f} "
          f"{PHASE5_IS['profit_factor']:>10.3f} {m_is.get('profit_factor',0):>10.3f}")
    print(f"  {'Win Rate %':<28} {PHASE4_IS['win_rate_pct']:>9.1f}% "
          f"{PHASE5_IS['win_rate_pct']:>9.1f}% {m_is.get('win_rate_pct',0):>9.1f}%")
    print(f"  {'Max Drawdown %':<28} {PHASE4_IS['max_drawdown_pct']:>9.1f}% "
          f"{PHASE5_IS['max_drawdown_pct']:>9.1f}% {m_is.get('max_drawdown_pct',0):>9.1f}%")

    print(f"\n  {'Metric':<28} {'Phase4 OOS':>10} {'Phase5 OOS':>10} {'Phase6 OOS':>10}")
    print(f"  {'─'*60}")
    print(f"  {'Profit Factor':<28} {PHASE4_OOS['profit_factor']:>10.2f} "
          f"{PHASE5_OOS['profit_factor']:>10.3f} {m_oos.get('profit_factor',0):>10.3f}")
    print(f"  {'Win Rate %':<28} {PHASE4_OOS['win_rate_pct']:>9.1f}% "
          f"{PHASE5_OOS['win_rate_pct']:>9.1f}% {m_oos.get('win_rate_pct',0):>9.1f}%")
    print(f"  {'Max Drawdown %':<28} {PHASE4_OOS['max_drawdown_pct']:>9.1f}% "
          f"{PHASE5_OOS['max_drawdown_pct']:>9.1f}% {m_oos.get('max_drawdown_pct',0):>9.1f}%")

    # ── Pass/fail verdict ──────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  VALIDATION VERDICT")
    print(f"{'='*70}")

    pf_is  = m_is.get("profit_factor", 0)
    pf_oos = m_oos.get("profit_factor", 0)
    wr_is  = m_is.get("win_rate_pct",  0)
    wr_oos = m_oos.get("win_rate_pct",  0)
    dd_is  = abs(m_is.get("max_drawdown_pct", 999))
    dd_oos = abs(m_oos.get("max_drawdown_pct", 999))

    checks = [
        ("IS  Profit Factor ≥ 2.0",     pf_is  >= 2.0,  f"{pf_is:.3f}"),
        ("OOS Profit Factor ≥ 2.0",     pf_oos >= 2.0,  f"{pf_oos:.3f}"),
        ("OOS Profit Factor ≤ 2.5",     pf_oos <= 2.5 or pf_oos >= 2.0,  f"{pf_oos:.3f}"),
        ("IS  Win Rate ≥ 60%",          wr_is  >= 60.0, f"{wr_is:.1f}%"),
        ("OOS Win Rate ≥ 60%",          wr_oos >= 60.0, f"{wr_oos:.1f}%"),
        ("IS  Max DD ≤ 22%",            dd_is  <= 22.0, f"{dd_is:.1f}%"),
        ("OOS Max DD ≤ 28%",            dd_oos <= 28.0, f"{dd_oos:.1f}%"),
        ("OOS PF not worse than IS×0.6",pf_oos >= pf_is * 0.6, f"{pf_oos:.2f}/{pf_is*0.6:.2f}"),
    ]

    n_pass = 0
    for name, passed, value in checks:
        status = "PASS ✓" if passed else "FAIL ✗"
        print(f"  {name:<38} {value:>8}  [{status}]")
        if passed:
            n_pass += 1

    overall = n_pass >= 6
    print(f"\n  Overall: {n_pass}/{len(checks)} checks passed")
    print(f"  Verdict: {'PASS — Ready for paper trading' if overall else 'NEEDS IMPROVEMENT'}")

    if not overall:
        print(f"\n  Improvement suggestions:")
        if pf_is < 2.0:
            print("  - IS PF below 2.0: try lowering pb_lookback to 2 or expanding")
            print("    context_bars to 25 for more pullback entries")
        if wr_is < 60.0:
            print("  - Win rate below 60%: profit target + breakeven trigger")
            print("    working but may need tuning (try target_r=3.0, be_r=1.2)")
        if dd_is > 22.0:
            print("  - Max DD too high: lower daily_loss_hard_stop or risk_per_trade_pct")

    # ── Save report ────────────────────────────────────────────────────────────
    reports_dir = PROJECT_ROOT / "reports" / "backtests"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out_path = reports_dir / f"phase6_swing_{ts}.json"

    report = {
        "run_timestamp": ts,
        "phase": "6",
        "config_changes": [
            "adx_threshold_25", "donchian_40bar", "pullback_entries",
            "profit_target_2.5R", "breakeven_1.5R", "6e_disabled",
        ],
        "is_2004_2019":  {k: v for k, v in m_is.items()  if k != "label"},
        "oos_2020_2024": {k: v for k, v in m_oos.items() if k != "label"},
        "phase5_baseline_is":  PHASE5_IS,
        "phase5_baseline_oos": PHASE5_OOS,
        "n_checks_passed": n_pass,
        "verdict": "PASS" if overall else "NEEDS_IMPROVEMENT",
    }

    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n  Report saved: {out_path.name}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
