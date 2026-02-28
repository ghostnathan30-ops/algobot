"""
AlgoBot — Stress Testing
==========================
Module:  src/backtest/stress_tester.py
Phase:   4 — Validation Suite
Purpose: Tests robustness of backtest results to adverse conditions.

Why stress testing matters:
  A strategy that works under normal backtest conditions can fail when
  transaction costs are higher than expected (live execution), when a
  small number of lucky trades drove most of the P&L, or when position
  sizes are forced smaller by drawdown or account limits.

  Stress tests probe the question: "How fragile is this edge?"

Tests implemented:
  Test 1 — Double Costs:         Commission ×2, slippage ×2
  Test 2 — Remove Best Trades:   Remove top 20 trades by P&L, check edge holds
  Test 3 — Risk Scaling Down:    Position sizes at 80% of normal
  Test 4 — Risk Scaling Up:      Position sizes at 120% of normal (amplified risk)
  Test 5 — Cost Sweep:           1×/1.5×/2×/3× costs — find the break-even point

Pass criteria (from config.yaml validation.stress_tests):
  test_double_costs:       PF > 1.5 after doubled costs
  test_remove_best_20:     PF > 1.5 after removing best 20 trades
  test_risk_scale_down:    PF > 1.5 after 80% position scaling

Usage:
    from src.backtest.stress_tester import run_all_stress_tests
    from src.backtest.engine import BacktestEngine

    result = engine.run(market_data, "2000-01-01", "2019-12-31")
    suite  = run_all_stress_tests(result, config)
    print(f"Stress tests: {'PASS' if suite.all_passed else 'FAIL'}")
    for r in suite.results:
        print(f"  {r.test_name}: PF={r.profit_factor:.2f} {'PASS' if r.passed else 'FAIL'}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.backtest.metrics import (
    profit_factor, sharpe_ratio, expectancy_per_trade,
    win_rate, avg_win_loss_ratio, max_drawdown
)
from src.utils.logger import get_logger

log = get_logger(__name__)

# Match engine constants
SLIPPAGE_PCT_PER_SIDE = 0.0005   # 0.05% per side
COMMISSION_PER_RT     = 10.0     # $10 round-turn


# ── Result containers ─────────────────────────────────────────────────────────

@dataclass
class StressResult:
    """Result of one stress test."""
    test_name:      str
    description:    str
    passed:         bool
    profit_factor:  float
    expectancy:     float    # $ per trade
    win_rate_pct:   float
    total_pnl:      float
    n_trades:       int
    threshold_pf:   float
    fail_reasons:   list = field(default_factory=list)

    def __str__(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}] {self.test_name}: "
            f"PF={self.profit_factor:.2f} (limit >{self.threshold_pf:.1f}) | "
            f"E[trade]=${self.expectancy:+.0f} | WR={self.win_rate_pct:.0f}%"
        )


@dataclass
class StressSuite:
    """Collection of all stress test results."""
    results:     list     # list[StressResult]
    all_passed:  bool
    fail_reasons: list = field(default_factory=list)
    summary:     dict = field(default_factory=dict)

    def __str__(self) -> str:
        n_pass = sum(1 for r in self.results if r.passed)
        return (
            f"StressSuite: {n_pass}/{len(self.results)} passed | "
            f"Overall: {'PASS' if self.all_passed else 'FAIL'}"
        )


# ── Helper: compute metrics from adjusted P&L list ───────────────────────────

def _metrics_from_pnl_list(pnl_list: list, initial_capital: float = 150_000.0) -> dict:
    """
    Compute basic metrics from a list of P&L values (not Trade objects).
    Used internally by stress tests that modify trade P&Ls.
    """
    if not pnl_list:
        return {"profit_factor": 0.0, "expectancy": 0.0, "win_rate": 0.0, "total_pnl": 0.0}

    wins   = [p for p in pnl_list if p > 0]
    losses = [p for p in pnl_list if p < 0]

    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))

    pf  = gross_profit / gross_loss if gross_loss > 0 else (math.inf if gross_profit > 0 else 0.0)
    exp = sum(pnl_list) / len(pnl_list)
    wr  = len(wins) / len(pnl_list) * 100.0

    return {
        "profit_factor": float(round(pf, 4)),
        "expectancy":    float(round(exp, 2)),
        "win_rate":      float(round(wr, 2)),
        "total_pnl":     float(round(sum(pnl_list), 2)),
    }


# ── Stress Test 1: Double Costs ───────────────────────────────────────────────

def test_double_costs(
    trades: list,
    initial_capital: float = 150_000.0,
    min_pf: float = 1.5,
) -> StressResult:
    """
    Apply 2× commission AND 2× slippage to every trade.

    Models the scenario where live execution costs are twice what we modelled:
    - Higher commission tiers (e.g., broker charges $20 instead of $10)
    - Wider spreads / worse fills in fast markets

    Method:
      Original costs per trade = commission ($10) + slippage (0.05% × position)
      Doubled costs  = extra $10 commission + extra 0.05% slippage × position value

    Since Trade stores entry_price_adj, exit_price_adj, and position_size,
    we can accurately calculate the extra slippage without rerunning the engine.

    Args:
        trades:          list of Trade objects from BacktestEngine
        initial_capital: for context only (not used in metric calc)
        min_pf:          pass threshold

    Returns:
        StressResult
    """
    if not trades:
        return StressResult(
            test_name="Double Costs", description="No trades",
            passed=False, profit_factor=0.0, expectancy=0.0,
            win_rate_pct=0.0, total_pnl=0.0, n_trades=0,
            threshold_pf=min_pf,
            fail_reasons=["No trades to stress-test"],
        )

    adjusted_pnls = []
    for t in trades:
        # Extra slippage: 0.05% × (entry + exit price) × size × point_value
        extra_slippage = (
            (t.entry_price_adj + t.exit_price_adj)
            * SLIPPAGE_PCT_PER_SIDE
            * t.position_size
            * t.point_value
        )
        extra_commission = COMMISSION_PER_RT  # Another $10
        adj_pnl = t.pnl_net - extra_slippage - extra_commission
        adjusted_pnls.append(adj_pnl)

    m = _metrics_from_pnl_list(adjusted_pnls, initial_capital)
    passed = m["profit_factor"] >= min_pf

    total_extra_cost = sum(
        (t.entry_price_adj + t.exit_price_adj) * SLIPPAGE_PCT_PER_SIDE * t.position_size * t.point_value
        + COMMISSION_PER_RT
        for t in trades
    )

    fail_reasons = []
    if not passed:
        fail_reasons.append(
            f"PF with double costs={m['profit_factor']:.2f} < {min_pf} limit"
        )

    log.info(
        "Stress Test — Double Costs: PF={pf:.2f} (limit {lim}) | "
        "Extra costs=${ec:,.0f} total | {result}",
        pf=m["profit_factor"], lim=min_pf,
        ec=total_extra_cost,
        result="PASS" if passed else "FAIL",
    )

    return StressResult(
        test_name="Double Costs",
        description=f"Commission ×2 ($20), slippage ×2 (0.1%/side) | Extra cost: ${total_extra_cost:,.0f}",
        passed=passed,
        profit_factor=m["profit_factor"],
        expectancy=m["expectancy"],
        win_rate_pct=m["win_rate"],
        total_pnl=m["total_pnl"],
        n_trades=len(trades),
        threshold_pf=min_pf,
        fail_reasons=fail_reasons,
    )


# ── Stress Test 2: Remove Best N Trades ──────────────────────────────────────

def test_remove_best_trades(
    trades: list,
    n: int = 20,
    initial_capital: float = 150_000.0,
    min_pf: float = 1.5,
) -> StressResult:
    """
    Remove the N best trades (by net P&L) and check the strategy still has edge.

    This answers: "Was our performance driven by a handful of lucky trades,
    or is the edge distributed across many trades?"

    A strategy that fails this test is fragile — a few bad luck events in live
    trading could wipe out the entire edge. A strategy that passes has broad,
    distributed edge that doesn't depend on outlier wins.

    Args:
        trades:  list of Trade objects
        n:       number of best trades to remove (default 20)
        min_pf:  pass threshold after removal

    Returns:
        StressResult
    """
    if not trades:
        return StressResult(
            test_name=f"Remove Best {n} Trades", description="No trades",
            passed=False, profit_factor=0.0, expectancy=0.0,
            win_rate_pct=0.0, total_pnl=0.0, n_trades=0,
            threshold_pf=min_pf,
            fail_reasons=["No trades to stress-test"],
        )

    actual_n = min(n, len(trades))
    sorted_trades = sorted(trades, key=lambda t: t.pnl_net, reverse=True)
    remaining = sorted_trades[actual_n:]

    if not remaining:
        return StressResult(
            test_name=f"Remove Best {n} Trades",
            description=f"Removed all {actual_n} trades",
            passed=False, profit_factor=0.0, expectancy=0.0,
            win_rate_pct=0.0, total_pnl=0.0, n_trades=0,
            threshold_pf=min_pf,
            fail_reasons=["No trades remain after removal"],
        )

    pnl_list = [t.pnl_net for t in remaining]
    m = _metrics_from_pnl_list(pnl_list, initial_capital)
    passed = m["profit_factor"] >= min_pf

    removed_pnl = sum(t.pnl_net for t in sorted_trades[:actual_n])
    best_trade   = sorted_trades[0].pnl_net if sorted_trades else 0

    fail_reasons = []
    if not passed:
        fail_reasons.append(
            f"PF after removing best {actual_n}={m['profit_factor']:.2f} < {min_pf} limit"
        )

    log.info(
        "Stress Test — Remove Best {n} Trades: PF={pf:.2f} (limit {lim}) | "
        "Removed P&L=${rpnl:+,.0f} | Remaining={rem} trades | {result}",
        n=actual_n, pf=m["profit_factor"], lim=min_pf,
        rpnl=removed_pnl, rem=len(remaining),
        result="PASS" if passed else "FAIL",
    )

    return StressResult(
        test_name=f"Remove Best {actual_n} Trades",
        description=(
            f"Removed top {actual_n} trades (${removed_pnl:+,.0f} total). "
            f"Best single trade: ${best_trade:+,.0f}. "
            f"Remaining: {len(remaining)} trades."
        ),
        passed=passed,
        profit_factor=m["profit_factor"],
        expectancy=m["expectancy"],
        win_rate_pct=m["win_rate"],
        total_pnl=m["total_pnl"],
        n_trades=len(remaining),
        threshold_pf=min_pf,
        fail_reasons=fail_reasons,
    )


# ── Stress Test 3: Risk Scaling ───────────────────────────────────────────────

def test_risk_scaling(
    trades: list,
    scale: float = 0.8,
    initial_capital: float = 150_000.0,
    min_pf: float = 1.5,
) -> StressResult:
    """
    Scale all position sizes by a factor and check strategy viability.

    Models: Topstep trailing drawdown forces account into reduced-risk mode,
    or risk manager manually reduces position size to 80% for conservative phase.

    Commission stays fixed (not position-size dependent).
    Gross P&L scales proportionally. Net P&L = pnl_gross × scale - commission.

    Args:
        trades: list of Trade objects
        scale:  size multiplier (0.8 = 80% of normal, 0.5 = half size)
        min_pf: pass threshold
    """
    if not trades:
        return StressResult(
            test_name=f"Risk Scale {scale:.0%}", description="No trades",
            passed=False, profit_factor=0.0, expectancy=0.0,
            win_rate_pct=0.0, total_pnl=0.0, n_trades=0,
            threshold_pf=min_pf,
            fail_reasons=["No trades to stress-test"],
        )

    adjusted_pnls = [
        t.pnl_gross * scale - t.commission
        for t in trades
    ]

    m = _metrics_from_pnl_list(adjusted_pnls, initial_capital)
    passed = m["profit_factor"] >= min_pf

    fail_reasons = []
    if not passed:
        fail_reasons.append(
            f"PF at {scale:.0%} scale={m['profit_factor']:.2f} < {min_pf} limit"
        )

    log.info(
        "Stress Test — Risk Scale {sc:.0%}: PF={pf:.2f} (limit {lim}) | "
        "Total P&L=${pnl:+,.0f} | {result}",
        sc=scale, pf=m["profit_factor"], lim=min_pf,
        pnl=m["total_pnl"],
        result="PASS" if passed else "FAIL",
    )

    return StressResult(
        test_name=f"Risk Scale {scale:.0%}",
        description=f"All position sizes at {scale:.0%} of normal. Commission unchanged.",
        passed=passed,
        profit_factor=m["profit_factor"],
        expectancy=m["expectancy"],
        win_rate_pct=m["win_rate"],
        total_pnl=m["total_pnl"],
        n_trades=len(trades),
        threshold_pf=min_pf,
        fail_reasons=fail_reasons,
    )


# ── Stress Test 4: Cost Sweep ─────────────────────────────────────────────────

def test_cost_sweep(
    trades: list,
    cost_multipliers: list = None,
    initial_capital: float = 150_000.0,
) -> list:
    """
    Sweep through cost multipliers to find the break-even cost level.

    Returns a list of StressResult (one per multiplier) — NOT subject to
    pass/fail threshold. Used for informational reporting only.

    Args:
        trades:            list of Trade objects
        cost_multipliers:  list of multipliers to test (default: 1.0, 1.5, 2.0, 3.0)

    Returns:
        list of StressResult, each with threshold_pf=1.0 (break-even check)
    """
    if cost_multipliers is None:
        cost_multipliers = [1.0, 1.5, 2.0, 3.0, 4.0]

    results = []
    for mult in cost_multipliers:
        extra_comm_factor = mult - 1.0
        adjusted_pnls = []
        for t in trades:
            extra_slip = (
                (t.entry_price_adj + t.exit_price_adj)
                * SLIPPAGE_PCT_PER_SIDE
                * t.position_size
                * t.point_value
                * extra_comm_factor
            )
            extra_comm = COMMISSION_PER_RT * extra_comm_factor
            adjusted_pnls.append(t.pnl_net - extra_slip - extra_comm)

        m = _metrics_from_pnl_list(adjusted_pnls)
        results.append(StressResult(
            test_name=f"Cost {mult:.1f}×",
            description=f"Transaction costs at {mult:.1f}× normal",
            passed=m["profit_factor"] >= 1.0,
            profit_factor=m["profit_factor"],
            expectancy=m["expectancy"],
            win_rate_pct=m["win_rate"],
            total_pnl=m["total_pnl"],
            n_trades=len(trades),
            threshold_pf=1.0,
        ))

    log.info(
        "Cost sweep: {results}",
        results=" | ".join(
            f"{r.test_name} PF={r.profit_factor:.2f}"
            for r in results
        ),
    )
    return results


# ── Master runner ─────────────────────────────────────────────────────────────

def run_all_stress_tests(
    result,                      # BacktestResult
    config: dict,
    initial_capital: float = 150_000.0,
) -> StressSuite:
    """
    Run all mandatory stress tests on a BacktestResult.

    Args:
        result:          BacktestResult from BacktestEngine.run()
        config:          Full config dict (for thresholds)
        initial_capital: Starting equity

    Returns:
        StressSuite with all results and overall pass/fail.
    """
    stress_cfg = config.get("validation", {}).get("stress_tests", {})
    min_pf     = float(stress_cfg.get("min_pf_after_stress", 1.5))
    n_remove   = int(stress_cfg.get("remove_best_n_trades", 20))

    trades = result.trades

    log.info(
        "Running stress tests on {n} trades | min_pf_threshold={p}",
        n=len(trades), p=min_pf,
    )

    # ── Run the three mandatory tests ─────────────────────────────────────────
    r_costs  = test_double_costs(trades, initial_capital, min_pf)
    r_remove = test_remove_best_trades(trades, n_remove, initial_capital, min_pf)
    r_scale  = test_risk_scaling(trades, 0.8, initial_capital, min_pf)

    mandatory = [r_costs, r_remove, r_scale]

    # ── Cost sweep (informational) ─────────────────────────────────────────────
    sweep = test_cost_sweep(trades, [1.0, 1.5, 2.0, 3.0], initial_capital)

    all_results  = mandatory + sweep
    all_passed   = all(r.passed for r in mandatory)
    fail_reasons = [f for r in mandatory for f in r.fail_reasons]

    n_mandatory_pass = sum(1 for r in mandatory if r.passed)

    summary = {
        "mandatory_tests":  len(mandatory),
        "mandatory_passed": n_mandatory_pass,
        "original_pf":      result.metrics.get("profit_factor", 0),
        "double_cost_pf":   r_costs.profit_factor,
        "remove_best_pf":   r_remove.profit_factor,
        "scale_80pct_pf":   r_scale.profit_factor,
        "cost_sweep": {
            r.test_name: r.profit_factor for r in sweep
        },
    }

    log.info(
        "Stress tests complete: {n}/{total} mandatory passed | Overall: {result}",
        n=n_mandatory_pass, total=len(mandatory),
        result="PASS" if all_passed else "FAIL",
    )

    return StressSuite(
        results=all_results,
        all_passed=all_passed,
        fail_reasons=fail_reasons,
        summary=summary,
    )
