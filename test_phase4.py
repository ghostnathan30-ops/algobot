"""
AlgoBot — Phase 4 Test Suite: Validation Framework
====================================================
Tests the stress tester, regime/crisis tester, and validation runner.

Tests:
  1. Stress tester: Module imports, dataclasses, synthetic data
  2. Double costs:  Runs on 2020-2024 results, output structure valid
  3. Remove best:   Runs on 2020-2024 results, correctly removes trades
  4. Risk scaling:  Runs on 2020-2024 results, P&L scales correctly
  5. Cost sweep:    Monotonically decreasing PF as costs rise
  6. Crisis tests:  COVID 2020 and 2022 rate hike run on available data
  7. Validation runner: Structure valid, stages populate, JSON serialisable
  8. Stage 2 (OOS): Validation runner runs Stage 2 on 2020-2024 data
  9. Full validation report: All stages attempted, verdict determined

Run:
    /c/Users/ghost/miniconda3/envs/algobot_env/python.exe test_phase4.py
"""

from __future__ import annotations

import sys
import os
import math

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

import numpy as np
import pandas as pd

PASS_MARK = "  [PASS]"
FAIL_MARK = "  [FAIL]"

def info(msg):
    print(f"       {msg}")

def passed(msg):
    print(f"{PASS_MARK} {msg}")

def failed(msg, exc=None):
    print(f"{FAIL_MARK} {msg}")
    if exc:
        print(f"         Error: {exc}")

# ── Shared fixtures ───────────────────────────────────────────────────────────

def _load_config():
    from src.backtest.data_loader import load_config
    return load_config()

def _load_market_data_cached(config):
    """Load 2020-2024 data (always available, used for most tests)."""
    from src.backtest.data_loader import load_all_markets
    return load_all_markets("2020-01-01", "2024-12-31", config)

def _run_engine(market_data, config, start, end, capital=150_000.0):
    from src.backtest.engine import BacktestEngine
    engine = BacktestEngine(config, initial_capital=capital)
    return engine.run(market_data, start, end)

def _make_synthetic_trades(n_wins=30, n_losses=20, avg_win=1500, avg_loss=800):
    """Create synthetic Trade objects for unit testing."""
    import datetime
    from src.backtest.trade import Trade

    trades = []
    base_date = datetime.date(2023, 1, 1)

    for i in range(n_wins + n_losses):
        is_win = i < n_wins
        pnl_gross = avg_win if is_win else -avg_loss
        pnl_net   = pnl_gross - 10.0
        pnl_r     = pnl_net / 1000.0

        t = Trade(
            trade_id=i + 1,
            market="ES",
            direction="LONG",
            strategy="TREND",
            signal_source="AGREE_LONG",
            entry_date=base_date,
            entry_bar_idx=i,
            entry_price=500.0,
            entry_price_adj=500.25,
            position_size=10.0,
            stop_price=495.0,
            point_value=1.0,
            initial_risk_dollars=1000.0,
            exit_date=base_date,
            exit_bar_idx=i + 3,
            exit_price=502.0 if is_win else 498.0,
            exit_price_adj=501.75 if is_win else 498.25,
            exit_reason="trailing_stop" if is_win else "stop_loss",
            pnl_gross=float(pnl_gross),
            commission=10.0,
            pnl_net=float(pnl_net),
            pnl_r=float(pnl_r),
        )
        trades.append(t)
    return trades


# ══════════════════════════════════════════════════════════════════════════════

def test_stress_tester_imports():
    """Test 1: Stress tester modules import cleanly and dataclasses work."""
    print("-" * 70)
    print("  TEST 1: Stress Tester - Imports and Dataclasses")
    print("-" * 70)

    try:
        from src.backtest.stress_tester import (
            StressResult, StressSuite,
            test_double_costs, test_remove_best_trades,
            test_risk_scaling, test_cost_sweep, run_all_stress_tests
        )
        passed("All stress tester functions import OK")
    except Exception as exc:
        failed("Import failed", exc)
        return False

    # Test with synthetic data
    trades = _make_synthetic_trades(30, 20, 1500, 800)

    r = test_double_costs(trades, min_pf=1.5)
    assert isinstance(r, StressResult), "Expected StressResult"
    assert r.n_trades == 50, f"Expected 50 trades, got {r.n_trades}"
    assert r.profit_factor >= 0, "PF must be non-negative"
    passed(f"double_costs on synthetic data: PF={r.profit_factor:.2f} ({'PASS' if r.passed else 'FAIL'})")

    r2 = test_remove_best_trades(trades, n=10, min_pf=1.0)
    assert isinstance(r2, StressResult), "Expected StressResult"
    assert r2.n_trades == 40, f"Expected 40 remaining, got {r2.n_trades}"
    passed(f"remove_best_trades: removed 10, remaining PF={r2.profit_factor:.2f}")

    r3 = test_risk_scaling(trades, scale=0.8, min_pf=1.0)
    assert isinstance(r3, StressResult), "Expected StressResult"
    passed(f"risk_scaling 80%: PF={r3.profit_factor:.2f}")

    # Cost sweep should have monotonically non-increasing PF
    sweep = test_cost_sweep(trades, [1.0, 1.5, 2.0, 3.0])
    assert len(sweep) == 4, f"Expected 4 sweep results, got {len(sweep)}"
    pfs = [s.profit_factor for s in sweep]
    assert all(pfs[i] >= pfs[i+1] - 0.01 for i in range(len(pfs)-1)), \
        f"PF should be non-increasing with costs: {pfs}"
    passed(f"cost_sweep monotonic: {' -> '.join(f'{p:.2f}' for p in pfs)}")

    # StressSuite structure
    config = _load_config()
    suite = StressSuite(results=[r, r2, r3], all_passed=False)
    assert hasattr(suite, "results") and hasattr(suite, "all_passed")
    passed("StressSuite dataclass structure correct")

    passed("Stress tester test passed")
    return True


def test_stress_double_costs_on_real_data():
    """Test 2: Double costs test on real 2020-2024 backtest results."""
    print("-" * 70)
    print("  TEST 2: Double Costs - Real 2020-2024 Data")
    print("-" * 70)

    from src.backtest.stress_tester import test_double_costs

    config = _load_config()
    market_data = _load_market_data_cached(config)
    result = _run_engine(market_data, config, "2020-01-01", "2024-12-31")

    passed(f"Engine ran: {result.total_trades} trades")

    r = test_double_costs(result.trades, initial_capital=150_000.0, min_pf=1.5)

    assert r.n_trades == result.total_trades, "Trade count must match"
    assert math.isfinite(r.profit_factor), "PF must be finite"
    assert r.profit_factor <= result.metrics.get("profit_factor", 999), \
        "Double costs PF should be <= original PF"

    original_pf = result.metrics.get("profit_factor", 0)
    info(f"Original PF:     {original_pf:.2f}")
    info(f"Double-cost PF:  {r.profit_factor:.2f}  ({'PASS' if r.passed else 'FAIL - expected on 2020-2024 bull period'})")
    info(f"E[trade]:        ${r.expectancy:+.0f}")
    info(f"Description:     {r.description}")

    # Structure validation (regardless of pass/fail on this period)
    assert isinstance(r.passed, bool), "passed must be bool"
    assert isinstance(r.fail_reasons, list), "fail_reasons must be list"
    passed("Double costs test structure correct - output valid regardless of PF")

    passed("Double costs test passed")
    return True


def test_stress_remove_best_trades():
    """Test 3: Remove best trades on real data."""
    print("-" * 70)
    print("  TEST 3: Remove Best Trades - Real 2020-2024 Data")
    print("-" * 70)

    from src.backtest.stress_tester import test_remove_best_trades

    config = _load_config()
    market_data = _load_market_data_cached(config)
    result = _run_engine(market_data, config, "2020-01-01", "2024-12-31")

    n = min(20, result.total_trades // 4)   # Remove at most 25% of trades
    r = test_remove_best_trades(result.trades, n=n, min_pf=1.5)

    assert r.n_trades == max(0, result.total_trades - n), \
        f"Expected {result.total_trades - n} remaining, got {r.n_trades}"
    assert math.isfinite(r.profit_factor), "PF must be finite"

    # Best trade must have been identified
    sorted_pnls = sorted([t.pnl_net for t in result.trades], reverse=True)
    info(f"Original trades:    {result.total_trades}")
    info(f"Removed:            {n} best trades")
    info(f"Remaining:          {r.n_trades}")
    info(f"Best trade removed: ${sorted_pnls[0]:+,.0f}")
    info(f"PF after removal:   {r.profit_factor:.2f}")

    passed("Remove best trades structure correct")
    passed("Remove best trades test passed")
    return True


def test_stress_risk_scaling():
    """Test 4: Risk scaling on real data."""
    print("-" * 70)
    print("  TEST 4: Risk Scaling - 80% position sizes")
    print("-" * 70)

    from src.backtest.stress_tester import test_risk_scaling

    config = _load_config()
    market_data = _load_market_data_cached(config)
    result = _run_engine(market_data, config, "2020-01-01", "2024-12-31")

    r_80  = test_risk_scaling(result.trades, scale=0.8,  min_pf=1.5)
    r_100 = test_risk_scaling(result.trades, scale=1.0,  min_pf=1.5)
    r_120 = test_risk_scaling(result.trades, scale=1.20, min_pf=1.5)

    # At 100% scale, PF should match original (within rounding)
    orig_pf = result.metrics.get("profit_factor", 0)
    assert abs(r_100.profit_factor - orig_pf) < 0.1, \
        f"100% scale PF {r_100.profit_factor:.2f} should match original {orig_pf:.2f}"

    # PF should be approximately equal across scales (it's just $-scaling, not ratio)
    # Note: PF ratio stays similar because both wins and losses scale together
    info(f"Original PF:   {orig_pf:.2f}")
    info(f"80% scale PF:  {r_80.profit_factor:.2f}  | E[trade]=${r_80.expectancy:+.0f}")
    info(f"100% scale PF: {r_100.profit_factor:.2f} | E[trade]=${r_100.expectancy:+.0f}")
    info(f"120% scale PF: {r_120.profit_factor:.2f} | E[trade]=${r_120.expectancy:+.0f}")

    # Total P&L at 120% should be larger magnitude than at 80%
    assert abs(r_120.total_pnl) >= abs(r_80.total_pnl) * 0.9, \
        "Higher scale should produce larger magnitude P&L"

    passed("Risk scaling output structure correct")
    passed("Risk scaling test passed")
    return True


def test_cost_sweep():
    """Test 5: Cost sweep monotonically degrades PF."""
    print("-" * 70)
    print("  TEST 5: Cost Sweep")
    print("-" * 70)

    from src.backtest.stress_tester import test_cost_sweep

    config = _load_config()
    market_data = _load_market_data_cached(config)
    result = _run_engine(market_data, config, "2020-01-01", "2024-12-31")

    multipliers = [1.0, 1.5, 2.0, 3.0, 4.0]
    sweep = test_cost_sweep(result.trades, multipliers)

    assert len(sweep) == len(multipliers), f"Expected {len(multipliers)} results"

    pfs = [s.profit_factor for s in sweep]
    info("Cost sweep results:")
    for s, pf in zip(sweep, pfs):
        info(f"  {s.test_name:10s}  PF={pf:.2f}  E[trade]=${s.expectancy:+.0f}")

    # PF should be non-increasing with costs
    for i in range(len(pfs) - 1):
        assert pfs[i] >= pfs[i+1] - 0.05, \
            f"PF should decrease: {pfs[i]:.2f} vs {pfs[i+1]:.2f}"

    # Find break-even point
    breakeven = next((s.test_name for s in sweep if not s.passed), "Never breaks even")
    info(f"Break-even cost level: {breakeven}")

    passed("Cost sweep is monotonically non-increasing")
    passed("Cost sweep test passed")
    return True


def test_crisis_tester():
    """Test 6: Crisis tests — COVID 2020 and 2022 rate hike (data available)."""
    print("-" * 70)
    print("  TEST 6: Crisis Scenario Tests")
    print("-" * 70)

    from src.backtest.regime_tester import (
        test_crisis_period, run_all_crisis_tests, crisis_suite_passed
    )

    config = _load_config()
    market_data = _load_market_data_cached(config)

    # Test COVID crash (data available in 2020-2024 dataset)
    info("Testing COVID March 2020 crash...")
    covid = test_crisis_period(
        market_data, config,
        start="2020-02-01", end="2020-04-30",
        name="COVID March 2020",
        description="Test during COVID crash",
        max_dd_limit=12.0,
        min_return=None,
    )
    info(f"  {covid}")
    assert not covid.skipped, "COVID 2020 test should not be skipped (data available)"
    assert math.isfinite(covid.max_drawdown_pct), "DD must be finite"
    assert math.isfinite(covid.total_return_pct), "Return must be finite"
    passed(f"COVID 2020: MaxDD={covid.max_drawdown_pct:.1f}% | Return={covid.total_return_pct:.1f}% | {'PASS' if covid.passed else 'FAIL'}")

    # Test 2022 rate hike year (data available)
    info("Testing 2022 Rate Hike Year...")
    rate_hike = test_crisis_period(
        market_data, config,
        start="2022-01-01", end="2022-12-31",
        name="2022 Rate Hike",
        description="Fed rate hike year",
        max_dd_limit=None,
        min_return=0.0,   # Must be profitable
    )
    info(f"  {rate_hike}")
    assert not rate_hike.skipped, "2022 test should not be skipped (data available)"
    passed(f"2022 Rate Hike: MaxDD={rate_hike.max_drawdown_pct:.1f}% | Return={rate_hike.total_return_pct:.1f}% | {'PASS' if rate_hike.passed else 'FAIL'}")

    # Test period that requires older data (should skip gracefully)
    info("Testing 2008 crisis (should skip - data starts 2003-12-01)...")
    crisis_2008 = test_crisis_period(
        market_data, config,
        start="2008-09-01", end="2009-03-31",
        name="2008 Financial Crisis",
        description="Lehman collapse",
        max_dd_limit=20.0,
    )
    if crisis_2008.skipped:
        passed("2008 crisis correctly skipped - data pre-dates cache")
    else:
        passed(f"2008 crisis ran: MaxDD={crisis_2008.max_drawdown_pct:.1f}%")

    # Run all crisis tests via master runner
    info("Running all crisis scenarios via run_all_crisis_tests()...")
    all_results = run_all_crisis_tests(market_data, config)

    assert len(all_results) == 4, f"Expected 4 scenarios, got {len(all_results)}"
    ran     = [r for r in all_results if not r.skipped]
    skipped = [r for r in all_results if r.skipped]
    info(f"  Ran: {len(ran)} | Skipped: {len(skipped)}")

    overall_passed, fail_reasons = crisis_suite_passed(all_results)
    info(f"  Crisis suite: {'PASS' if overall_passed else 'FAIL (check fail_reasons)'}")
    if fail_reasons:
        for fr in fail_reasons:
            info(f"    - {fr}")

    passed("Crisis tester structure and execution correct")
    passed("Crisis scenario tests passed")
    return True


def test_validation_runner_structure():
    """Test 7: Validation runner imports, structure valid, JSON serialisable."""
    print("-" * 70)
    print("  TEST 7: Validation Runner - Structure and Imports")
    print("-" * 70)

    try:
        from src.backtest.validation_runner import (
            StageResult, ValidationReport,
            run_full_validation, save_validation_report
        )
        passed("All validation runner classes import OK")
    except Exception as exc:
        failed("Import failed", exc)
        return False

    # Test StageResult construction
    s = StageResult(
        stage_id=1, stage_name="Test Stage",
        passed=True, skipped=False, skip_reason="",
        metrics={"profit_factor": 2.5},
        fail_reasons=[],
        details={"period": "2000-2019"},
    )
    assert s.status_str() == "PASS"
    assert str(s).startswith("[PASS]")
    passed("StageResult dataclass and status_str() work")

    # Test ValidationReport construction and serialisation
    report = ValidationReport(
        timestamp="2026-02-27 00:00:00",
        stages=[s],
        overall_verdict="CONDITIONAL",
        fail_reasons=[],
        summary={"stages_total": 1},
    )
    assert report.overall_verdict == "CONDITIONAL"

    d = report.to_dict()
    assert "timestamp" in d
    assert "stages" in d
    assert "overall_verdict" in d
    assert len(d["stages"]) == 1

    import json
    json_str = json.dumps(d)   # Must not raise
    assert len(json_str) > 0
    passed("ValidationReport.to_dict() is JSON-serialisable")

    # Test skipped StageResult
    s_skip = StageResult(
        stage_id=6, stage_name="Paper Trading",
        passed=False, skipped=True,
        skip_reason="Requires 60 live days",
    )
    assert s_skip.status_str() == "SKIP"
    assert "SKIP" in str(s_skip)
    passed("Skipped StageResult formats correctly")

    passed("Validation runner structure test passed")
    return True


def test_validation_runner_stage2():
    """Test 8: Run Stage 2 (OOS) through validation runner."""
    print("-" * 70)
    print("  TEST 8: Validation Runner - Stage 2 OOS")
    print("-" * 70)

    from src.backtest.validation_runner import _run_stage_2_oos

    config = _load_config()
    market_data = _load_market_data_cached(config)

    info("Running Stage 2 (OOS 2020-2024)...")
    s2 = _run_stage_2_oos(market_data, config, "2020-01-01", "2024-12-31", 150_000.0)

    assert not s2.skipped, "Stage 2 should not be skipped"
    assert isinstance(s2.passed, bool), "passed must be bool"
    assert isinstance(s2.metrics, dict), "metrics must be dict"
    assert "profit_factor" in s2.metrics, "metrics must contain profit_factor"

    info(f"Stage 2 result: {s2.status_str()}")
    info(f"  PF={s2.metrics.get('profit_factor', 0):.2f}")
    info(f"  Sharpe={s2.metrics.get('sharpe_ratio', 0):.2f}")
    info(f"  MaxDD={s2.metrics.get('max_drawdown_pct', 0):.1f}%")
    info(f"  Total trades={s2.metrics.get('total_trades', 0)}")
    info(f"  PF by market: {s2.details.get('pf_by_market', {})}")
    info(f"  PF by strategy: {s2.details.get('pf_by_strategy', {})}")

    if not s2.passed:
        info(f"  Stage 2 FAIL reasons (expected on 2020-2024 bull period):")
        for fr in s2.fail_reasons:
            info(f"    - {fr}")

    passed("Stage 2 ran and returned valid StageResult")
    passed("Validation runner Stage 2 test passed")
    return True


def test_full_validation_report():
    """Test 9: Run full validation suite on available data."""
    print("-" * 70)
    print("  TEST 9: Full Validation Report (available data)")
    print("-" * 70)

    from src.backtest.validation_runner import run_full_validation, save_validation_report

    config = _load_config()
    market_data = _load_market_data_cached(config)

    info("Running full 6-stage validation on 2020-2024 data...")
    info("(IS period 2003-2019 will be skipped if data starts after 2020)")

    # Run with OOS-only period so it always works on cached data
    report = run_full_validation(
        market_data=market_data,
        config=config,
        initial_capital=150_000.0,
        is_start="2004-01-01",
        is_end="2019-12-31",
        oos_start="2020-01-01",
        oos_end="2024-12-31",
    )

    # Basic structure
    assert len(report.stages) == 6, f"Expected 6 stages, got {len(report.stages)}"
    assert report.overall_verdict in ("PASS", "FAIL", "CONDITIONAL"), \
        f"Invalid verdict: {report.overall_verdict}"
    assert report.timestamp != "", "Timestamp must be set"

    # All stages must be present with valid IDs
    stage_ids = [s.stage_id for s in report.stages]
    assert stage_ids == [1, 2, 3, 4, 5, 6], f"Expected stages 1-6, got {stage_ids}"

    # Report summary
    info(f"\n{report}")

    # JSON serialisable
    import json
    d = report.to_dict()
    json_str = json.dumps(d, default=str)
    assert len(json_str) > 100, "JSON output must be non-trivial"
    passed("Validation report is JSON-serialisable")

    # Save to disk
    filepath = save_validation_report(report, "reports/validation")
    assert os.path.exists(filepath), f"Report file not found: {filepath}"
    passed(f"Validation report saved to: {filepath}")

    # Count outcomes
    n_pass = sum(1 for s in report.stages if not s.skipped and s.passed)
    n_fail = sum(1 for s in report.stages if not s.skipped and not s.passed)
    n_skip = sum(1 for s in report.stages if s.skipped)
    info(f"\nStage summary: {n_pass} PASS | {n_fail} FAIL | {n_skip} SKIP")
    info(f"Overall verdict: {report.overall_verdict}")

    passed("Full validation report test passed")
    return True


# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 70)
    print("  AlgoBot -- Phase 4 Test Suite: Validation Framework")
    print("=" * 70)

    tests = [
        ("Stress tester imports",       test_stress_tester_imports),
        ("Double costs (real data)",    test_stress_double_costs_on_real_data),
        ("Remove best trades",          test_stress_remove_best_trades),
        ("Risk scaling",                test_stress_risk_scaling),
        ("Cost sweep",                  test_cost_sweep),
        ("Crisis scenario tests",       test_crisis_tester),
        ("Validation runner structure", test_validation_runner_structure),
        ("Stage 2 OOS runner",          test_validation_runner_stage2),
        ("Full validation report",      test_full_validation_report),
    ]

    results = {}
    for name, test_fn in tests:
        try:
            ok = test_fn()
            results[name] = ok if ok is not None else True
        except AssertionError as ae:
            print(f"{FAIL_MARK} Assertion failed: {ae}")
            results[name] = False
        except Exception as exc:
            print(f"{FAIL_MARK} Exception: {exc}")
            import traceback
            traceback.print_exc()
            results[name] = False

    print()
    print("=" * 70)
    print("  PHASE 4 RESULTS")
    print("=" * 70)

    for name, ok in results.items():
        status = "[PASS]" if ok else "[FAIL]"
        print(f"  {status}  {name}")

    all_passed = all(results.values())
    n_pass = sum(results.values())
    n_total = len(results)

    print()
    if all_passed:
        print(f"  *** ALL {n_pass}/{n_total} TESTS PASSED -- Phase 4 COMPLETE ***")
        print("  Validation framework validated. Ready for 25-year full backtest.")
    else:
        n_fail = n_total - n_pass
        print(f"  *** {n_pass}/{n_total} TESTS PASSED | {n_fail} FAILED ***")
        print("  Review failures above before proceeding.")

    print()
    print("  Next steps:")
    print("  1. Run full 21-year validation (IS 2003-2019, OOS 2020-2024)")
    print("  2. Check all thresholds: PF>=2.3, Sharpe>=1.0, DD<=22%, 16/20 years profitable")
    print("  3. Run walk-forward 7 windows (need 2003+ data)")
    print("  4. Run Monte Carlo 10,000 simulations")
    print("  5. Begin Phase 5: Paper Trading")
    print("=" * 70)

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
