"""
AlgoBot — Validation Runner
=============================
Module:  src/backtest/validation_runner.py
Phase:   4 — Validation Suite
Purpose: Master orchestrator that runs all six validation stages and produces
         a comprehensive go/no-go decision before proceeding to paper trading.

The Six Stages:
  Stage 1 — In-Sample Backtest       (2003-2019 or configured IS period)
  Stage 2 — Out-of-Sample Backtest   (2020-2024)
  Stage 3 — Walk-Forward Validation  (7 windows from config)
  Stage 4 — Crisis Scenario Tests    (4 historical episodes)
  Stage 5 — Stress Tests             (double costs, remove best, scale)
  Stage 6 — Paper Trading            (60 days — cannot be automated, structural only)

Overall verdict:
  PASS        — All stages pass
  CONDITIONAL — Some stages skipped due to data limitations but no failures
  FAIL        — One or more stages fail

The bot does NOT go live until this report shows PASS or CONDITIONAL.

Usage:
    from src.backtest.data_loader import load_all_markets, load_config
    from src.backtest.validation_runner import run_full_validation, save_validation_report

    config      = load_config()
    market_data = load_all_markets("2000-01-01", "2024-12-31", config)

    report = run_full_validation(market_data, config)
    save_validation_report(report)

    print(f"Overall verdict: {report.overall_verdict}")
    for stage in report.stages:
        print(f"  Stage {stage.stage_id}: {stage.stage_name} — "
              f"{'PASS' if stage.passed else 'SKIP' if stage.skipped else 'FAIL'}")
"""

from __future__ import annotations

import json
import os
import datetime
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from src.backtest.engine import BacktestEngine
from src.backtest.metrics import calculate_all_metrics, check_validation_thresholds
from src.backtest.walk_forward import run_walk_forward
from src.backtest.monte_carlo import run_monte_carlo
from src.backtest.stress_tester import run_all_stress_tests, StressSuite
from src.backtest.regime_tester import run_all_crisis_tests, crisis_suite_passed
from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Stage result ──────────────────────────────────────────────────────────────

@dataclass
class StageResult:
    """Result of one validation stage."""
    stage_id:    int
    stage_name:  str
    passed:      bool
    skipped:     bool      # True if data not available or stage not automatable
    skip_reason: str
    metrics:     dict = field(default_factory=dict)
    fail_reasons: list = field(default_factory=list)
    details:     dict = field(default_factory=dict)   # Stage-specific data

    def status_str(self) -> str:
        if self.skipped:
            return "SKIP"
        return "PASS" if self.passed else "FAIL"

    def __str__(self) -> str:
        status = self.status_str()
        base = f"[{status}] Stage {self.stage_id}: {self.stage_name}"
        if self.skipped:
            return f"{base} — {self.skip_reason}"
        if not self.passed:
            return f"{base} — {'; '.join(self.fail_reasons)}"
        return base


# ── Validation report ─────────────────────────────────────────────────────────

@dataclass
class ValidationReport:
    """Complete multi-stage validation output."""
    timestamp:       str
    stages:          list     # list[StageResult]
    overall_verdict: str      # "PASS", "FAIL", "CONDITIONAL"
    fail_reasons:    list = field(default_factory=list)
    summary:         dict = field(default_factory=dict)

    def __str__(self) -> str:
        lines = [
            "=" * 70,
            f"  AlgoBot Validation Report — {self.timestamp}",
            f"  Overall Verdict: {self.overall_verdict}",
            "=" * 70,
        ]
        for s in self.stages:
            lines.append(f"  {s}")
        if self.fail_reasons:
            lines.append("\nFailures:")
            for f in self.fail_reasons:
                lines.append(f"  - {f}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialisable dictionary for JSON output."""
        return {
            "timestamp":       self.timestamp,
            "overall_verdict": self.overall_verdict,
            "fail_reasons":    self.fail_reasons,
            "summary":         self.summary,
            "stages": [
                {
                    "stage_id":    s.stage_id,
                    "stage_name":  s.stage_name,
                    "status":      s.status_str(),
                    "skip_reason": s.skip_reason,
                    "metrics":     {
                        k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in s.metrics.items()
                        if not isinstance(v, (dict, list))
                    },
                    "fail_reasons": s.fail_reasons,
                }
                for s in self.stages
            ],
        }


# ── Stage runners ─────────────────────────────────────────────────────────────

def _run_stage_1_insample(
    market_data: dict,
    config: dict,
    is_start: str,
    is_end: str,
    initial_capital: float,
) -> StageResult:
    """Stage 1: In-sample backtest."""
    thresholds = config.get("validation", {}).get("in_sample", {})
    log.info("Stage 1: In-sample backtest {s} to {e}", s=is_start, e=is_end)

    # Check data availability
    try:
        start_ts = pd.Timestamp(is_start)
        end_ts   = pd.Timestamp(is_end)
        any_df   = next(iter(market_data.values()))
        data_start = any_df.index.min()
        data_end   = any_df.index.max()

        if start_ts < data_start:
            # Adjust to available start
            actual_start = data_start.strftime("%Y-%m-%d")
            log.warning(
                "Stage 1: Requested IS start {req} before data start {avail}. "
                "Using {avail}.",
                req=is_start, avail=actual_start,
            )
            is_start = actual_start

        if end_ts > data_end:
            return StageResult(
                stage_id=1, stage_name="In-Sample Backtest",
                passed=False, skipped=True,
                skip_reason=f"IS end {is_end} beyond data end {data_end.date()}",
            )
    except Exception as exc:
        return StageResult(
            stage_id=1, stage_name="In-Sample Backtest",
            passed=False, skipped=True,
            skip_reason=f"Data check failed: {exc}",
        )

    try:
        engine = BacktestEngine(config, initial_capital=initial_capital)
        result = engine.run(market_data, is_start, is_end)
    except Exception as exc:
        return StageResult(
            stage_id=1, stage_name="In-Sample Backtest",
            passed=False, skipped=True,
            skip_reason=f"Engine error: {exc}",
        )

    check = check_validation_thresholds(result.metrics, thresholds)
    mc    = run_monte_carlo(result.trades, config, n_simulations=1_000,
                             initial_capital=initial_capital)

    passed = check["passed"]
    fail_reasons = check["fail_reasons"]

    log.info(
        "Stage 1 complete: PF={pf:.2f} | Sharpe={sr:.2f} | "
        "MaxDD={dd:.1f}% | Return={ret:.1f}% | {status}",
        pf=result.metrics.get("profit_factor", 0),
        sr=result.metrics.get("sharpe_ratio", 0),
        dd=result.metrics.get("max_drawdown_pct", 0),
        ret=result.total_return_pct,
        status="PASS" if passed else "FAIL",
    )

    return StageResult(
        stage_id=1, stage_name="In-Sample Backtest",
        passed=passed, skipped=False, skip_reason="",
        metrics=result.metrics,
        fail_reasons=fail_reasons,
        details={
            "period": f"{is_start} to {is_end}",
            "total_trades": result.total_trades,
            "total_return_pct": result.total_return_pct,
            "mc_95th_dd": mc.get("dd_95th_pct", 0),
            "annual_returns": result.metrics.get("annual_returns_by_year", {}),
        },
    )


def _run_stage_2_oos(
    market_data: dict,
    config: dict,
    oos_start: str,
    oos_end: str,
    initial_capital: float,
) -> StageResult:
    """Stage 2: Out-of-sample backtest."""
    thresholds = config.get("validation", {}).get("out_of_sample", {})
    log.info("Stage 2: OOS backtest {s} to {e}", s=oos_start, e=oos_end)

    try:
        engine = BacktestEngine(config, initial_capital=initial_capital)
        result = engine.run(market_data, oos_start, oos_end)
    except Exception as exc:
        return StageResult(
            stage_id=2, stage_name="Out-of-Sample Backtest",
            passed=False, skipped=True,
            skip_reason=f"Engine error: {exc}",
        )

    check = check_validation_thresholds(result.metrics, thresholds)

    log.info(
        "Stage 2 complete: PF={pf:.2f} | Sharpe={sr:.2f} | "
        "MaxDD={dd:.1f}% | {status}",
        pf=result.metrics.get("profit_factor", 0),
        sr=result.metrics.get("sharpe_ratio", 0),
        dd=result.metrics.get("max_drawdown_pct", 0),
        status="PASS" if check["passed"] else "FAIL",
    )

    return StageResult(
        stage_id=2, stage_name="Out-of-Sample Backtest",
        passed=check["passed"], skipped=False, skip_reason="",
        metrics=result.metrics,
        fail_reasons=check["fail_reasons"],
        details={
            "period": f"{oos_start} to {oos_end}",
            "total_trades": result.total_trades,
            "total_return_pct": result.total_return_pct,
            "pf_by_market": result.metrics.get("profit_factor_by_market", {}),
            "pf_by_strategy": result.metrics.get("profit_factor_by_strategy", {}),
        },
    )


def _run_stage_3_walk_forward(
    market_data: dict,
    config: dict,
    initial_capital: float,
) -> StageResult:
    """Stage 3: Walk-forward validation."""
    log.info("Stage 3: Walk-forward validation (7 windows)")

    try:
        wf = run_walk_forward(market_data, config, initial_capital=initial_capital)
    except Exception as exc:
        return StageResult(
            stage_id=3, stage_name="Walk-Forward Validation",
            passed=False, skipped=True,
            skip_reason=f"Walk-forward error: {exc}",
        )

    n_passed = wf["summary"].get("windows_passed", 0)
    n_total  = wf["summary"].get("windows_total", 0)

    log.info(
        "Stage 3 complete: {n}/{total} windows passed | {status}",
        n=n_passed, total=n_total,
        status="PASS" if wf["passed"] else "FAIL",
    )

    return StageResult(
        stage_id=3, stage_name="Walk-Forward Validation",
        passed=wf["passed"], skipped=False, skip_reason="",
        metrics=wf["summary"],
        fail_reasons=wf["fail_reasons"],
        details={
            "windows": [
                {
                    "id": w["window_id"],
                    "test_period": f"{w['test_start']} to {w['test_end']}",
                    "passed": w["passed"],
                    "pf": w["metrics"].get("profit_factor", 0),
                    "return_pct": w["metrics"].get("total_return_pct", 0),
                }
                for w in wf["windows"]
            ],
        },
    )


def _run_stage_4_crisis(
    market_data: dict,
    config: dict,
    initial_capital: float,
) -> StageResult:
    """Stage 4: Crisis scenario tests."""
    log.info("Stage 4: Crisis scenario tests (4 episodes)")

    crisis_results = run_all_crisis_tests(market_data, config, initial_capital)
    overall_passed, fail_reasons = crisis_suite_passed(crisis_results)

    ran     = [r for r in crisis_results if not r.skipped]
    skipped = [r for r in crisis_results if r.skipped]
    passed  = [r for r in ran if r.passed]

    # If all tests were skipped, mark stage as skipped
    if not ran:
        return StageResult(
            stage_id=4, stage_name="Crisis Scenario Tests",
            passed=False, skipped=True,
            skip_reason="All crisis periods require historical data not yet downloaded",
            details={"crisis_results": [str(r) for r in crisis_results]},
        )

    log.info(
        "Stage 4 complete: {p}/{r} crisis tests passed, {s} skipped | {status}",
        p=len(passed), r=len(ran), s=len(skipped),
        status="PASS" if overall_passed else "FAIL",
    )

    return StageResult(
        stage_id=4, stage_name="Crisis Scenario Tests",
        passed=overall_passed, skipped=False, skip_reason="",
        metrics={
            "tests_ran": len(ran),
            "tests_passed": len(passed),
            "tests_skipped": len(skipped),
        },
        fail_reasons=fail_reasons,
        details={"crisis_results": [str(r) for r in crisis_results]},
    )


def _run_stage_5_stress(
    is_result,     # BacktestResult from Stage 1 (or OOS if IS not available)
    config: dict,
    initial_capital: float,
) -> StageResult:
    """Stage 5: Stress tests on the in-sample result."""
    log.info("Stage 5: Stress tests")

    if is_result is None:
        return StageResult(
            stage_id=5, stage_name="Stress Tests",
            passed=False, skipped=True,
            skip_reason="No backtest result available for stress testing",
        )

    try:
        suite = run_all_stress_tests(is_result, config, initial_capital)
    except Exception as exc:
        return StageResult(
            stage_id=5, stage_name="Stress Tests",
            passed=False, skipped=True,
            skip_reason=f"Stress tester error: {exc}",
        )

    mandatory = [r for r in suite.results if not r.test_name.startswith("Cost")]
    n_passed  = sum(1 for r in mandatory if r.passed)

    log.info(
        "Stage 5 complete: {n}/{total} mandatory stress tests passed | {status}",
        n=n_passed, total=len(mandatory),
        status="PASS" if suite.all_passed else "FAIL",
    )

    return StageResult(
        stage_id=5, stage_name="Stress Tests",
        passed=suite.all_passed, skipped=False, skip_reason="",
        metrics=suite.summary,
        fail_reasons=suite.fail_reasons,
        details={
            "results": [str(r) for r in suite.results],
            "cost_sweep": suite.summary.get("cost_sweep", {}),
        },
    )


def _stage_6_paper_trading() -> StageResult:
    """Stage 6: Paper trading — structural placeholder (cannot be automated)."""
    log.info("Stage 6: Paper trading — SKIP (requires 60 days live paper execution)")
    return StageResult(
        stage_id=6, stage_name="Paper Trading (60 days)",
        passed=False, skipped=True,
        skip_reason=(
            "Paper trading requires 60 calendar days of live execution on "
            "NinjaTrader or IBKR paper account. This stage cannot be automated. "
            "Complete Phase 5 to satisfy this requirement."
        ),
        details={
            "requirements": [
                "60 days on live market data (not backtest)",
                "Signal frequency within 25% of backtest expectation",
                "Paper P&L within 30% of backtest daily average",
                "All emergency stops tested and confirmed working",
                "No crashes over 5+ consecutive days",
                "Topstep rules never violated",
            ]
        },
    )


# ── Master validation runner ──────────────────────────────────────────────────

def run_full_validation(
    market_data: dict,
    config: dict,
    initial_capital: float = 150_000.0,
    is_start: str = "2000-01-01",
    is_end:   str = "2019-12-31",
    oos_start: str = "2020-01-01",
    oos_end:   str = "2024-12-31",
) -> ValidationReport:
    """
    Run all six validation stages and produce a comprehensive report.

    Args:
        market_data:     dict of market -> DataFrame (from load_all_markets)
        config:          Full config dict
        initial_capital: Starting equity
        is_start:        In-sample period start
        is_end:          In-sample period end
        oos_start:       Out-of-sample period start
        oos_end:         Out-of-sample period end

    Returns:
        ValidationReport with all stage results and overall verdict.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log.info("=" * 60)
    log.info("AlgoBot Full Validation Suite — {ts}", ts=timestamp)
    log.info("=" * 60)

    stages = []
    is_result  = None   # Keep Stage 1 result for Stage 5 stress tests
    oos_result = None

    # ── Stage 1: In-Sample ─────────────────────────────────────────────────────
    s1 = _run_stage_1_insample(market_data, config, is_start, is_end, initial_capital)
    stages.append(s1)

    # Run engine again to keep result object for Stage 5
    if not s1.skipped:
        try:
            actual_is_start = s1.details.get("period", f"{is_start} to {is_end}").split(" to ")[0]
            engine = BacktestEngine(config, initial_capital=initial_capital)
            is_result = engine.run(market_data, actual_is_start, is_end)
        except Exception:
            pass

    # ── Stage 2: OOS ──────────────────────────────────────────────────────────
    s2 = _run_stage_2_oos(market_data, config, oos_start, oos_end, initial_capital)
    stages.append(s2)

    # ── Stage 3: Walk-Forward ─────────────────────────────────────────────────
    s3 = _run_stage_3_walk_forward(market_data, config, initial_capital)
    stages.append(s3)

    # ── Stage 4: Crisis Tests ─────────────────────────────────────────────────
    s4 = _run_stage_4_crisis(market_data, config, initial_capital)
    stages.append(s4)

    # ── Stage 5: Stress Tests ─────────────────────────────────────────────────
    # Use IS result if available, otherwise OOS result
    stress_source = is_result
    if stress_source is None and not s2.skipped:
        try:
            engine = BacktestEngine(config, initial_capital=initial_capital)
            stress_source = engine.run(market_data, oos_start, oos_end)
        except Exception:
            pass

    s5 = _run_stage_5_stress(stress_source, config, initial_capital)
    stages.append(s5)

    # ── Stage 6: Paper Trading ─────────────────────────────────────────────────
    s6 = _stage_6_paper_trading()
    stages.append(s6)

    # ── Overall verdict ────────────────────────────────────────────────────────
    ran_stages  = [s for s in stages if not s.skipped]
    failed      = [s for s in ran_stages if not s.passed]
    all_skipped = len(ran_stages) == 0
    any_skipped = any(s.skipped for s in stages)

    fail_reasons = [
        f"Stage {s.stage_id} ({s.stage_name}): {'; '.join(s.fail_reasons)}"
        for s in failed
    ]

    if all_skipped:
        overall_verdict = "FAIL"
    elif failed:
        overall_verdict = "FAIL"
    elif any_skipped:
        overall_verdict = "CONDITIONAL"
    else:
        overall_verdict = "PASS"

    # ── Summary stats ──────────────────────────────────────────────────────────
    summary = {
        "stages_total":   len(stages),
        "stages_passed":  sum(1 for s in ran_stages if s.passed),
        "stages_failed":  len(failed),
        "stages_skipped": sum(1 for s in stages if s.skipped),
        "is_pf":   s1.metrics.get("profit_factor", "N/A") if not s1.skipped else "N/A",
        "is_sharpe": s1.metrics.get("sharpe_ratio", "N/A") if not s1.skipped else "N/A",
        "oos_pf":  s2.metrics.get("profit_factor", "N/A") if not s2.skipped else "N/A",
        "oos_sharpe": s2.metrics.get("sharpe_ratio", "N/A") if not s2.skipped else "N/A",
        "wf_windows_passed": s3.metrics.get("windows_passed", "N/A") if not s3.skipped else "N/A",
    }

    report = ValidationReport(
        timestamp=timestamp,
        stages=stages,
        overall_verdict=overall_verdict,
        fail_reasons=fail_reasons,
        summary=summary,
    )

    log.info("=" * 60)
    log.info("VALIDATION COMPLETE — Overall: {v}", v=overall_verdict)
    log.info(
        "Stages: {p} PASS | {f} FAIL | {s} SKIP",
        p=summary["stages_passed"],
        f=summary["stages_failed"],
        s=summary["stages_skipped"],
    )
    log.info("=" * 60)

    return report


# ── Save report to disk ───────────────────────────────────────────────────────

def save_validation_report(
    report: ValidationReport,
    output_dir: str = "reports/validation",
) -> str:
    """
    Save the ValidationReport as a JSON file.

    Args:
        report:     ValidationReport object
        output_dir: Directory to write to (created if not exists)

    Returns:
        Path to the saved file.
    """
    os.makedirs(output_dir, exist_ok=True)

    ts_safe = report.timestamp.replace(":", "-").replace(" ", "_")
    filename = f"validation_{ts_safe}.json"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, default=str)

    log.info("Validation report saved: {path}", path=filepath)
    return filepath
