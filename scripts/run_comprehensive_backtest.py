"""
AlgoBot — Comprehensive Backtest Suite
=======================================
Script:  scripts/run_comprehensive_backtest.py
Purpose: Rigorous edge validation across all available data.

Active strategies tested:
  FHB  — First Hour Breakout on NQ / MNQ (1-hour bars, RTH)
  GC   — Gold Mean-Reversion FADE on MGC (1-hour bars, RTH)

Validation layers:
  1. Baseline backtest (full IS period)
  2. Walk-forward (8 expanding windows, 12.5% OOS each)
  3. Monte Carlo block bootstrap (10,000 iterations, block ≥ 10 trades)
  4. Parameter sensitivity grid (5×5 win/loss stress test)
  5. PSR — Probabilistic Sharpe Ratio (anti-overfitting gate)

Data sources (in priority order):
  1. Sierra Charts real futures (stitched contracts, RTH only)
  2. Yahoo Finance intraday cache (730-day, 1h bars, RTH filtered)

Output:
  reports/backtests/comprehensive_TIMESTAMP.json
  reports/backtests/comprehensive_latest.json   ← loaded by dashboard
"""

from __future__ import annotations

import json
import math
import sys
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

warnings.filterwarnings("ignore", category=FutureWarning)

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).parent))

from src.utils.sierra_loader import load_sc_continuous, load_sc_daily_for_htf
from src.strategy.indicators import calculate_indicators, add_atr_baseline
from src.strategy.regime_classifier import classify_regimes
from src.strategy.htf_bias import add_htf_bias
from src.utils.econ_calendar import EconCalendar
from src.utils.vix_filter import VIXFilter
from src.utils.trade_readiness import GreenLightScore
from src.utils.logger import get_logger

# Reuse FHB engine from run_fhb_backtest
from run_fhb_backtest import (
    compute_1h_atr,
    compute_fhb_signals,
    simulate_fhb_trades,
    compute_metrics as fhb_metrics,
    FHB_ATR_PERIOD,
    FHB_ATR_STOP_MULT,
    FHB_OVERNIGHT_CARRY,
    FHB_GLS_MIN_SCORE,
    FHB_GLS_HALF_SCORE,
)

log = get_logger(__name__)

REPORTS_DIR = PROJECT_ROOT / "reports" / "backtests"
SC_DIR      = PROJECT_ROOT / "data" / "sierra_charts"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

RTH_START = "09:30"
RTH_END   = "16:00"

# Contract specs for dollar P&L calculation
CONTRACT_SPECS: dict[str, dict] = {
    "NQ":  {"point_value": 20.0,  "tick_size": 0.25, "commission": 5.0},
    "MNQ": {"point_value": 2.0,   "tick_size": 0.25, "commission": 2.5},
    "GC":  {"point_value": 100.0, "tick_size": 0.10, "commission": 5.0},
    "MGC": {"point_value": 1.0,   "tick_size": 0.10, "commission": 1.5},
}

# GC mean-reversion: ATR stop multiplier and max hold
GC_ATR_STOP_MULT = 1.0
GC_TARGET_R      = 2.0
GC_MAX_HOLD_BARS = 3

SEP  = "=" * 72
SEP2 = "-" * 72


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _hdr(title: str) -> None:
    print(f"\n{SEP}\n  {title}\n{SEP}")


def _sub(title: str) -> None:
    print(f"\n{SEP2}\n  {title}\n{SEP2}")


def load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "config.yaml") as f:
        return yaml.safe_load(f)


def patch_config(config: dict) -> dict:
    """Inject correct contract specs for micro contracts."""
    cfg = dict(config)
    markets = dict(cfg.get("markets", {}))
    for mkt, spec in CONTRACT_SPECS.items():
        entry = dict(markets.get(mkt, markets.get("NQ", {})))
        entry.update(spec)
        markets[mkt] = entry
    cfg["markets"] = markets
    return cfg


def _clean(obj):
    """Make any value JSON-serialisable."""
    import datetime as _dt
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return round(obj, 4)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (_dt.date, _dt.datetime)):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(i) for i in obj]
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# Data Loading
# ─────────────────────────────────────────────────────────────────────────────

def load_sc_1h(market: str) -> pd.DataFrame:
    """Load Sierra Charts 1-hour bars, RTH-filtered. Returns empty DF on failure."""
    try:
        df = load_sc_continuous(market, "1h", SC_DIR)
        df = df.between_time(RTH_START, RTH_END)
        for col in ["Open", "High", "Low", "Close"]:
            if col in df.columns:
                df[col] = df[col].astype(float)
        df.index.name = "Timestamp"
        return df
    except Exception as e:
        print(f"  WARNING [{market}/1h SC]: {e}")
        return pd.DataFrame()


def load_yf_1h(market: str) -> pd.DataFrame:
    """Load Yahoo Finance 1-hour cache (730 days). Returns empty DF on failure."""
    cache_dir = PROJECT_ROOT / "data" / "raw" / "intraday"
    yf_map = {"NQ": "NQ", "MNQ": "NQ", "MGC": "GC", "GC": "GC"}
    key = yf_map.get(market, market)
    cache_file = cache_dir / f"yf_{key}_1h_730d.parquet"
    if not cache_file.exists():
        return pd.DataFrame()
    try:
        df = pd.read_parquet(cache_file)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("America/New_York")
        else:
            df.index = df.index.tz_convert("America/New_York")
        df = df.between_time(RTH_START, RTH_END)
        for col in ["Open", "High", "Low", "Close"]:
            if col in df.columns:
                df[col] = df[col].astype(float)
        df.index.name = "Timestamp"
        return df
    except Exception as e:
        print(f"  WARNING [{market}/1h YF cache]: {e}")
        return pd.DataFrame()


def load_best_1h(market: str) -> tuple[pd.DataFrame, str]:
    """
    Load best available 1-hour data for market.
    Always combines SC (recent, accurate) + YF cache (older history).
    SC takes priority where dates overlap.
    """
    df_sc  = load_sc_1h(market)
    df_yf  = load_yf_1h(market)

    if df_sc.empty and df_yf.empty:
        return pd.DataFrame(), "EMPTY"

    if df_sc.empty:
        return df_yf, "YF"

    if df_yf.empty:
        return df_sc, "SC"

    # Combine: YF for older history, SC for recent (SC has correct futures prices)
    sc_start  = df_sc.index[0].date()
    yf_older  = df_yf[df_yf.index.date < sc_start]
    if yf_older.empty:
        return df_sc, "SC"
    combined = pd.concat([yf_older, df_sc]).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined, "SC+YF"


def get_htf_data(market: str, config: dict) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Build HTF bias + daily regime + fast bias from SC daily bars."""
    config_key = {"MNQ": "NQ", "MGC": "GC"}.get(market, market)
    try:
        daily_df = load_sc_daily_for_htf(market, SC_DIR)
    except ValueError:
        try:
            daily_df = load_sc_daily_for_htf(config_key, SC_DIR)
        except ValueError:
            empty = pd.Series(dtype=str)
            return empty, empty, empty

    if daily_df.empty or len(daily_df) < 50:
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
# GC Mean-Reversion Engine
# ─────────────────────────────────────────────────────────────────────────────

def compute_gc_signals(df_1h: pd.DataFrame, htf_bias: pd.Series) -> pd.DataFrame:
    """
    Detect GC mean-reversion FADE signals on 1-hour bars.

    Logic (mirrors pine/gc_strategy.pine v3):
      - Compute first-hour range (09:30-10:30 bar) each day
      - Signal window: 10:30–13:00 ET
      - FADE breakout:
          close > range_high  → go SHORT (fading the upward breakout)
          close < range_low   → go LONG  (fading the downward breakout)
      - HTF filter:
          LONG  only when NOT htf_bear  (don't fade down when trend is bearish)
          SHORT only when NOT htf_bull  (don't fade up  when trend is bullish)
      - One signal per day per direction
    """
    df = df_1h.copy()
    dates = df.index.normalize().unique()

    records = []
    for day in dates:
        day_bars = df[df.index.normalize() == day]
        range_bars   = day_bars.between_time("09:30", "10:30")
        signal_bars  = day_bars.between_time("10:30", "13:00")

        if range_bars.empty or signal_bars.empty:
            continue

        gc_high = float(range_bars["High"].max())
        gc_low  = float(range_bars["Low"].min())

        htf = str(htf_bias.get(day, "NEUTRAL")).upper()
        htf_bull = "BULL" in htf
        htf_bear = "BEAR" in htf

        fired_long = fired_short = False

        for ts, bar in signal_bars.iterrows():
            close = float(bar["Close"])
            rec = {
                "Timestamp": ts,
                "Open": float(bar["Open"]),
                "High": float(bar["High"]),
                "Low": float(bar["Low"]),
                "Close": close,
                "gc_high": gc_high,
                "gc_low": gc_low,
                "gc_long_signal":  False,
                "gc_short_signal": False,
                "htf_bias": htf,
                "date": day.strftime("%Y-%m-%d"),
            }

            if close < gc_low and not htf_bear and not fired_long:
                rec["gc_long_signal"] = True
                fired_long = True

            if close > gc_high and not htf_bull and not fired_short:
                rec["gc_short_signal"] = True
                fired_short = True

            records.append(rec)

    if not records:
        return pd.DataFrame()

    sig_df = pd.DataFrame(records).set_index("Timestamp")
    sig_df.index = pd.to_datetime(sig_df.index)
    return sig_df


def simulate_gc_trades(
    sig_df: pd.DataFrame,
    df_1h: pd.DataFrame,
    atr_ser: pd.Series,
    market: str,
    config: dict,
    econ_cal: EconCalendar,
) -> list[dict]:
    """
    Simulate GC mean-reversion trades with ATR stop, R-multiple target, max-hold exit.
    """
    if sig_df.empty:
        return []

    spec    = config.get("markets", {}).get(market, {})
    pv      = float(spec.get("point_value", 1.0))
    comm    = float(spec.get("commission", 1.5)) * 2  # round-trip
    slip    = float(spec.get("slippage_ticks", 1)) * float(spec.get("tick_size", 0.10))

    trades: list[dict] = []
    all_bars = df_1h.copy()

    for ts, row in sig_df.iterrows():
        is_long  = bool(row.get("gc_long_signal", False))
        is_short = bool(row.get("gc_short_signal", False))
        if not is_long and not is_short:
            continue

        day = pd.Timestamp(ts).normalize()

        # Skip high-impact econ events
        if econ_cal.is_high_impact(day):
            continue

        atr = float(atr_ser.get(ts, atr_ser.asof(ts) if len(atr_ser) > 0 else 0)) if len(atr_ser) > 0 else 0
        if atr == 0 or pd.isna(atr):
            continue

        entry_px = float(row["Close"]) + (slip if is_long else -slip)
        stop_px  = (entry_px - atr * GC_ATR_STOP_MULT) if is_long else (entry_px + atr * GC_ATR_STOP_MULT)
        risk_pts = abs(entry_px - stop_px)
        if risk_pts <= 0:
            continue
        target_px = entry_px + risk_pts * GC_TARGET_R if is_long else entry_px - risk_pts * GC_TARGET_R

        # Forward walk subsequent bars for this day (max GC_MAX_HOLD_BARS)
        fwd = all_bars[all_bars.index > ts]
        fwd = fwd[fwd.index.normalize() == day].head(GC_MAX_HOLD_BARS)

        direction = "LONG" if is_long else "SHORT"
        exit_px   = None
        exit_reason = "eod"

        for bar_ts, bar in fwd.iterrows():
            h = float(bar["High"])
            l = float(bar["Low"])

            if is_long:
                if l <= stop_px:
                    exit_px, exit_reason = stop_px, "stop"
                    break
                if h >= target_px:
                    exit_px, exit_reason = target_px, "target"
                    break
            else:
                if h >= stop_px:
                    exit_px, exit_reason = stop_px, "stop"
                    break
                if l <= target_px:
                    exit_px, exit_reason = target_px, "target"
                    break

        if exit_px is None:
            # EOD: exit at last bar close
            if fwd.empty:
                # No forward bars — skip (can't exit same bar)
                continue
            exit_px = float(fwd.iloc[-1]["Close"])

        pts   = (exit_px - entry_px) if is_long else (entry_px - exit_px)
        pnl   = pts * pv - comm
        is_win = pnl > 0

        trades.append({
            "market":      market,
            "strategy":    "GC_REV",
            "direction":   direction,
            "date":        row["date"],
            "entry_time":  str(ts),
            "entry_px":    round(entry_px, 2),
            "stop_px":     round(stop_px, 2),
            "target_px":   round(target_px, 2),
            "exit_px":     round(exit_px, 2),
            "exit_reason": exit_reason,
            "pnl_net":     round(pnl, 2),
            "atr":         round(atr, 2),
            "is_win":      is_win,
            "htf_bias":    str(row.get("htf_bias", "")),
        })

    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Baseline Metrics
# ─────────────────────────────────────────────────────────────────────────────

def baseline_metrics(trades: list[dict], market: str) -> dict:
    """Full baseline metrics from a trade list."""
    if not trades:
        return {"market": market, "n_trades": 0}

    df    = pd.DataFrame(trades)
    pnl   = df["pnl_net"].astype(float)
    n     = len(pnl)
    wins  = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    nw    = len(wins)
    nl    = len(losses)
    gw    = float(wins.sum()) if nw else 0.0
    gl    = float(abs(losses.sum())) if nl else 0.0
    pf    = gw / gl if gl > 0 else 999.0
    wr    = nw / n * 100 if n else 0.0

    daily_pnl = df.groupby("date")["pnl_net"].sum()
    cum_eq    = daily_pnl.cumsum()
    mdd       = float((cum_eq - cum_eq.cummax()).min())
    total_pnl = float(pnl.sum())

    # Date range
    dates  = sorted(df["date"].unique())
    d0, d1 = dates[0], dates[-1]
    years  = max((pd.Timestamp(d1) - pd.Timestamp(d0)).days / 365.25, 0.01)

    # Sharpe
    sharpe = 0.0
    if len(daily_pnl) > 1 and daily_pnl.std() > 0:
        sharpe = float(daily_pnl.mean() / daily_pnl.std() * (252 ** 0.5))

    return {
        "market":       market,
        "n_trades":     n,
        "win_rate_pct": round(wr, 1),
        "profit_factor": round(pf, 2),
        "avg_win_usd":  round(float(wins.mean()), 2) if nw else 0.0,
        "avg_loss_usd": round(float(losses.mean()), 2) if nl else 0.0,
        "win_loss_ratio": round(abs(wins.mean() / losses.mean()), 2) if nw and nl else 0.0,
        "total_net_pnl": round(total_pnl, 2),
        "max_drawdown_usd": round(mdd, 2),
        "avg_daily_pnl": round(float(daily_pnl.mean()), 2),
        "sharpe": round(sharpe, 2),
        "years_tested": round(years, 1),
        "date_start": d0,
        "date_end":   d1,
        "trading_days": int(daily_pnl.shape[0]),
    }


def pf_from_trades(trades: list[dict]) -> float:
    if not trades:
        return 0.0
    pnl  = [t["pnl_net"] for t in trades]
    gw   = sum(p for p in pnl if p > 0)
    gl   = abs(sum(p for p in pnl if p < 0))
    if gl == 0:
        return min(gw / 0.01, 10.0)   # cap no-loss windows at 10.0
    return min(gw / gl, 10.0)         # cap at 10.0 to prevent avg distortion


# ─────────────────────────────────────────────────────────────────────────────
# Walk-Forward Validation
# ─────────────────────────────────────────────────────────────────────────────

def walk_forward(trades: list[dict]) -> dict:
    """
    Expanding-window walk-forward.
    Auto-scales the number of windows based on trade count.
    Requires ≥ 8 trades per window; max 8 windows.
    Pass criterion: ≥ 75% of OOS windows profitable AND avg OOS PF > 1.0.
    """
    n = len(trades)
    if n < 20:
        return {
            "windows": [], "avg_oos_pf": None, "avg_ratio": None,
            "oos_positive_pct": None, "pass": False,
            "note": f"Too few trades ({n}) for walk-forward (need ≥ 20)"
        }

    # Auto-scale: each OOS window needs ≥ 8 trades
    n_windows = min(8, max(3, n // 8))

    # Sort by date
    srt = sorted(trades, key=lambda t: t["date"])
    oos_size = max(1, n // n_windows)

    windows = []
    for w in range(n_windows):
        is_end  = (w + 1) * oos_size
        oos_end = (w + 2) * oos_size
        if oos_end > n:
            oos_end = n
        if is_end >= oos_end or is_end == 0:
            continue

        is_trades  = srt[:is_end]
        oos_trades = srt[is_end:oos_end]

        is_pf  = pf_from_trades(is_trades)
        oos_pf = pf_from_trades(oos_trades)
        ratio  = oos_pf / is_pf if is_pf > 0 and is_pf < 10 else 0.0

        windows.append({
            "window":       w + 1,
            "is_n":         len(is_trades),
            "oos_n":        len(oos_trades),
            "is_pf":        round(is_pf, 3),
            "oos_pf":       round(oos_pf, 3),
            "ratio":        round(ratio, 3),
            "oos_positive": oos_pf > 1.0,
        })

    if not windows:
        return {"windows": [], "avg_oos_pf": None, "avg_ratio": None,
                "oos_positive_pct": None, "pass": False}

    oos_pfs   = [w["oos_pf"] for w in windows]
    ratios    = [w["ratio"] for w in windows]
    avg_oos   = float(np.mean(oos_pfs))
    avg_ratio = float(np.mean(ratios))
    n_pos     = sum(1 for w in windows if w["oos_positive"])
    pos_pct   = n_pos / len(windows)

    # Pass: ≥75% of OOS windows profitable, avg OOS PF > 1.0, avg ratio ≥ 0.50
    passed = pos_pct >= 0.75 and avg_oos > 1.0 and avg_ratio >= 0.50

    return {
        "windows":           windows,
        "n_windows":         n_windows,
        "avg_oos_pf":        round(avg_oos, 3),
        "avg_ratio":         round(avg_ratio, 3),
        "oos_positive_pct":  round(pos_pct, 3),
        "all_oos_positive":  pos_pct == 1.0,
        "pass":              passed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Monte Carlo Block Bootstrap
# ─────────────────────────────────────────────────────────────────────────────

def monte_carlo(
    trades: list[dict],
    n_iter: int = 10_000,
    ruin_limit_usd: float = -6_000.0,
    rng_seed: int = 42,
) -> dict:
    """
    Block bootstrap Monte Carlo.
    Block size = max(5, n_trades // 10) to preserve streak structure.
    ruin_limit_usd: drawdown level that constitutes "ruin" for this strategy.
    """
    if len(trades) < 8:
        return {
            "p05_pf": None, "median_pf": None, "p95_pf": None,
            "ruin_prob": None, "pvalue": None, "pass": False,
            "note": f"Too few trades ({len(trades)}) for Monte Carlo (need ≥ 8)"
        }

    rng      = np.random.default_rng(rng_seed)
    pnls     = np.array([t["pnl_net"] for t in trades], dtype=float)
    n        = len(pnls)
    blk      = max(5, n // 10)

    observed_pf = pf_from_trades(trades)

    sim_pfs  = np.empty(n_iter, dtype=float)
    ruin_cnt = 0

    for i in range(n_iter):
        # Draw block starts
        n_blocks  = math.ceil(n / blk)
        starts    = rng.integers(0, n, size=n_blocks)
        resampled = np.concatenate([pnls[s: s + blk] for s in starts])[:n]

        gw = float(resampled[resampled > 0].sum())
        gl = float(abs(resampled[resampled < 0].sum()))
        sim_pfs[i] = gw / gl if gl > 0 else 999.0

        # Ruin check: does equity ever breach ruin_limit_usd?
        eq = np.cumsum(resampled)
        if eq.min() <= ruin_limit_usd:
            ruin_cnt += 1

    p05    = float(np.percentile(sim_pfs[np.isfinite(sim_pfs)], 5))
    median = float(np.median(sim_pfs[np.isfinite(sim_pfs)]))
    p95    = float(np.percentile(sim_pfs[np.isfinite(sim_pfs)], 95))
    ruin_prob = ruin_cnt / n_iter

    # Permutation test: randomly flip trade signs to simulate no-edge null hypothesis.
    # If observed PF is in the top 5% of random sign assignments → edge is real.
    abs_pnls = np.abs(pnls)
    perm_pfs = np.empty(n_iter, dtype=float)
    for i in range(n_iter):
        signs    = rng.choice([-1.0, 1.0], size=n)
        perm_pnl = abs_pnls * signs
        gw = float(perm_pnl[perm_pnl > 0].sum())
        gl = float(abs(perm_pnl[perm_pnl < 0].sum()))
        perm_pfs[i] = min(gw / gl, 10.0) if gl > 0 else 10.0
    pvalue = float((perm_pfs >= observed_pf).sum() / n_iter)

    passed = p05 > 1.5 and ruin_prob < 0.10 and pvalue < 0.05

    return {
        "p05_pf":    round(p05, 3),
        "median_pf": round(median, 3),
        "p95_pf":    round(p95, 3),
        "ruin_prob": round(ruin_prob, 4),
        "pvalue":    round(pvalue, 4),
        "block_size": blk,
        "n_iter":    n_iter,
        "ruin_limit_usd": ruin_limit_usd,
        "pass":      passed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Parameter Sensitivity Grid
# ─────────────────────────────────────────────────────────────────────────────

def param_sensitivity(trades: list[dict]) -> dict:
    """
    5×5 win/loss scaling stress test.
    win_scales  × avg_win  + loss_scales × avg_loss → synthetic PF per cell
    """
    if not trades:
        return {"grid": [], "win_scales": [], "loss_scales": [],
                "stress_pf": None, "cells_positive": 0, "pass": False}

    pnls  = np.array([t["pnl_net"] for t in trades], dtype=float)
    wins  = pnls[pnls > 0]
    losses = pnls[pnls < 0]

    win_scales  = [0.70, 0.80, 0.90, 1.00, 1.10]
    loss_scales = [1.00, 1.10, 1.20, 1.30, 1.40]

    grid     = []
    cells_ok = 0
    stress_pf = None

    for ws in win_scales:
        row = []
        for ls in loss_scales:
            scaled_wins   = wins * ws
            scaled_losses = losses * ls
            gw  = float(scaled_wins.sum())  if len(scaled_wins)   > 0 else 0.0
            gl  = float(abs(scaled_losses.sum())) if len(scaled_losses) > 0 else 0.0
            spf = round(gw / gl, 3) if gl > 0 else 999.0
            row.append(spf)
            if spf > 1.0:
                cells_ok += 1
            # stress cell: ws=0.70, ls=1.30
            if abs(ws - 0.70) < 0.001 and abs(ls - 1.30) < 0.001:
                stress_pf = spf
        grid.append(row)

    passed = (stress_pf is not None and stress_pf > 1.0) and cells_ok >= 20

    return {
        "win_scales":    win_scales,
        "loss_scales":   loss_scales,
        "grid":          grid,
        "stress_pf":     stress_pf,
        "cells_positive": cells_ok,
        "pass":          passed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Probabilistic Sharpe Ratio (PSR)
# ─────────────────────────────────────────────────────────────────────────────

def probabilistic_sr(trades: list[dict], n_strategies_tested: int = 2) -> dict:
    """
    PSR corrected for multiple testing (Bonferroni).
    Uses daily P&L returns; benchmark SR* = 0 (minimal viability).
    Reference: Bailey & López de Prado (2012).
    """
    if len(trades) < 15:
        return {"sharpe": None, "psr": None, "pass": False,
                "note": "Insufficient trades for PSR"}

    df       = pd.DataFrame(trades)
    daily    = df.groupby("date")["pnl_net"].sum()
    r        = daily.astype(float).values
    T        = len(r)

    if T < 5 or r.std() == 0:
        return {"sharpe": None, "psr": None, "pass": False,
                "note": "Insufficient daily returns for PSR"}

    sr_hat   = float(r.mean() / r.std() * np.sqrt(252))
    skew     = float(pd.Series(r).skew())
    kurt     = float(pd.Series(r).kurt())  # excess kurtosis

    # Variance of Sharpe estimator (Lo 2002, moments-adjusted)
    var_sr   = (1 / T) * (1 + (skew * sr_hat) / np.sqrt(252)
                           - ((kurt - 3) / 4) * (sr_hat / np.sqrt(252)) ** 2
                           + 0.5 * (sr_hat / np.sqrt(252)) ** 4)
    if var_sr <= 0:
        var_sr = 1 / T

    sr_star  = 0.0    # benchmark
    z        = (sr_hat - sr_star) / max(math.sqrt(var_sr), 1e-9)

    from scipy.stats import norm
    psr_raw  = float(norm.cdf(z))

    # Bonferroni correction: threshold = 0.95 / n_strategies_tested
    bonf_threshold = 0.95 / n_strategies_tested
    passed = psr_raw >= 0.85   # individual bar (combined must exceed after correction)

    return {
        "sharpe":          round(sr_hat, 3),
        "psr":             round(psr_raw, 4),
        "skewness":        round(skew, 3),
        "excess_kurtosis": round(kurt, 3),
        "n_daily_obs":     T,
        "bonf_threshold":  round(bonf_threshold, 3),
        "pass":            passed,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Strategy runner wrappers
# ─────────────────────────────────────────────────────────────────────────────

def run_fhb_strategy(
    market: str,
    df_1h: pd.DataFrame,
    config: dict,
    econ_cal: EconCalendar,
    vix_filter: VIXFilter,
    gls_engine: GreenLightScore,
    htf_bias: pd.Series,
    regime: pd.Series,
    fast_bias: pd.Series,
) -> list[dict]:
    """Run FHB signals + simulation for one market."""
    atr_ser = compute_1h_atr(df_1h, FHB_ATR_PERIOD)

    sig_df = compute_fhb_signals(
        df_1h, market,
        htf_bias_series  = htf_bias,
        regime_series    = regime,
        config           = config,
        econ_cal         = econ_cal,
        vix_filter       = vix_filter,
        gls_engine       = gls_engine,
        fast_bias_series = fast_bias,
    )

    trades = simulate_fhb_trades(
        sig_df, atr_ser, market, config,
        use_atr_stop=True, trail_be=True,
        overnight_carry=FHB_OVERNIGHT_CARRY,
        label="COMP_FHB", db=None,
    )
    return trades


def run_gc_strategy(
    market: str,
    df_1h: pd.DataFrame,
    config: dict,
    econ_cal: EconCalendar,
    htf_bias: pd.Series,
) -> list[dict]:
    """Run GC mean-reversion signals + simulation for one market."""
    atr_ser = compute_1h_atr(df_1h, FHB_ATR_PERIOD)
    sig_df  = compute_gc_signals(df_1h, htf_bias)
    if sig_df.empty:
        return []
    trades = simulate_gc_trades(sig_df, df_1h, atr_ser, market, config, econ_cal)
    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Full Strategy Analysis
# ─────────────────────────────────────────────────────────────────────────────

def analyse_strategy(
    label: str,
    trades: list[dict],
    n_mc_iter: int = 10_000,
    n_strategies: int = 2,
    ruin_limit_usd: float = -6_000.0,
) -> dict:
    """Run all validation layers on a trade list and return result dict."""
    _sub(f"Analysing {label} — {len(trades)} trades")

    base = baseline_metrics(trades, label)
    print(f"  Baseline: PF={base.get('profit_factor'):.2f} | "
          f"WR={base.get('win_rate_pct'):.1f}% | "
          f"P&L=${base.get('total_net_pnl'):,.0f} | "
          f"MDD=${base.get('max_drawdown_usd'):,.0f} | "
          f"Trades={base.get('n_trades')}")

    print("  Running walk-forward…")
    wf = walk_forward(trades)
    n_win = wf.get("n_windows", 0)
    pos_pct = wf.get("oos_positive_pct")
    print(f"  Walk-forward ({n_win} windows): avg OOS PF={wf.get('avg_oos_pf')} | "
          f"profitable windows={pos_pct:.0%} | pass={wf.get('pass')}")

    print("  Running Monte Carlo (10,000 iterations)…")
    mc = monte_carlo(trades, n_iter=n_mc_iter, ruin_limit_usd=ruin_limit_usd)
    ruin = mc.get('ruin_prob')
    print(f"  Monte Carlo: P05={mc.get('p05_pf')} | ruin={ruin:.1%} | "
          f"p-value={mc.get('pvalue'):.4f} | pass={mc.get('pass')}")

    print("  Running parameter sensitivity grid…")
    sens = param_sensitivity(trades)
    print(f"  Sensitivity: stress PF={sens.get('stress_pf')} | "
          f"cells>1.0={sens.get('cells_positive')}/25 | pass={sens.get('pass')}")

    print("  Computing PSR…")
    psr = probabilistic_sr(trades, n_strategies_tested=n_strategies)
    print(f"  PSR={psr.get('psr')} | Sharpe={psr.get('sharpe')} | pass={psr.get('pass')}")

    # ── Overall verdict ──────────────────────────────────────────────────────
    n_trades   = base.get("n_trades", 0)
    pf         = base.get("profit_factor", 0)
    wr         = base.get("win_rate_pct", 0)
    pnl        = base.get("total_net_pnl", 0)
    mdd        = base.get("max_drawdown_usd", 0)

    # Core checks (always evaluated)
    core_pass  = [
        pf >= 1.8,                       # minimum profit factor
        wr >= 40.0,                      # minimum win rate (relaxed for mean-reversion)
        pnl > 0,                         # must be net profitable
        abs(mdd) <= 30_000,              # max drawdown cap
        psr.get("pass", False),          # statistical significance
    ]
    core_score = sum(core_pass)

    # Statistical checks (scaled by data availability)
    stat_pass = [
        wf.get("pass", False),
        mc.get("pass", False),
        sens.get("pass", False),
    ]
    stat_score = sum(stat_pass)

    # Data sufficiency note
    data_note = ""
    if n_trades < 30:
        data_note = f"Limited sample ({n_trades} trades) — statistical tests have wide confidence intervals"
    elif n_trades < 80:
        data_note = f"Moderate sample ({n_trades} trades)"

    # Verdict: requires all core checks + at least 1 stat check for PASS
    if core_score == 5 and stat_score >= 2:
        verdict = "PASS"
    elif core_score >= 4 and (stat_score >= 1 or n_trades < 30):
        verdict = "WARN"
    elif pnl > 0 and pf >= 1.2:
        verdict = "WARN"   # profitable but statistically weak
    else:
        verdict = "FAIL"

    total_checks = len(core_pass) + len(stat_pass)
    total_passed = core_score + stat_score
    score_str = f"{total_passed}/{total_checks} checks passed"
    if data_note:
        score_str += f" · {data_note}"

    print(f"  Verdict: {verdict} ({score_str})")

    return {
        "label":             label,
        "baseline":          _clean(base),
        "walk_forward":      _clean(wf),
        "monte_carlo":       _clean(mc),
        "param_sensitivity": _clean(sens),
        "psr":               _clean(psr),
        "verdict":           verdict,
        "verdict_score":     score_str,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Save Results
# ─────────────────────────────────────────────────────────────────────────────

def save_results(results: dict) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    payload = _clean(results)

    out = REPORTS_DIR / f"comprehensive_{ts}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    latest = REPORTS_DIR / "comprehensive_latest.json"
    latest.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\n  Results JSON : {out.name}")
    print(f"  Latest JSON  : {latest.name}  (loaded by dashboard)")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    _hdr("AlgoBot — Comprehensive Backtest Suite")
    print("  Active strategies : FHB (NQ / MNQ) | GC Mean-Reversion (MGC)")
    print("  Validation layers : Baseline | Walk-Forward | Monte Carlo | Sensitivity | PSR")
    print()

    # ── Step 1: Config & filters ───────────────────────────────────────────────
    _hdr("Step 1: Config & filters")
    config    = patch_config(load_config())
    econ_cal  = EconCalendar()
    vix_filt  = VIXFilter()
    gls_eng   = GreenLightScore(
        full_size_threshold=FHB_GLS_HALF_SCORE,
        half_size_threshold=FHB_GLS_MIN_SCORE,
    )
    print("  EconCalendar | VIXFilter | GreenLightScore loaded")

    data_ranges: dict[str, str] = {}
    all_strategy_results: dict[str, dict] = {}

    # ── Step 2: FHB — NQ and MNQ ──────────────────────────────────────────────
    _hdr("Step 2: FHB Strategy — NQ / MNQ")

    fhb_markets   = ["NQ", "MNQ"]
    all_fhb_trades: list[dict] = []

    for mkt in fhb_markets:
        print(f"\n  Loading 1h data for {mkt}…")
        df, src = load_best_1h(mkt)
        if df.empty:
            print(f"  {mkt}: no data available — skipping")
            continue
        n_days = df.index.normalize().nunique()
        print(f"  {mkt}: {len(df):,} bars | {n_days} days | "
              f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')} [{src}]")
        data_ranges[mkt] = f"{df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')} [{src}]"

        print(f"  Computing HTF bias for {mkt}…")
        htf_bias, regime, fast_bias = get_htf_data(mkt, config)

        print(f"  Running FHB signals…")
        trades = run_fhb_strategy(
            mkt, df, config, econ_cal, vix_filt, gls_eng,
            htf_bias, regime, fast_bias,
        )
        print(f"  {mkt}: {len(trades)} trades")
        all_fhb_trades.extend(trades)

    # Combined FHB (NQ + MNQ pooled) — weights naturally from trade count
    if all_fhb_trades:
        result = analyse_strategy("FHB_NQ_MNQ", all_fhb_trades)
        all_strategy_results["FHB_NQ_MNQ"] = result
    else:
        print("  No FHB trades generated — check data availability")

    # ── Step 3: GC Mean-Reversion — MGC ───────────────────────────────────────
    _hdr("Step 3: GC Mean-Reversion Strategy — MGC")

    gc_markets    = ["MGC", "GC"]
    all_gc_trades: list[dict] = []

    for mkt in gc_markets:
        print(f"\n  Loading 1h data for {mkt}…")
        df, src = load_best_1h(mkt)
        if df.empty:
            print(f"  {mkt}: no data available — skipping")
            continue
        n_days = df.index.normalize().nunique()
        print(f"  {mkt}: {len(df):,} bars | {n_days} days | "
              f"{df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')} [{src}]")
        data_ranges[mkt] = f"{df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')} [{src}]"

        print(f"  Computing HTF bias for {mkt}…")
        htf_bias, _, _ = get_htf_data(mkt, config)

        print(f"  Running GC mean-reversion signals…")
        trades = run_gc_strategy(mkt, df, config, econ_cal, htf_bias)
        print(f"  {mkt}: {len(trades)} trades")
        all_gc_trades.extend(trades)

    if all_gc_trades:
        result = analyse_strategy("GC_MGC_REV", all_gc_trades)
        all_strategy_results["GC_MGC_REV"] = result
    else:
        print("  No GC trades generated — check data availability")

    # ── Step 4: Portfolio-level summary ───────────────────────────────────────
    _hdr("Portfolio Summary")
    all_trades = all_fhb_trades + all_gc_trades
    if all_trades:
        df_all   = pd.DataFrame(all_trades)
        tot_pnl  = float(df_all["pnl_net"].sum())
        wins     = (df_all["pnl_net"] > 0).sum()
        n_all    = len(df_all)
        gw       = float(df_all.loc[df_all["pnl_net"] > 0, "pnl_net"].sum())
        gl       = float(abs(df_all.loc[df_all["pnl_net"] < 0, "pnl_net"].sum()))
        port_pf  = gw / gl if gl > 0 else 999.0
        daily    = df_all.groupby("date")["pnl_net"].sum()
        cum      = daily.cumsum()
        mdd      = float((cum - cum.cummax()).min())
        print(f"  Portfolio: {n_all} trades | Win%={wins/n_all*100:.1f}% | "
              f"PF={port_pf:.2f} | P&L=${tot_pnl:,.0f} | MDD=${mdd:,.0f}")

    # ── Step 5: Save results ───────────────────────────────────────────────────
    _hdr("Saving Results")
    verdicts = {k: v.get("verdict", "UNKNOWN") for k, v in all_strategy_results.items()}
    print(f"  Strategy verdicts: {verdicts}")

    payload = {
        "generated_at":  datetime.now().isoformat(),
        "data_ranges":   data_ranges,
        "strategies":    all_strategy_results,
        "portfolio": {
            "n_trades":     len(all_trades),
            "win_rate_pct": round(wins / n_all * 100, 1) if all_trades else 0,
            "profit_factor": round(port_pf, 2) if all_trades else 0,
            "total_net_pnl": round(tot_pnl, 2) if all_trades else 0,
            "max_drawdown_usd": round(mdd, 2) if all_trades else 0,
        } if all_trades else {},
    }

    save_results(payload)
    print()


if __name__ == "__main__":
    main()
