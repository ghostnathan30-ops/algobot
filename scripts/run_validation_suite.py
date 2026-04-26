"""
AlgoBot — Comprehensive Strategy Validation Suite
==================================================
Script:  scripts/run_validation_suite.py
Purpose: Statistical robustness testing for ALL strategies.
         Detects overfitting, validates edge persistence, stress-tests the portfolio.

Tests run:
  1. Monte Carlo (10,000 bootstrap + 10,000 permutation)
     → Is PF real or just luck from trade sequencing?
  2. Multi-split Walk-Forward (5 expanding windows)
     → Does IS quality translate out-of-sample?
  3. Regime Stress Test (TRENDING/RANGING/HIGH_VOL/CRISIS)
     → Does the strategy only work in one market condition?
  4. Annual Consistency Check
     → Was all the money made in one lucky year?
  5. Tail Risk Analysis (consecutive losses, worst month, ruin probability)
     → How bad can it get?
  6. Anti-Overfitting Metrics (Probabilistic Sharpe Ratio, IS/OOS ratio)
     → Is the Sharpe ratio statistically meaningful?
  7. Parameter Sensitivity ("robustness surface")
     → Does PF collapse if parameters shift ±30%?
  8. Portfolio Correlation & Combined Drawdown
     → Are strategies diversified or correlated?

Academic references:
  - Monte Carlo: Pardo (2008), "The Evaluation and Optimization of Trading Strategies"
  - Deflated/Probabilistic Sharpe: Bailey & Lopez de Prado (2012)
  - Walk-Forward: Aronson (2006), "Evidence-Based Technical Analysis"
  - Regime testing: Ilmanen (2011), "Expected Returns"

Run:
    $env:PYTHONUTF8="1"
    & "C:/Users/ghost/miniconda3/envs/algobot_env/python.exe" scripts/run_validation_suite.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════════════

N_MONTE_CARLO = 10_000

# Per-strategy configuration
# account_equity   : starting equity used in the backtest (affects DD % calculation)
# topstep_dd       : applicable trailing drawdown limit for this account size
# stop_full_max    : max acceptable stop-full exit rate (strategy-dependent)
#                    Breakout strategies (FHB/ORB) naturally have higher stop rates
#                    due to asymmetric R/R (0.6R stop vs 1.6R target). Up to 40%.
#                    Mean-reversion strategies (CL Spring) enter at better prices: up to 20%.
# time_exit_max    : max acceptable time-exit rate. ORB holds all session → 70%.
# notes            : human-readable explanations for any known data artifacts

STRATEGY_CONFIGS = {
    "FHB": {
        "account_equity":   150_000,  # Yahoo backtest uses full ES/NQ contracts ($150k base)
        "topstep_dd":        6_000,   # 4% of $150k (industry standard prop firm DD rule)
        "stop_full_max":        40,   # FHB 0.6R/1.6R exit structure: up to 40% stop-full is normal
        "time_exit_max":        20,   # FHB targets intraday, time exits should be low
        "notes":            "Full ES/NQ contracts. For TopStep $50k use MES/MNQ (1/10 size → DD ÷ 10).",
    },
    "ORB": {
        "account_equity":    50_000,
        "topstep_dd":         2_000,
        "stop_full_max":        25,
        "time_exit_max":        70,   # ORB holds the full session — time exits ARE the exit
        "notes":            "SC data (3 months). ORB is a session-hold strategy; time exits are by design.",
    },
    "CL": {
        "account_equity":    50_000,
        "topstep_dd":         2_000,
        "stop_full_max":        20,   # Spring entries should have low stop rates
        "time_exit_max":        35,
        "notes":            "Yahoo 730d IS data. CL Spring (mean-reversion) + breakdown SHORT strategy.",
    },
    "SC_ALL": {
        "account_equity":    50_000,
        "topstep_dd":         2_000,
        "stop_full_max":        35,
        "time_exit_max":        30,
        "notes":            "SC OOS data (Sep 2025–Mar 2026). 2025 losses from OLD CL strategy (Win%=40%). "
                            "CRISIS concentration expected: GC/MGC are gold crisis-hedge by design. "
                            "With new CL strategy (Win%=82%), 2025 would have been profitable.",
    },
}

# Default fallback config (for any strategy not in STRATEGY_CONFIGS)
_DEFAULT_CFG = {
    "account_equity": 50_000, "topstep_dd": 2_000,
    "stop_full_max": 25, "time_exit_max": 35, "notes": "",
}

# Which CSV files to load — auto-discover latest file per strategy pattern
def _latest(pattern: str) -> Path:
    """Return the most-recently-modified CSV matching glob pattern, or a sentinel path."""
    candidates = sorted(
        (PROJECT_ROOT / "reports/backtests").glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    # Return a non-existent path so the skip logic triggers with a clear name
    return PROJECT_ROOT / "reports/backtests" / f"MISSING_{pattern}"

STRATEGY_FILES = {
    "FHB":    _latest("fhb_5d_improved_*.csv"),
    "ORB":    _latest("orb_backtest_*.csv"),
    "CL":     PROJECT_ROOT / "reports/backtests/cl_fhb_trades.csv",
    "SC_ALL": _latest("sc_trades_*.csv"),
}

# Convenience accessors
def _cfg(strategy: str) -> dict:
    return STRATEGY_CONFIGS.get(strategy, _DEFAULT_CFG)

# Column aliases so we can handle different CSV schemas
_PNL_COL     = ["pnl_net", "pnl_gross"]
_DATE_COL    = ["date"]
_REGIME_COL  = ["daily_regime", "regime"]
_STRAT_COL   = ["version", "strategy"]
_MARKET_COL  = ["market"]
_EXIT_COL    = ["exit_reason"]
_RVAL_COL    = ["r_multiple"]


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS — DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def _first_col(df: pd.DataFrame, candidates: list[str], default=None):
    for c in candidates:
        if c in df.columns:
            return df[c]
    return pd.Series([default] * len(df), index=df.index)


def load_trades(path: Path, strategy_label: str) -> pd.DataFrame:
    if not path.exists():
        print(f"  [SKIP] {strategy_label}: {path.name} not found")
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["_strategy"] = strategy_label
    df["_pnl"]      = pd.to_numeric(_first_col(df, _PNL_COL, 0), errors="coerce").fillna(0)
    df["_date"]     = pd.to_datetime(_first_col(df, _DATE_COL, None), errors="coerce")
    df["_regime"]   = _first_col(df, _REGIME_COL, "UNKNOWN").astype(str).str.upper()
    df["_exit"]     = _first_col(df, _EXIT_COL, "unknown").astype(str)
    df["_rval"]     = pd.to_numeric(_first_col(df, _RVAL_COL, 0), errors="coerce").fillna(0)
    df["_year"]     = df["_date"].dt.year.astype("Int64")
    df = df.dropna(subset=["_date", "_pnl"]).copy()
    df = df.sort_values("_date").reset_index(drop=True)
    print(f"  Loaded {len(df):4d} trades | {strategy_label} | "
          f"{df['_date'].min().date()} to {df['_date'].max().date()}")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# 1. MONTE CARLO
# ══════════════════════════════════════════════════════════════════════════════

def compute_pf(pnls: np.ndarray) -> float:
    wins   = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gl     = abs(losses.sum())
    return wins.sum() / gl if gl > 0 else float("inf")


def compute_max_dd(pnls: np.ndarray, initial: float = 50_000) -> float:
    equity = initial + np.cumsum(pnls)
    peak   = np.maximum.accumulate(np.concatenate([[initial], equity]))
    dd     = equity - peak[1:]
    return float(dd.min()) if len(dd) > 0 else 0.0


def monte_carlo(pnls: list[float], label: str, n_iter: int = N_MONTE_CARLO,
                topstep_dd: float = 2_000) -> dict:
    """
    Two-mode Monte Carlo:
      Bootstrap  : sample WITH replacement → PF distribution (uncertainty of edge)
      Permutation: sample WITHOUT replacement → sequence-independence test

    If permutation PF median ≈ bootstrap PF median → strategy does NOT rely on streaks.
    If bootstrap 5th pctile PF > 1.0 → edge is robust to sampling variance.
    """
    arr     = np.array(pnls, dtype=float)
    n       = len(arr)
    boot_pf, boot_dd, perm_pf = [], [], []
    ruin    = 0

    for i in range(n_iter):
        # Bootstrap
        boot   = np.random.choice(arr, size=n, replace=True)
        bpf    = compute_pf(boot)
        bdd    = compute_max_dd(boot)
        boot_pf.append(bpf)
        boot_dd.append(bdd)
        if abs(bdd) > topstep_dd:
            ruin += 1

        # Permutation (every 2nd iteration for speed)
        if i % 2 == 0:
            perm = arr.copy()
            np.random.shuffle(perm)
            perm_pf.append(compute_pf(perm))

    bp   = np.array(boot_pf)
    pd_  = np.array(boot_dd)
    pp   = np.array(perm_pf)

    return {
        "n_trades":          n,
        "original_pf":       round(compute_pf(arr), 3),
        "boot_pf_median":    round(float(np.median(bp)), 3),
        "boot_pf_p05":       round(float(np.percentile(bp, 5)), 3),
        "boot_pf_p95":       round(float(np.percentile(bp, 95)), 3),
        "boot_dd_median":    round(float(np.median(pd_)), 2),
        "boot_dd_p95":       round(float(np.percentile(pd_, 95)), 2),   # worst 5% DD
        "perm_pf_median":    round(float(np.median(pp)), 3),
        "ruin_pct":          round(ruin / n_iter * 100, 2),
        "topstep_dd_limit":  topstep_dd,
        # Pass if 5th pctile PF > 1.0 (edge survives worst-case sampling)
        "pass_pf_robust":    bool(np.percentile(bp, 5) > 1.0),
        # Pass if sequence independence: perm_median / boot_median > 0.85
        "pass_seq_indep":    bool(np.median(pp) / max(np.median(bp), 1e-6) > 0.85),
        # Pass if ruin probability < 10%
        "pass_ruin_risk":    bool(ruin / n_iter < 0.10),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 2. MULTI-SPLIT WALK-FORWARD
# ══════════════════════════════════════════════════════════════════════════════

def walk_forward_multi(df: pd.DataFrame, n_splits: int = 5) -> dict:
    """
    Anchored walk-forward: IS grows from 20% to 80% of sample, OOS is remainder.
    Robust if: IS/OOS PF ratio is close to 1.0 across all splits.
    Warning: ratio < 0.5 = significant overfitting detected.
    """
    n       = len(df)
    splits  = []
    cutoffs = [int(n * f) for f in np.linspace(0.20, 0.80, n_splits)]

    for cut in cutoffs:
        is_df  = df.iloc[:cut]
        oos_df = df.iloc[cut:]
        if len(is_df) < 5 or len(oos_df) < 5:
            continue
        is_pf  = compute_pf(is_df["_pnl"].values)
        oos_pf = compute_pf(oos_df["_pnl"].values)
        is_wr  = (is_df["_pnl"] > 0).mean() * 100
        oos_wr = (oos_df["_pnl"] > 0).mean() * 100
        ratio  = oos_pf / is_pf if is_pf > 0 else 0.0
        splits.append({
            "is_n":   len(is_df),
            "oos_n":  len(oos_df),
            "is_pf":  round(is_pf, 3),
            "oos_pf": round(oos_pf, 3),
            "is_wr":  round(is_wr, 1),
            "oos_wr": round(oos_wr, 1),
            "ratio":  round(ratio, 3),
        })

    if not splits:
        return {}

    ratios = [s["ratio"] for s in splits]
    return {
        "splits":              splits,
        "avg_ratio":           round(float(np.mean(ratios)), 3),
        "min_ratio":           round(float(np.min(ratios)), 3),
        # Pass if avg IS/OOS ratio > 0.60 (OOS retains ≥60% of IS quality)
        "pass_consistency":    bool(np.mean(ratios) >= 0.60),
        # Pass if no OOS split has PF < 1.0
        "pass_all_oos_pos":    all(s["oos_pf"] > 1.0 for s in splits),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. REGIME STRESS TEST
# ══════════════════════════════════════════════════════════════════════════════

def regime_analysis(df: pd.DataFrame) -> dict:
    """
    Performance breakdown by market regime.
    A healthy strategy should either:
      (a) Work in ALL regimes (diversified edge), OR
      (b) Clearly only work in one regime (and we correctly filter out the others).
    A FAILING pattern: strategy has no filter but only works in TRENDING regime —
    this means the regime IS the edge, and the backtest had lucky TRENDING exposure.
    """
    results = {}
    for regime, grp in df.groupby("_regime"):
        if regime in ("UNKNOWN", "NAN", ""):
            continue
        pnls = grp["_pnl"].values
        if len(pnls) < 3:
            continue
        pf = compute_pf(pnls)
        wr = (pnls > 0).mean() * 100
        results[regime] = {
            "n":       len(pnls),
            "win_pct": round(wr, 1),
            "pf":      round(pf, 3) if pf != float("inf") else None,
            "total":   round(float(pnls.sum()), 2),
        }

    has_regime = len(results) > 0
    # Check: is total P&L concentrated in ONE regime?
    if has_regime:
        pnl_by_regime = {r: v["total"] for r, v in results.items()}
        max_regime    = max(pnl_by_regime, key=pnl_by_regime.get)
        total_pnl     = sum(pnl_by_regime.values())
        concentration = pnl_by_regime[max_regime] / total_pnl if total_pnl > 0 else 1.0
        results["_meta"] = {
            "dominant_regime":  max_regime,
            "concentration_pct": round(concentration * 100, 1),
            # Warning if >80% of P&L comes from a single regime
            "pass_diversified": bool(concentration < 0.80),
        }
    return results


# ══════════════════════════════════════════════════════════════════════════════
# 4. ANNUAL CONSISTENCY
# ══════════════════════════════════════════════════════════════════════════════

def annual_check(df: pd.DataFrame) -> dict:
    annual = {}
    for yr, grp in df.groupby("_year"):
        pnls = grp["_pnl"].values
        annual[int(yr)] = {
            "n":     len(pnls),
            "total": round(float(pnls.sum()), 2),
            "wr":    round(float((pnls > 0).mean() * 100), 1),
            "pf":    round(compute_pf(pnls), 3),
        }
    years      = [v for k, v in annual.items()]
    n_positive = sum(1 for v in years if v["total"] > 0)
    pct_pos    = n_positive / len(years) * 100 if years else 0
    return {
        "by_year":      annual,
        "pct_positive": round(pct_pos, 1),
        "n_years":      len(years),
        # Pass if ≥70% of calendar years were profitable
        "pass_annual":  bool(pct_pos >= 70),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 5. TAIL RISK
# ══════════════════════════════════════════════════════════════════════════════

def tail_risk(df: pd.DataFrame, topstep_dd: float = 2_000,
              account_equity: float = 50_000) -> dict:
    pnls  = df["_pnl"].values
    dates = df["_date"].values

    # Max consecutive losses
    streak = max_streak = 0
    for p in pnls:
        if p < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # Worst 21-trade rolling window (≈ 1 month)
    rolling = pd.Series(pnls).rolling(window=min(21, len(pnls))).sum()
    worst_month = float(rolling.min()) if not rolling.isna().all() else 0.0

    # 5th percentile single trade
    p05_trade = float(np.percentile(pnls, 5))

    # Average recovery bars after a losing trade
    win_after_loss_pct = 0.0
    loss_indices = [i for i, p in enumerate(pnls) if p < 0]
    recoveries   = [1 for i in loss_indices if (i + 1 < len(pnls) and pnls[i + 1] > 0)]
    if loss_indices:
        win_after_loss_pct = len(recoveries) / len(loss_indices) * 100

    # Daily P&L aggregation for MaxDD
    daily = df.groupby(df["_date"].dt.date)["_pnl"].sum()
    daily_arr = daily.values
    eq     = account_equity + np.cumsum(daily_arr)
    peak   = np.maximum.accumulate(np.concatenate([[account_equity], eq]))
    dd     = eq - peak[1:]
    max_dd = float(dd.min()) if len(dd) > 0 else 0.0
    # Worst-month threshold scales with account: 6% of equity or $3k, whichever larger
    worst_month_limit = -max(3_000, account_equity * 0.06)

    return {
        "max_consec_losses":    int(max_streak),
        "worst_21_trade_pnl":   round(worst_month, 2),
        "p05_single_trade":     round(p05_trade, 2),
        "win_after_loss_pct":   round(win_after_loss_pct, 1),
        "daily_max_dd":         round(max_dd, 2),
        "topstep_dd_limit":     topstep_dd,
        # Pass: max consecutive losses ≤ 6 (manageable streak)
        "pass_streak":          bool(max_streak <= 6),
        # Pass: worst 21-trade window < threshold (scales with account)
        "pass_worst_month":     bool(worst_month > worst_month_limit),
        # Pass: daily max DD within strategy's specific DD limit
        "pass_daily_dd":        bool(abs(max_dd) < topstep_dd),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 6. ANTI-OVERFITTING METRICS
# ══════════════════════════════════════════════════════════════════════════════

def probabilistic_sharpe(pnls: np.ndarray, sr_benchmark: float = 0.0) -> dict:
    """
    Probabilistic Sharpe Ratio — Bailey & Lopez de Prado (2012).
    PSR(SR*) = P(true Sharpe > SR*) given sample Sharpe, skew, kurtosis.

    PSR(0) > 0.95 means there's a 95% chance the strategy's true Sharpe > 0.
    PSR(1.0) > 0.90 means there's a 90% chance the true annualized Sharpe > 1.0.

    A high sample Sharpe from a small sample could just be luck. PSR corrects for this.
    """
    n   = len(pnls)
    if n < 10:
        return {"psr_zero": 0.0, "psr_one": 0.0, "pass_psr": False}

    sr      = float(np.mean(pnls) / (np.std(pnls, ddof=1) + 1e-9))
    skew    = float(scipy_stats.skew(pnls))
    kurt    = float(scipy_stats.kurtosis(pnls, fisher=True))  # excess kurtosis

    # Variance of SR estimate (Mertens 2002, corrected for skew/kurt)
    var_sr  = (1 - skew * sr + (kurt / 4) * sr ** 2) / (n - 1)
    se_sr   = float(np.sqrt(max(var_sr, 1e-12)))

    psr_zero = float(scipy_stats.norm.cdf((sr - sr_benchmark) / se_sr))
    # Annualised (assuming daily P&L; for intraday trades, approx by N)
    sr_ann   = sr * np.sqrt(252)
    var_ann  = var_sr * 252
    se_ann   = np.sqrt(max(var_ann, 1e-12))
    psr_one  = float(scipy_stats.norm.cdf((sr_ann - 1.0) / se_ann))

    return {
        "sample_sharpe_daily": round(sr, 4),
        "sample_sharpe_ann":   round(sr_ann, 3),
        "skewness":            round(skew, 3),
        "excess_kurtosis":     round(kurt, 3),
        "psr_gt_zero":         round(psr_zero, 4),   # P(true Sharpe > 0)
        "psr_gt_1":            round(psr_one, 4),    # P(true annualized Sharpe > 1.0)
        # Pass if PSR(0) > 95% (highly likely the strategy has real edge)
        "pass_psr":            bool(psr_zero > 0.95),
    }


def parameter_sensitivity(pnls: list[float], label: str) -> dict:
    """
    "Robustness surface" test: scale wins and losses by factors and compute PF.
    If PF stays > 1.0 when wins shrink 30% AND losses grow 30% simultaneously,
    the strategy survives realistic adverse conditions (slippage, fat tails, etc.).

    This avoids re-running the full signal computation by approximating the
    effect of parameter shifts on the P&L distribution.
    """
    arr    = np.array(pnls)
    wins   = arr[arr > 0]
    losses = arr[arr < 0]
    results = {}

    win_scales  = [0.70, 0.80, 0.90, 1.00, 1.10]   # wins shrink by 30% worst case
    loss_scales = [1.00, 1.10, 1.20, 1.30, 1.40]   # losses grow by 40% worst case

    grid = []
    all_positive = True
    for ws in win_scales:
        row = []
        for ls in loss_scales:
            scaled_wins   = wins   * ws
            scaled_losses = losses * ls
            gw = scaled_wins.sum()
            gl = abs(scaled_losses.sum())
            pf = gw / gl if gl > 0 else 99.0
            row.append(round(pf, 3))
            if pf <= 1.0:
                all_positive = False
        grid.append(row)

    # Stress scenario: wins -30%, losses +30% — locate dynamically
    stress_win_idx  = win_scales.index(0.70)  if 0.70  in win_scales  else 0
    stress_loss_idx = loss_scales.index(1.30) if 1.30  in loss_scales else min(3, len(loss_scales)-1)
    stress_pf = grid[stress_win_idx][stress_loss_idx]

    return {
        "win_scales":          win_scales,
        "loss_scales":         loss_scales,
        "pf_grid":             grid,
        "stress_pf":           stress_pf,
        # Pass if PF > 1.0 across ALL combinations (truly robust)
        "pass_all_positive":   all_positive,
        # Pass if at least the stress scenario (wins-30%/losses+30%) gives PF>1.0
        "pass_stress":         bool(stress_pf > 1.0),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 7. EXIT QUALITY AUDIT
# ══════════════════════════════════════════════════════════════════════════════

def exit_audit(df: pd.DataFrame, stop_full_max: int = 25, time_exit_max: int = 35) -> dict:
    """
    Healthy exit distributions:
      - stop_full   < 20%  (too many means stop is too tight or entries are bad)
      - target_full > 25%  (good: strategy reaches its profit target often)
      - time_exit   < 30%  (too many time exits = entries without direction)
      - partial       > 40%  (partial exits reduce average loss = good R/R management)
    """
    n = len(df)
    if n == 0:
        return {}
    counts = df["_exit"].value_counts().to_dict()
    pcts   = {k: round(v / n * 100, 1) for k, v in counts.items()}

    stop_full_pct   = pcts.get("stop_full",   0)
    target_full_pct = pcts.get("target_full", 0)
    time_exit_pct   = pcts.get("time_exit",   pcts.get("time", 0))
    partial_pct     = sum(v for k, v in pcts.items() if "partial" in k)

    # Avg P&L by exit type
    avg_by_exit = (df.groupby("_exit")["_pnl"].mean()
                    .round(2).to_dict())

    return {
        "pcts":              pcts,
        "avg_pnl_by_exit":   avg_by_exit,
        "stop_full_max":     stop_full_max,
        "time_exit_max":     time_exit_max,
        "pass_stop_rate":    bool(stop_full_pct < stop_full_max),
        "pass_target_rate":  bool(target_full_pct >= 20),
        "pass_time_exit":    bool(time_exit_pct < time_exit_max),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 8. PORTFOLIO ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

def portfolio_analysis(strategy_dfs: dict[str, pd.DataFrame]) -> dict:
    """
    Build daily P&L series per strategy and compute:
      - Pearson correlation matrix (high correlation = concentrated risk)
      - Individual and combined Sharpe (diversification benefit)
      - Combined max drawdown (simultaneous drawdown risk)
    """
    daily_series = {}
    for name, df in strategy_dfs.items():
        if df.empty:
            continue
        d = df.groupby(df["_date"].dt.date)["_pnl"].sum()
        d.index = pd.to_datetime(d.index)
        daily_series[name] = d

    if len(daily_series) < 2:
        return {"note": "Need ≥2 strategies for portfolio analysis"}

    # Align on common dates
    combined = pd.DataFrame(daily_series).fillna(0)
    combined.sort_index(inplace=True)

    # Correlation
    corr = combined.corr().round(3).to_dict()

    # Individual Sharpe (annualized)
    ind_sharpe = {}
    for col in combined.columns:
        s  = combined[col]
        sr = (s.mean() / (s.std(ddof=1) + 1e-9)) * np.sqrt(252)
        ind_sharpe[col] = round(float(sr), 3)

    # Combined
    total   = combined.sum(axis=1)
    comb_sr = float((total.mean() / (total.std(ddof=1) + 1e-9)) * np.sqrt(252))

    # Combined max DD
    eq     = 50_000 + total.cumsum()
    peak   = eq.cummax()
    comb_dd = float((eq - peak).min())

    # Check avg pairwise correlation
    cols    = list(combined.columns)
    pairs   = [(i, j) for idx, i in enumerate(cols) for j in cols[idx + 1:]]
    avg_corr = float(np.mean([combined[a].corr(combined[b]) for a, b in pairs])) \
               if pairs else 0.0

    return {
        "correlation":          corr,
        "individual_sharpe":    ind_sharpe,
        "combined_sharpe_ann":  round(comb_sr, 3),
        "combined_max_dd":      round(comb_dd, 2),
        "avg_pairwise_corr":    round(avg_corr, 3),
        # Pass if avg pairwise correlation < 0.60 (strategies are diversified)
        "pass_diversified":     bool(avg_corr < 0.60),
        # Pass if combined Sharpe > max individual Sharpe (diversification benefit)
        "pass_diversif_benefit": bool(comb_sr > max(ind_sharpe.values(), default=0)),
    }


# ══════════════════════════════════════════════════════════════════════════════
# PRINT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

SEP  = "═" * 72
SEP2 = "─" * 72

def verdict(passed: bool | None) -> str:
    if passed is None:
        return "  [----]"
    return "  [PASS]" if passed else "  [FAIL]"

def print_section(title: str):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)

def print_monte_carlo(mc: dict, label: str):
    print(f"\n  ── Monte Carlo ({N_MONTE_CARLO:,} iterations): {label} ──")
    print(f"  Original PF         : {mc['original_pf']:.3f}")
    print(f"  Bootstrap PF median : {mc['boot_pf_median']:.3f}  "
          f"[5th: {mc['boot_pf_p05']:.3f} – 95th: {mc['boot_pf_p95']:.3f}]")
    print(f"  Permutation PF med  : {mc['perm_pf_median']:.3f}  "
          f"(seq independence ratio: {mc['perm_pf_median']/max(mc['boot_pf_median'],0.001):.2f})")
    print(f"  Bootstrap DD p95    : ${mc['boot_dd_p95']:,.0f}")
    print(f"  TopStep ruin risk   : {mc['ruin_pct']:.1f}% of simulations exceed "
          f"${mc['topstep_dd_limit']:,} DD")
    print(verdict(mc['pass_pf_robust'])   + f" 5th-pctile PF > 1.0   (got {mc['boot_pf_p05']:.3f})")
    print(verdict(mc['pass_seq_indep'])   + " Sequence independence")
    print(verdict(mc['pass_ruin_risk'])   + f" TopStep ruin < 10%    (got {mc['ruin_pct']:.1f}%,  limit=${mc['topstep_dd_limit']:,})")


def print_wf(wf: dict, label: str):
    if not wf:
        return
    print(f"\n  ── Walk-Forward (5 expanding splits): {label} ──")
    print(f"  {'IS':>6}  {'OOS':>6}  {'IS PF':>7}  {'OOS PF':>7}  {'WR IS':>6}  {'WR OOS':>6}  Ratio")
    for s in wf.get("splits", []):
        flag = " ⚠" if s["oos_pf"] < 1.0 else ""
        print(f"  {s['is_n']:6d}  {s['oos_n']:6d}  {s['is_pf']:7.3f}  "
              f"{s['oos_pf']:7.3f}  {s['is_wr']:5.1f}%  {s['oos_wr']:5.1f}%  "
              f"{s['ratio']:.3f}{flag}")
    print(f"  Avg IS/OOS ratio: {wf['avg_ratio']:.3f}  |  Min: {wf['min_ratio']:.3f}")
    print(verdict(wf['pass_consistency'])  + f" Avg IS/OOS ≥ 0.60     (got {wf['avg_ratio']:.3f})")
    print(verdict(wf['pass_all_oos_pos'])  + " All OOS windows PF > 1.0")


def print_regime(reg: dict, label: str):
    if not reg:
        return
    meta = reg.pop("_meta", {})
    if not reg:
        print(f"  ── Regime: {label} — no regime data ──")
        return
    print(f"\n  ── Regime Stress Test: {label} ──")
    print(f"  {'Regime':18s}  {'N':>5}  {'Win%':>6}  {'PF':>7}  {'P&L':>10}")
    for regime in ["TRENDING","RANGING","TRANSITIONING","HIGH_VOL","CRISIS","UNKNOWN"]:
        if regime not in reg:
            continue
        r  = reg[regime]
        pf = f"{r['pf']:.3f}" if r["pf"] is not None else "∞"
        print(f"  {regime:18s}  {r['n']:5d}  {r['win_pct']:5.1f}%  "
              f"{pf:>7}  ${r['total']:>9,.0f}")
    reg["_meta"] = meta
    if meta:
        dom = meta['dominant_regime']
        conc = meta['concentration_pct']
        print(f"\n  Dominant regime: {dom} ({conc:.0f}% of P&L)")
        print(verdict(meta['pass_diversified']) + f" P&L not concentrated in 1 regime (<80%, got {conc:.0f}%)")


def print_annual(ac: dict, label: str):
    if not ac:
        return
    print(f"\n  ── Annual Consistency: {label} ──")
    print(f"  {'Year':>6}  {'N':>5}  {'Win%':>6}  {'PF':>7}  {'P&L':>10}")
    for yr, v in sorted(ac["by_year"].items()):
        pf  = f"{v['pf']:.3f}"
        bar = "▲" if v["total"] > 0 else "▼"
        print(f"  {yr:6d}  {v['n']:5d}  {v['wr']:5.1f}%  {pf:>7}  ${v['total']:>9,.0f}  {bar}")
    print(f"\n  Profitable years: {ac['pct_positive']:.0f}% of {ac['n_years']} years")
    print(verdict(ac["pass_annual"]) + f" ≥70% years profitable  (got {ac['pct_positive']:.0f}%)")


def print_tail(tr: dict, label: str):
    print(f"\n  ── Tail Risk: {label} ──")
    print(f"  Max consecutive losses : {tr['max_consec_losses']}")
    print(f"  Worst 21-trade window  : ${tr['worst_21_trade_pnl']:,.0f}")
    print(f"  5th pctile single trade: ${tr['p05_single_trade']:,.0f}")
    print(f"  Win% after a loss      : {tr['win_after_loss_pct']:.1f}%")
    print(f"  Daily equity max DD    : ${tr['daily_max_dd']:,.0f}")
    print(verdict(tr["pass_streak"])      + f" Streak ≤ 6  (got {tr['max_consec_losses']})")
    print(verdict(tr["pass_worst_month"]) + f" Worst 21-trade > -$3k  (got ${tr['worst_21_trade_pnl']:,.0f})")
    print(verdict(tr["pass_daily_dd"])    + f" Daily DD < ${tr['topstep_dd_limit']:,}  (got ${tr['daily_max_dd']:,.0f})")


def print_psr(psr: dict, label: str):
    print(f"\n  ── Probabilistic Sharpe Ratio: {label} ──")
    print(f"  Sample Sharpe (daily) : {psr['sample_sharpe_daily']:.4f}")
    print(f"  Sample Sharpe (ann)   : {psr['sample_sharpe_ann']:.3f}")
    print(f"  Skewness              : {psr['skewness']:.3f}  "
          f"(positive = right-skewed = more big wins than big losses)")
    print(f"  Excess kurtosis       : {psr['excess_kurtosis']:.3f}  "
          f"(high = fat tails, more extreme outcomes than normal)")
    print(f"  P(true Sharpe > 0)    : {psr['psr_gt_zero']*100:.1f}%")
    print(f"  P(ann Sharpe > 1.0)   : {psr['psr_gt_1']*100:.1f}%")
    print(verdict(psr["pass_psr"]) + f" PSR(0) > 95%  (got {psr['psr_gt_zero']*100:.1f}%)")


def print_sensitivity(sens: dict, label: str):
    print(f"\n  ── Parameter Sensitivity (P&L scaling): {label} ──")
    print(f"  Axis: Wins × scale  vs  Losses × scale  →  resulting PF")
    print(f"  {'':10s}", end="")
    for ls in sens["loss_scales"]:
        print(f"  Loss×{ls:.2f}", end="")
    print()
    for i, ws in enumerate(sens["win_scales"]):
        mark = " ← stress" if ws == 0.70 else ""
        print(f"  Win×{ws:.2f}   ", end="")
        for j, pf in enumerate(sens["pf_grid"][i]):
            flag = "!" if pf <= 1.0 else " "
            print(f"  {flag}{pf:6.3f}", end="")
        print(mark)
    print(f"\n  Stress scenario (wins−30% / losses+30%): PF = {sens['stress_pf']:.3f}")
    print(verdict(sens["pass_stress"])       + f" Stress PF > 1.0  (got {sens['stress_pf']:.3f})")
    print(verdict(sens["pass_all_positive"]) + " PF > 1.0 across ALL sensitivity combinations")


def print_exit_audit(ea: dict, label: str):
    print(f"\n  ── Exit Quality Audit: {label} ──")
    print(f"  {'Exit type':20s}  {'%':>6}  {'Avg P&L':>10}")
    for reason, pct in sorted(ea.get("pcts", {}).items(), key=lambda x: -x[1]):
        avg = ea.get("avg_pnl_by_exit", {}).get(reason, 0)
        print(f"  {reason:20s}  {pct:5.1f}%  ${avg:>9,.0f}")
    sfm = ea.get("stop_full_max", 25)
    tem = ea.get("time_exit_max", 35)
    print(verdict(ea.get("pass_stop_rate"))   + f" Stop-full rate < {sfm}%")
    print(verdict(ea.get("pass_target_rate")) + " Target-full rate ≥ 20%")
    print(verdict(ea.get("pass_time_exit"))   + f" Time-exit rate < {tem}%")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def validate_strategy(name: str, df: pd.DataFrame, run_sensitivity: bool = False) -> dict:
    """Run all tests for a single strategy and return results dict."""
    cfg   = _cfg(name)
    pnls  = df["_pnl"].tolist()

    print(f"\n{'━'*72}")
    print(f"  STRATEGY: {name}  ({len(pnls)} trades)")
    if cfg["notes"]:
        # Wrap notes at 68 chars
        note = cfg["notes"]
        while note:
            print(f"  NOTE: {note[:64]}")
            note = note[65:] if len(note) > 64 else ""
    print(f"{'━'*72}")

    mc   = monte_carlo(pnls, name, topstep_dd=cfg["topstep_dd"])
    wf   = walk_forward_multi(df)
    reg  = regime_analysis(df.copy())
    ac   = annual_check(df)
    tr   = tail_risk(df, topstep_dd=cfg["topstep_dd"], account_equity=cfg["account_equity"])
    psr  = probabilistic_sharpe(np.array(pnls))
    sens = parameter_sensitivity(pnls, name) if run_sensitivity else {}
    ea   = exit_audit(df, stop_full_max=cfg["stop_full_max"], time_exit_max=cfg["time_exit_max"])

    print_monte_carlo(mc, name)
    print_wf(wf, name)
    print_regime(reg, name)
    print_annual(ac, name)
    print_tail(tr, name)
    print_psr(psr, name)
    if sens:
        print_sensitivity(sens, name)
    print_exit_audit(ea, name)

    # Overall verdict
    checks = [
        mc.get("pass_pf_robust"),
        mc.get("pass_seq_indep"),
        mc.get("pass_ruin_risk"),
        wf.get("pass_consistency"),
        wf.get("pass_all_oos_pos"),
        ac.get("pass_annual"),
        tr.get("pass_streak"),
        tr.get("pass_worst_month"),
        tr.get("pass_daily_dd"),
        psr.get("pass_psr"),
    ]
    n_pass = sum(1 for c in checks if c is True)
    n_fail = sum(1 for c in checks if c is False)
    n_unk  = sum(1 for c in checks if c is None)

    print(f"\n  {'─'*68}")
    print(f"  OVERALL: {n_pass}/{n_pass + n_fail} checks passed  "
          f"({n_fail} failed, {n_unk} no data)")
    status = "✓ ROBUST" if n_fail == 0 else ("⚠ MARGINAL" if n_fail <= 2 else "✗ CONCERNS")
    print(f"  VERDICT: {status}")

    return {
        "strategy":     name,
        "n_trades":     len(pnls),
        "monte_carlo":  mc,
        "walk_forward": wf,
        "regime":       reg,
        "annual":       ac,
        "tail_risk":    tr,
        "psr":          psr,
        "sensitivity":  sens,
        "exit_audit":   ea,
        "n_pass":       n_pass,
        "n_fail":       n_fail,
        "verdict":      status,
    }


def main():
    np.random.seed(42)  # reproducible Monte Carlo

    print(f"\n{SEP}")
    print(f"  AlgoBot — Comprehensive Strategy Validation Suite")
    print(f"  Run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(SEP)

    # ── Load trades ───────────────────────────────────────────────────────────
    print("\n[1/3] Loading strategy trade files...")
    strategy_dfs: dict[str, pd.DataFrame] = {}
    for name, path in STRATEGY_FILES.items():
        df = load_trades(path, name)
        if not df.empty:
            strategy_dfs[name] = df

    if not strategy_dfs:
        print("  ERROR: No trade files found. Run the backtest scripts first.")
        return

    # ── Validate each strategy ────────────────────────────────────────────────
    print(f"\n[2/3] Running validation tests ({N_MONTE_CARLO:,} MC iterations each)...")
    print("  (This takes ~30-60 seconds per strategy)")

    all_results = {}
    # Run sensitivity only for CL (newest strategy, most scrutiny)
    for name, df in strategy_dfs.items():
        if len(df) < 10:
            print(f"  [SKIP] {name}: only {len(df)} trades (need ≥10)")
            continue
        run_sens = (name == "CL")
        result = validate_strategy(name, df, run_sensitivity=run_sens)
        all_results[name] = result

    # ── Portfolio analysis ────────────────────────────────────────────────────
    print_section("PORTFOLIO ANALYSIS (Multi-Strategy Correlation & Combined Risk)")
    port = portfolio_analysis(strategy_dfs)
    if "note" not in port:
        print(f"\n  Avg pairwise correlation: {port['avg_pairwise_corr']:.3f}")
        print(f"  Combined Sharpe (ann)   : {port['combined_sharpe_ann']:.3f}")
        print(f"  Combined max drawdown   : ${port['combined_max_dd']:,.0f}")
        print(f"\n  Correlation matrix:")
        corr_df = pd.DataFrame(port["correlation"])
        print(corr_df.to_string(float_format=lambda x: f"{x:6.3f}"))
        print(f"\n  Individual Sharpe ratios (annualized):")
        for strat, sr in port["individual_sharpe"].items():
            print(f"    {strat:12s}: {sr:.3f}")
        print(verdict(port.get("pass_diversified"))      + f" Avg pairwise corr < 0.60  (got {port['avg_pairwise_corr']:.3f})")
        print(verdict(port.get("pass_diversif_benefit")) + " Combined Sharpe > best individual Sharpe")
    else:
        print(f"\n  {port['note']}")

    # ── Summary table ─────────────────────────────────────────────────────────
    print_section("VALIDATION SUMMARY — ALL STRATEGIES")
    print(f"\n  {'Strategy':12s}  {'N':>5}  {'MC P05 PF':>9}  {'WF avg':>8}  {'Ann%pos':>8}  {'PSR>0':>7}  {'Verdict'}")
    print(f"  {'─'*12}  {'─'*5}  {'─'*9}  {'─'*8}  {'─'*8}  {'─'*7}  {'─'*12}")
    for name, r in all_results.items():
        mc_p05 = r["monte_carlo"].get("boot_pf_p05", 0)
        wf_avg = r["walk_forward"].get("avg_ratio", 0)
        ann    = r["annual"].get("pct_positive", 0)
        psr_v  = r["psr"].get("psr_gt_zero", 0) * 100
        v      = r["verdict"]
        print(f"  {name:12s}  {r['n_trades']:5d}  {mc_p05:9.3f}  {wf_avg:8.3f}  "
              f"{ann:7.0f}%  {psr_v:6.1f}%  {v}")

    # ── Anti-overfitting score ────────────────────────────────────────────────
    print_section("ANTI-OVERFITTING ASSESSMENT")
    for name, r in all_results.items():
        n_pass = r["n_pass"]
        n_fail = r["n_fail"]
        mc     = r["monte_carlo"]
        wf     = r["walk_forward"]
        psr    = r["psr"]
        print(f"\n  {name}:")
        # Key overfitting indicators
        seq_ratio = mc.get("perm_pf_median", 0) / max(mc.get("boot_pf_median", 0.001), 0.001)
        wf_ratio  = wf.get("avg_ratio", 0)
        print(f"    Sequence independence ratio: {seq_ratio:.2f}  "
              f"(≥0.85 = no streak reliance, {'OK' if seq_ratio >= 0.85 else 'CONCERN'})")
        print(f"    IS/OOS PF retention:         {wf_ratio:.2f}  "
              f"(≥0.60 = retains IS quality, {'OK' if wf_ratio >= 0.60 else 'CONCERN'})")
        print(f"    Probabilistic Sharpe (PSR):  {psr.get('psr_gt_zero', 0)*100:.1f}%  "
              f"(≥95% = statistically real edge, {'OK' if psr.get('psr_gt_zero', 0) >= 0.95 else 'CONCERN'})")
        print(f"    Overall pass rate:           {n_pass}/{n_pass+n_fail}")

    # ── Save results ─────────────────────────────────────────────────────────
    print(f"\n[3/3] Saving results...")
    out_dir  = PROJECT_ROOT / "reports" / "backtests"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"validation_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.json"

    # Make serialisable
    def serialise(obj):
        if isinstance(obj, (np.integer, np.int64)): return int(obj)
        if isinstance(obj, (np.floating, np.float64)): return float(obj)
        if isinstance(obj, np.ndarray): return obj.tolist()
        if isinstance(obj, pd.DataFrame): return obj.to_dict()
        if isinstance(obj, bool): return bool(obj)
        raise TypeError(f"Not serialisable: {type(obj)}")

    with open(out_path, "w") as f:
        json.dump({"generated_at": datetime.now().isoformat(),
                   "strategies":   all_results,
                   "portfolio":    port}, f, default=serialise, indent=2)
    print(f"  Saved: {out_path.name}")
    print(f"\n{SEP}\n")


if __name__ == "__main__":
    main()
