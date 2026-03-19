"""
AlgoBot — Sierra Charts Real-Data Backtest
============================================
Script:  scripts/run_sc_backtest.py
Purpose: Run the full FHB (1h) + ORB (5m) backtest on REAL Sierra Charts
         futures data instead of Yahoo Finance proxies.

Why this matters:
  The standard backtests use Yahoo Finance ETF proxies (QQQ for NQ, GC=F
  from Yahoo, etc.) which:
    - Are limited to ~60 days for intraday bars (5m/1h)
    - Use ETF prices, not actual futures contract prices (different $-value)
    - Miss overnight sessions and have adjusted pricing

  This script uses the actual CME/COMEX/NYMEX futures data exported from
  Sierra Charts, giving us:
    - True futures prices (correct point values, $-P&L calculations)
    - Multi-year intraday history stitched from contract rolls
    - Real bid/ask volume (used for synthetic delta confirmation)
    - NQ, MNQ, GC, MGC, CL — all covered

Markets tested:
  NQ   — E-mini Nasdaq-100 (CME)  — FHB (1h) + ORB (5m)
  MNQ  — Micro E-mini NQ (CME)    — FHB (1h) + ORB (5m)  [1/10 contract]
  GC   — Gold Futures (COMEX)     — FHB (1h)
  MGC  — Micro Gold (COMEX)       — FHB (1h)              [1/10 contract]
  CL   — Crude Oil (NYMEX)        — FHB (1h) + ORB (5m)

Output:
  - Full console report (strategies, markets, year-by-year)
  - reports/backtests/sc_backtest_TIMESTAMP.json  ← loaded by dashboard

Run:
    cd AlgoBot
    python scripts/run_sc_backtest.py
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
SC_DIR       = PROJECT_ROOT / "data" / "sierra_charts"
REPORTS_DIR  = PROJECT_ROOT / "reports" / "backtests"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

# ── Sierra Charts loader ────────────────────────────────────────────────────────
from src.utils.sierra_loader import (
    load_sc_continuous,
    load_sc_daily_for_htf,
    load_all_sc_markets,
)

# ── Reuse FHB logic from run_fhb_backtest ────────────────────────────────────
from run_fhb_backtest import (
    compute_1h_atr,
    compute_fhb_signals,
    simulate_fhb_trades,
    compute_metrics as fhb_metrics,
    yearly_breakdown,
    FHB_ATR_PERIOD,
    FHB_ATR_STOP_MULT,
    FHB_OVERNIGHT_CARRY,
    FHB_GLS_MIN_SCORE,
    FHB_GLS_HALF_SCORE,
)

# ── Reuse ORB logic from run_orb_backtest ────────────────────────────────────
from run_orb_backtest import (
    simulate_orb_trades,
)
from src.strategy.orb_signal import compute_orb_signals

# ── Shared library modules ────────────────────────────────────────────────────
from src.strategy.indicators    import calculate_indicators, add_atr_baseline
from src.strategy.regime_classifier import classify_regimes
from src.strategy.htf_bias      import add_htf_bias
from src.utils.econ_calendar    import EconCalendar
from src.utils.vix_filter       import VIXFilter
from src.utils.trade_readiness  import GreenLightScore
from src.utils.trade_db         import TradeDB
from src.utils.logger           import get_logger

log = get_logger(__name__)

# ── Contract specs for SC markets ─────────────────────────────────────────────
# MNQ and MGC are 1/10th of NQ and GC but the config only has NQ/GC.
# We override the point values so dollar P&L is correct.
SC_MARKET_OVERRIDES: dict[str, dict] = {
    "MNQ": {"point_value": 2.0,   "tick_size": 0.25,  "commission": 2.5, "slippage_ticks": 1},
    "MGC": {"point_value": 1.0,   "tick_size": 0.10,  "commission": 1.5, "slippage_ticks": 1},
    "NQ":  {"point_value": 20.0,  "tick_size": 0.25,  "commission": 5.0, "slippage_ticks": 1},
    "GC":  {"point_value": 100.0, "tick_size": 0.10,  "commission": 5.0, "slippage_ticks": 1},
    "CL":  {"point_value": 1000.0,"tick_size": 0.01,  "commission": 5.0, "slippage_ticks": 1},
}

# ── Markets to run ─────────────────────────────────────────────────────────────
FHB_MARKETS = ["NQ", "MNQ", "GC", "MGC", "CL"]
ORB_MARKETS = ["NQ"]   # ORB validated on NQ; CL/MNQ added in future phase

RTH_START = "09:30"
RTH_END   = "16:00"


# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "config.yaml") as f:
        return yaml.safe_load(f)


def patch_config_for_sc(config: dict) -> dict:
    """
    Inject SC market overrides into the config's markets section.
    This ensures simulate_fhb_trades / simulate_orb_trades use correct
    point values for micro contracts (MNQ, MGC).
    Also extends the intraday.markets list so compute_orb_signals accepts CL.
    """
    cfg = dict(config)

    # ── Contract spec overrides ─────────────────────────────────────────────
    markets_section = dict(cfg.get("markets", {}))
    for mkt, overrides in SC_MARKET_OVERRIDES.items():
        entry = dict(markets_section.get(mkt, markets_section.get("NQ", {})))
        entry.update(overrides)
        markets_section[mkt] = entry
    cfg["markets"] = markets_section

    # ── Extend ORB intraday markets to include CL ───────────────────────────
    intraday = dict(cfg.get("intraday", {}))
    current_mkts = list(intraday.get("markets", ["ES", "NQ"]))
    for m in ["NQ", "MNQ", "CL"]:
        if m not in current_mkts:
            current_mkts.append(m)
    intraday["markets"] = current_mkts
    cfg["intraday"] = intraday

    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Data loaders: SC → strategy-ready DataFrames
# ─────────────────────────────────────────────────────────────────────────────

def load_sc_1h_rth(market: str) -> pd.DataFrame:
    """
    Load SC 1-hour bars for ``market``, RTH-filtered (09:30–16:00 ET).
    Returns empty DataFrame if SC data not available.
    """
    try:
        df = load_sc_continuous(market, "1h", SC_DIR)
    except ValueError as e:
        print(f"  WARNING [{market}/1h]: {e}")
        return pd.DataFrame()

    df = df.between_time(RTH_START, RTH_END)
    for col in ["Open", "High", "Low", "Close"]:
        if col in df.columns:
            df[col] = df[col].astype(float)
    df.index.name = "Timestamp"
    return df


def load_sc_5m_rth(market: str) -> pd.DataFrame:
    """
    Load SC 5-minute bars for ``market``, RTH-filtered (09:30–16:00 ET).
    Returns empty DataFrame if SC data not available.
    """
    try:
        df = load_sc_continuous(market, "5m", SC_DIR)
    except ValueError as e:
        print(f"  WARNING [{market}/5m]: {e}")
        return pd.DataFrame()

    df = df.between_time(RTH_START, RTH_END)
    for col in ["Open", "High", "Low", "Close"]:
        if col in df.columns:
            df[col] = df[col].astype(float)
    df.index.name = "Timestamp"
    return df


def get_sc_htf_data(
    market: str,
    config: dict,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Build HTF bias, daily regime, and fast bias from SC daily bars.
    Falls back to NEUTRAL if SC daily data is unavailable.

    Returns:
        (htf_bias_series, regime_series, fast_bias_series)
    """
    # Map SC root to config market key (MNQ → NQ, MGC → GC for indicators)
    config_key = {"MNQ": "NQ", "MGC": "GC"}.get(market, market)

    try:
        daily_df = load_sc_daily_for_htf(market, SC_DIR)
    except ValueError:
        # Fallback: try the parent contract (e.g. MNQ → NQ)
        try:
            daily_df = load_sc_daily_for_htf(config_key, SC_DIR)
        except ValueError:
            print(f"  WARNING [{market}]: No SC daily data — using NEUTRAL bias")
            empty = pd.Series(dtype=str)
            return empty, empty, empty

    if daily_df.empty or len(daily_df) < 50:
        print(f"  WARNING [{market}]: SC daily data too sparse ({len(daily_df)} bars) — using NEUTRAL")
        empty = pd.Series(dtype=str)
        return empty, empty, empty

    strat_cfg  = config.get("strategy", config)
    regime_cfg = config.get("regime", strat_cfg)

    df = calculate_indicators(daily_df, strat_cfg, config_key)
    df = add_atr_baseline(df)
    df = classify_regimes(df, regime_cfg, config_key)
    df = add_htf_bias(df, config, config_key)

    bias = df["htf_combined_bias"].copy()
    bias.index = pd.to_datetime(bias.index).normalize()

    regime = df["regime"].copy()
    regime.index = pd.to_datetime(regime.index).normalize()

    fast_bias = df.get("fast_bias", pd.Series(dtype=str)).copy()
    fast_bias.index = pd.to_datetime(fast_bias.index).normalize()

    return bias, regime, fast_bias


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate metrics across markets
# ─────────────────────────────────────────────────────────────────────────────

def combined_metrics(all_trades: list[dict], label: str) -> dict:
    """Compute portfolio-level metrics from a flat list of all strategy trades."""
    if not all_trades:
        return {"label": label, "total_trades": 0}

    df       = pd.DataFrame(all_trades)
    total    = len(df)
    wins     = int(df["is_win"].sum())
    win_rate = wins / total if total > 0 else 0.0

    gross_wins   = df.loc[df["pnl_net"] > 0,  "pnl_net"].sum()
    gross_losses = abs(df.loc[df["pnl_net"] <= 0, "pnl_net"].sum())
    pf           = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    daily_pnl = df.groupby("date")["pnl_net"].sum()
    cum_eq    = daily_pnl.cumsum()
    max_dd    = float((cum_eq - cum_eq.cummax()).min())
    total_pnl = float(df["pnl_net"].sum())

    # Sharpe (daily returns)
    if len(daily_pnl) > 1 and daily_pnl.std() > 0:
        sharpe = float((daily_pnl.mean() / daily_pnl.std()) * (252 ** 0.5))
    else:
        sharpe = 0.0

    return {
        "label":         label,
        "total_trades":  total,
        "win_rate_pct":  round(win_rate * 100, 1),
        "profit_factor": round(pf, 2),
        "total_pnl":     round(total_pnl, 2),
        "max_drawdown":  round(max_dd, 2),
        "sharpe_daily":  round(sharpe, 2),
        "trading_days":  int(daily_pnl.shape[0]),
        "avg_daily_pnl": round(float(daily_pnl.mean()), 2),
        "trades_per_day": round(total / max(daily_pnl.shape[0], 1), 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Print helpers
# ─────────────────────────────────────────────────────────────────────────────

SEP  = "=" * 72
SEP2 = "-" * 72

def hdr(title: str) -> None:
    print(f"\n{SEP}\n  {title}\n{SEP}")

def sub(title: str) -> None:
    print(f"\n{SEP2}\n  {title}\n{SEP2}")


def print_market_table(results: dict[str, dict]) -> None:
    print(f"  {'Market':<8} {'Trades':>7} {'Win%':>7} {'PF':>7} {'Net P&L':>12} {'MaxDD':>12} {'Avg/Day':>10}")
    print("  " + "-" * 65)
    for mkt, m in sorted(results.items()):
        if m.get("total_trades", 0) == 0:
            print(f"  {mkt:<8} {'—':>7}")
            continue
        print(
            f"  {mkt:<8} {m['total_trades']:>7} {m['win_rate_pct']:>6.1f}%"
            f" {m['profit_factor']:>7.2f} ${m['total_net_pnl']:>10,.0f}"
            f" ${m['max_drawdown_usd']:>10,.0f} ${m['avg_daily_pnl']:>8,.0f}"
        )


def print_combined_summary(fhb_combined: dict, orb_combined: dict, all_combined: dict) -> None:
    sub("COMBINED PORTFOLIO SUMMARY")
    for label, m in [("FHB Strategy", fhb_combined),
                     ("ORB Strategy", orb_combined),
                     ("ALL Strategies", all_combined)]:
        t = m.get("total_trades", 0)
        if t == 0:
            print(f"  {label}: No trades")
            continue
        print(
            f"  {label:<16} | {t:>5} trades | "
            f"Win={m['win_rate_pct']:.1f}% | PF={m['profit_factor']:.2f} | "
            f"P&L=${m['total_pnl']:,.0f} | MDD=${m['max_drawdown']:,.0f} | "
            f"Sharpe={m['sharpe_daily']:.2f} | {m['trades_per_day']:.1f}t/day"
        )


def save_results(
    fhb_by_market: dict,
    orb_by_market: dict,
    fhb_combined:  dict,
    orb_combined:  dict,
    all_combined:  dict,
    all_trades:    list[dict],
) -> Path:
    """Save backtest results to a timestamped JSON in reports/backtests/."""
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Make all values JSON-serialisable
    def _clean(obj):
        if isinstance(obj, float):
            if np.isnan(obj) or np.isinf(obj):
                return None
            return round(obj, 4)
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, dict):
            return {k: _clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_clean(i) for i in obj]
        return obj

    payload = {
        "generated_at":  datetime.now().isoformat(),
        "data_source":   "Sierra Charts",
        "markets_tested": FHB_MARKETS,
        "fhb_by_market": _clean(fhb_by_market),
        "orb_by_market": _clean(orb_by_market),
        "fhb_combined":  _clean(fhb_combined),
        "orb_combined":  _clean(orb_combined),
        "all_combined":  _clean(all_combined),
    }

    out = REPORTS_DIR / f"sc_backtest_{ts}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Also write a fixed-name "latest" file that the dashboard always reads
    latest = REPORTS_DIR / "sc_backtest_latest.json"
    latest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Save trade-level CSV
    if all_trades:
        csv_path = REPORTS_DIR / f"sc_trades_{ts}.csv"
        pd.DataFrame(all_trades).to_csv(csv_path, index=False)
        print(f"\n  Trade CSV  : {csv_path.name}")

    print(f"  Results JSON: {out.name}")
    print(f"  Latest JSON : {latest.name}  (loaded by dashboard)")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    hdr("AlgoBot — Sierra Charts Real-Data Backtest")
    print(f"  Data source : {SC_DIR}")
    print(f"  FHB markets : {FHB_MARKETS}")
    print(f"  ORB markets : {ORB_MARKETS}")
    print(f"  Timeframes  : Daily (HTF bias), 1H (FHB), 5m (ORB)")

    # ── Step 1: Config ─────────────────────────────────────────────────────
    hdr("Step 1: Loading config")
    config = patch_config_for_sc(load_config())
    print("  Config loaded and patched for SC contract specs")

    # ── Step 2: Filters (EconCalendar, VIX, GreenLightScore) ──────────────
    hdr("Step 2: Initialising filters")
    econ_cal   = EconCalendar()
    vix_filter = VIXFilter()
    gls_engine = GreenLightScore(
        full_size_threshold=FHB_GLS_HALF_SCORE,
        half_size_threshold=FHB_GLS_MIN_SCORE,
    )
    print("  EconCalendar  : FOMC + NFP + ECB + CPI events loaded")
    print("  VIXFilter     : QUIET / OPTIMAL / ELEVATED / CRISIS bands")
    print("  GreenLightScore: composite quality gate (0–100)")

    # ── Step 3: Load 1-hour SC data for FHB ───────────────────────────────
    hdr("Step 3: Loading Sierra Charts 1-hour data (FHB)")
    intraday_1h: dict[str, pd.DataFrame] = {}
    for mkt in FHB_MARKETS:
        df = load_sc_1h_rth(mkt)
        if not df.empty:
            n_days = df.index.normalize().nunique()
            print(f"  {mkt}: {len(df):,} bars | {n_days} RTH days | "
                  f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")
            intraday_1h[mkt] = df
        else:
            print(f"  {mkt}: SKIPPED — no 1h data available")

    if not intraday_1h:
        print("\nERROR: No 1-hour SC data found. Check data/sierra_charts/ folder.\n")
        sys.exit(1)

    # ── Step 4: Load 5-minute SC data for ORB ─────────────────────────────
    hdr("Step 4: Loading Sierra Charts 5-minute data (ORB)")
    intraday_5m: dict[str, pd.DataFrame] = {}
    for mkt in ORB_MARKETS:
        df = load_sc_5m_rth(mkt)
        if not df.empty:
            n_days = df.index.normalize().nunique()
            print(f"  {mkt}: {len(df):,} bars | {n_days} RTH days | "
                  f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")
            intraday_5m[mkt] = df
        else:
            print(f"  {mkt}: SKIPPED — no 5m data available")

    # ── Step 5: HTF bias from SC daily data ───────────────────────────────
    hdr("Step 5: Computing HTF bias from SC daily data")
    htf_bias_map:  dict[str, pd.Series] = {}
    regime_map:    dict[str, pd.Series] = {}
    fast_bias_map: dict[str, pd.Series] = {}

    all_markets = list(set(list(intraday_1h.keys()) + list(intraday_5m.keys())))
    for mkt in all_markets:
        bias, regime, fast_bias = get_sc_htf_data(mkt, config)
        htf_bias_map[mkt]  = bias
        regime_map[mkt]    = regime
        fast_bias_map[mkt] = fast_bias
        if len(bias) > 0:
            recent_bias = str(bias.iloc[-1]) if not bias.empty else "N/A"
            print(f"  {mkt}: {len(bias)} daily bars | Latest HTF bias: {recent_bias}")
        else:
            print(f"  {mkt}: HTF bias unavailable — using NEUTRAL for all days")

    # ── Step 6: FHB signals + ATR ─────────────────────────────────────────
    hdr("Step 6: Computing FHB signals on SC 1-hour data")
    atr_by_market: dict[str, pd.Series] = {}
    fhb_signal_dfs: dict[str, pd.DataFrame] = {}

    for mkt, df_1h in intraday_1h.items():
        # ATR
        atr_ser = compute_1h_atr(df_1h, FHB_ATR_PERIOD)
        atr_by_market[mkt] = atr_ser
        recent_atr = float(atr_ser.dropna().iloc[-1]) if len(atr_ser.dropna()) > 0 else 0
        print(f"  {mkt}: ATR(14,1h)={recent_atr:.2f} | stop={FHB_ATR_STOP_MULT}×={recent_atr*FHB_ATR_STOP_MULT:.2f}")

        # Signals
        df_sig = compute_fhb_signals(
            df_1h, mkt,
            htf_bias_series  = htf_bias_map.get(mkt, pd.Series(dtype=str)),
            regime_series    = regime_map.get(mkt, pd.Series(dtype=str)),
            config           = config,
            econ_cal         = econ_cal,
            vix_filter       = vix_filter,
            gls_engine       = gls_engine,
            fast_bias_series = fast_bias_map.get(mkt, pd.Series(dtype=str)),
        )
        fhb_signal_dfs[mkt] = df_sig

        longs   = int(df_sig["fhb_long_signal"].sum())
        shorts  = int(df_sig["fhb_short_signal"].sum())
        blocked = int(df_sig["fhb_htf_blocked"].sum())
        print(f"  {mkt}: {longs} long + {shorts} short signals | {blocked} HTF blocked")

    # ── Step 7: FHB trade simulation ──────────────────────────────────────
    hdr("Step 7: Simulating FHB trades")
    fhb_by_market: dict[str, dict] = {}
    all_fhb_trades: list[dict] = []

    for mkt, df_sig in fhb_signal_dfs.items():
        atr_ser = atr_by_market.get(mkt, pd.Series(dtype=float))
        trades  = simulate_fhb_trades(
            df_sig, atr_ser, mkt, config,
            use_atr_stop=True, trail_be=True,
            overnight_carry=FHB_OVERNIGHT_CARRY,
            label="SC_FHB", db=None,
        )
        all_fhb_trades.extend(trades)
        m = fhb_metrics(trades, mkt)
        fhb_by_market[mkt] = m
        print(
            f"  {mkt}: {m['total_trades']} trades | "
            f"PF={m['profit_factor']:.2f} | Win={m['win_rate_pct']:.1f}% | "
            f"P&L=${m['total_net_pnl']:,.0f} | MDD=${m['max_drawdown_usd']:,.0f}"
        )

    hdr("Step 8: Computing ORB signals on SC 5-minute data")
    orb_by_market: dict[str, dict] = {}
    all_orb_trades: list[dict] = []

    for mkt, df_5m in intraday_5m.items():
        htf_bias = htf_bias_map.get(mkt, pd.Series(dtype=str))

        # compute_orb_signals expects a simple bias Series indexed by date
        df_orb = compute_orb_signals(df_5m, mkt, htf_bias, config)

        longs   = int(df_orb.get("orb_long_signal",  pd.Series()).sum())
        shorts  = int(df_orb.get("orb_short_signal", pd.Series()).sum())
        blocked = int(df_orb.get("orb_htf_blocked",  pd.Series()).sum())
        print(f"  {mkt}: {longs} long + {shorts} short ORB signals | {blocked} HTF blocked")

        trades = simulate_orb_trades(df_orb, mkt, config)
        all_orb_trades.extend(trades)

        if trades:
            t = pd.DataFrame(trades)
            total = len(t)
            wins  = int(t["is_win"].sum()) if "is_win" in t.columns else 0
            gw    = t.loc[t["pnl_net"] > 0,  "pnl_net"].sum() if "pnl_net" in t.columns else 0
            gl    = abs(t.loc[t["pnl_net"] <= 0, "pnl_net"].sum()) if "pnl_net" in t.columns else 1
            pf    = gw / gl if gl > 0 else float("inf")
            pnl   = float(t["pnl_net"].sum()) if "pnl_net" in t.columns else 0
            wr    = wins / total * 100 if total > 0 else 0
            daily = t.groupby("date")["pnl_net"].sum() if "pnl_net" in t.columns and "date" in t.columns else pd.Series([0])
            cum   = daily.cumsum()
            mdd   = float((cum - cum.cummax()).min())
            orb_by_market[mkt] = {
                "market": mkt, "total_trades": total,
                "win_rate_pct": round(wr, 1),
                "profit_factor": round(pf, 2),
                "total_net_pnl": round(pnl, 2),
                "max_drawdown_usd": round(mdd, 2),
                "avg_daily_pnl": round(float(daily.mean()), 2),
            }
            print(
                f"  {mkt}: {total} trades | "
                f"PF={pf:.2f} | Win={wr:.1f}% | "
                f"P&L=${pnl:,.0f} | MDD=${mdd:,.0f}"
            )
        else:
            orb_by_market[mkt] = {"market": mkt, "total_trades": 0}
            print(f"  {mkt}: 0 trades")

    # ── Step 9: Combined metrics ───────────────────────────────────────────
    all_trades     = all_fhb_trades + all_orb_trades
    fhb_combined   = combined_metrics(all_fhb_trades, "FHB (SC)")
    orb_combined   = combined_metrics(all_orb_trades, "ORB (SC)")
    all_combined   = combined_metrics(all_trades,     "ALL (SC)")

    # ── Step 10: Report ────────────────────────────────────────────────────
    hdr("RESULTS — FHB Strategy (Sierra Charts Data)")
    print_market_table(fhb_by_market)

    hdr("RESULTS — ORB Strategy (Sierra Charts Data)")
    print_market_table(orb_by_market)

    print_combined_summary(fhb_combined, orb_combined, all_combined)

    # ── Step 11: Year-by-year ─────────────────────────────────────────────
    hdr("Year-by-Year Breakdown (FHB, all SC markets)")
    for mkt in FHB_MARKETS:
        mkt_trades = [t for t in all_fhb_trades if t.get("market") == mkt]
        yearly_breakdown(mkt_trades, mkt)

    # ── Step 12: Profitability verdict ────────────────────────────────────
    hdr("PROFITABILITY VERDICT — Sierra Charts Real Data")
    targets = {"profit_factor": 1.5, "win_rate_pct": 50.0, "total_trades": 20}
    pass_count = 0
    checks = [
        ("FHB Combined PF",   fhb_combined.get("profit_factor", 0), targets["profit_factor"], "≥"),
        ("ORB Combined PF",   orb_combined.get("profit_factor", 0), targets["profit_factor"], "≥"),
        ("ALL Combined PF",   all_combined.get("profit_factor", 0), targets["profit_factor"], "≥"),
        ("FHB Win Rate %",    fhb_combined.get("win_rate_pct", 0),  targets["win_rate_pct"], "≥"),
        ("FHB Trade Count",   fhb_combined.get("total_trades", 0),  targets["total_trades"], "≥"),
        ("FHB Max DD < $10k", abs(fhb_combined.get("max_drawdown", 0)), 10_000, "≤"),
    ]
    for name, val, threshold, op in checks:
        if op == "≥":
            passed = val >= threshold
        else:
            passed = val <= threshold
        icon = "PASS" if passed else "FAIL"
        if passed:
            pass_count += 1
        print(f"  [{icon}] {name:<28} = {val:.2f}  (target {op} {threshold})")

    verdict = "READY FOR PAPER TRADING" if pass_count >= 4 else "NEEDS MORE TUNING"
    print(f"\n  {pass_count}/{len(checks)} checks passed — {verdict}")

    # ── Step 13: Save results ─────────────────────────────────────────────
    hdr("Saving Results")
    save_results(
        fhb_by_market, orb_by_market,
        fhb_combined, orb_combined, all_combined,
        all_trades,
    )
    print()


if __name__ == "__main__":
    main()
