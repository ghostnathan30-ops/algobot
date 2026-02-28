"""
AlgoBot — Monte Carlo Simulation
===================================
Module:  src/backtest/monte_carlo.py
Phase:   3 — Backtesting Engine
Purpose: Stress-tests strategy robustness by shuffling trade order 10,000×.

Why Monte Carlo matters:
  In a real backtest, trades arrive in chronological order. But markets don't
  always cluster wins and losses the same way twice. Monte Carlo answers:
    "What if our trades had arrived in a different order?"

  Specifically, it probes the distribution of worst-case drawdowns.
  A strategy that achieves 18% max drawdown in one ordering might produce
  40% drawdown in an unlucky ordering. We need to know this BEFORE going live.

Method:
  1. Take the list of closed trades from the backtest
  2. Shuffle the trade order randomly 10,000 times
  3. For each shuffle, compute the equity curve and max drawdown
  4. Report the 95th percentile worst drawdown

Pass criteria (from config.yaml):
  monte_carlo.max_dd_95th_percentile_limit: 35.0
  The 95th percentile max drawdown must be below 35% of equity.

Usage:
    from src.backtest.monte_carlo import run_monte_carlo

    mc_result = run_monte_carlo(backtest_result.trades, config)
    print(f"95th pct DD: {mc_result['dd_95th_pct']:.1f}%")
    print(f"Passed: {mc_result['passed']}")
"""

from __future__ import annotations

import random

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Monte Carlo runner ────────────────────────────────────────────────────────

def run_monte_carlo(
    trades: list,
    config: dict,
    n_simulations: int = 10_000,
    initial_capital: float = 150_000.0,
    seed: int = 42,
) -> dict:
    """
    Run Monte Carlo simulation by shuffling trade order.

    Args:
        trades:          list of Trade objects from BacktestEngine
        config:          Full config dict (for pass threshold)
        n_simulations:   Number of shuffled simulations (default 10,000)
        initial_capital: Starting equity for each simulation
        seed:            Random seed for reproducibility (42 by default)

    Returns:
        dict with keys:
          "passed"          — bool: True if 95th pct DD < limit
          "dd_95th_pct"     — 95th percentile max drawdown (negative %)
          "dd_median_pct"   — Median max drawdown
          "dd_worst_pct"    — Worst max drawdown across all simulations
          "dd_best_pct"     — Best (smallest) max drawdown
          "dd_distribution" — list of all max drawdowns (for histogram)
          "final_eq_50th"   — Median final equity
          "final_eq_5th"    — 5th percentile final equity (bad-luck scenario)
          "n_simulations"   — Number of simulations run
          "n_trades"        — Number of trades shuffled
          "fail_reasons"    — list of strings if failed
    """
    mc_cfg   = config.get("backtest", {}).get("monte_carlo", {})
    dd_limit = float(mc_cfg.get("max_dd_95th_percentile_limit", 35.0))

    if not trades:
        log.warning("Monte Carlo: No trades to simulate")
        return {
            "passed": False, "dd_95th_pct": 0.0, "dd_median_pct": 0.0,
            "dd_worst_pct": 0.0, "dd_best_pct": 0.0,
            "dd_distribution": [], "final_eq_50th": initial_capital,
            "final_eq_5th": initial_capital, "n_simulations": 0,
            "n_trades": 0, "fail_reasons": ["No trades available"],
        }

    log.info(
        "Monte Carlo: {n_sim:,} simulations, {n_tr} trades, "
        "initial=${cap:,.0f}",
        n_sim=n_simulations, n_tr=len(trades), cap=initial_capital,
    )

    # Extract trade P&Ls as a numpy array (fast shuffling)
    pnl_array = np.array([t.pnl_net for t in trades], dtype=np.float64)

    rng = np.random.default_rng(seed)

    max_drawdowns = []
    final_equities = []

    for _ in range(n_simulations):
        # Shuffle trade order
        shuffled = rng.permutation(pnl_array)

        # Build equity curve
        equity = initial_capital + np.cumsum(shuffled)
        equity = np.concatenate([[initial_capital], equity])

        # Calculate max drawdown
        peak    = np.maximum.accumulate(equity)
        dd      = (equity - peak) / peak * 100.0
        max_dd  = float(dd.min())

        max_drawdowns.append(max_dd)
        final_equities.append(float(equity[-1]))

    max_drawdowns  = sorted(max_drawdowns)   # Sort ascending (most negative first)
    final_equities = sorted(final_equities)

    # Percentiles (95th pct drawdown = 95th worst = index 9500 of 10000)
    dd_95th  = float(np.percentile(max_drawdowns, 5))   # 5th pct of sorted = 95th worst
    dd_median = float(np.percentile(max_drawdowns, 50))
    dd_worst = float(max_drawdowns[0])
    dd_best  = float(max_drawdowns[-1])

    # Final equity percentiles
    eq_50th = float(np.percentile(final_equities, 50))
    eq_5th  = float(np.percentile(final_equities, 5))

    passed = abs(dd_95th) <= dd_limit
    fail_reasons = []
    if not passed:
        fail_reasons.append(
            f"95th percentile max DD {abs(dd_95th):.1f}% exceeds limit {dd_limit}%"
        )

    log.info(
        "Monte Carlo: DD 95th={d95:.1f}% | Median={med:.1f}% | Worst={worst:.1f}% | "
        "Limit={lim:.1f}% | {result}",
        d95=dd_95th, med=dd_median, worst=dd_worst,
        lim=-dd_limit,
        result="PASS" if passed else "FAIL",
    )

    return {
        "passed":          passed,
        "dd_95th_pct":     round(dd_95th, 2),
        "dd_median_pct":   round(dd_median, 2),
        "dd_worst_pct":    round(dd_worst, 2),
        "dd_best_pct":     round(dd_best, 2),
        "dd_distribution": max_drawdowns,
        "final_eq_50th":   round(eq_50th, 2),
        "final_eq_5th":    round(eq_5th, 2),
        "n_simulations":   n_simulations,
        "n_trades":        len(trades),
        "fail_reasons":    fail_reasons,
    }
