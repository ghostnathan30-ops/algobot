"""
AlgoBot — Walk-Forward Validation
====================================
Module:  src/backtest/walk_forward.py
Phase:   3 — Backtesting Engine
Purpose: Runs the 7-window walk-forward validation defined in config.yaml.
         Walk-forward testing is the gold standard for avoiding overfitting:
         we train on one period, test on the NEXT (unseen) period, and repeat.

Why walk-forward matters:
  In a simple in-sample/out-of-sample split, the strategy "knows" which period
  it will be tested on. Walk-forward uses rolling windows to simulate how a
  strategy would have performed if deployed in real time — training on all
  past data and trading into the immediate future, period after period.

Pass criteria (from config.yaml validation.walk_forward):
  - At least 5 of 7 windows must be profitable (positive total return)
  - No single window's drawdown may exceed 30%

The 7 windows (from config.yaml):
  Window 1:  Train through 2004, Test 2005-2006
  Window 2:  Train through 2006, Test 2007-2008
  Window 3:  Train through 2008, Test 2009-2010
  Window 4:  Train through 2010, Test 2011-2013
  Window 5:  Train through 2013, Test 2014-2017
  Window 6:  Train through 2017, Test 2018-2020
  Window 7:  Train through 2020, Test 2021-2024

Usage:
    from src.backtest.walk_forward import run_walk_forward

    wf_results = run_walk_forward(market_data, config)
    for window in wf_results["windows"]:
        print(f"Window {window['window_id']}: PF={window['metrics']['profit_factor']:.2f}")
    print(f"PASS: {wf_results['passed']}")
"""

from __future__ import annotations

from src.utils.logger import get_logger
from src.backtest.engine import BacktestEngine
from src.backtest.metrics import check_validation_thresholds

log = get_logger(__name__)


# ── Walk-forward runner ───────────────────────────────────────────────────────

def run_walk_forward(
    market_data: dict,
    config: dict,
    initial_capital: float = 150_000.0,
) -> dict:
    """
    Run 7-window walk-forward validation using windows from config.yaml.

    Each window runs the backtest engine only on the TEST period.
    This simulates deploying the strategy with fixed parameters and
    evaluating performance on truly unseen data.

    Args:
        market_data:     dict of market_code -> processed DataFrame
                         (must cover the full date range of all windows)
        config:          Full config dict (loaded from config.yaml)
        initial_capital: Starting equity for each window (reset each window)

    Returns:
        dict with keys:
          "windows"   — list of per-window result dicts
          "passed"    — bool: True if ≥5/7 windows profitable, no DD >30%
          "summary"   — aggregate stats across all windows
          "fail_reasons" — list of strings if failed
    """
    windows_cfg = config.get("backtest", {}).get("walk_forward_windows", [])
    wf_thresholds = config.get("validation", {}).get("walk_forward", {})

    min_profitable = int(wf_thresholds.get("min_profitable_windows", 5))
    max_dd_pct     = float(wf_thresholds.get("max_single_window_dd",   30.0))

    if not windows_cfg:
        log.error("No walk_forward_windows found in config.yaml backtest section")
        return {"windows": [], "passed": False, "summary": {}, "fail_reasons": ["No windows configured"]}

    log.info("Walk-forward validation: {n} windows", n=len(windows_cfg))

    window_results = []

    for i, win in enumerate(windows_cfg, start=1):
        test_start = str(win["test_start"])
        test_end   = str(win["test_end"])
        train_end  = str(win["train_end"])

        log.info(
            "Window {i}/{total}: train through {tr_end}, test {ts} to {te}",
            i=i, total=len(windows_cfg),
            tr_end=train_end, ts=test_start, te=test_end,
        )

        try:
            engine = BacktestEngine(config, initial_capital=initial_capital)
            result = engine.run(market_data, test_start, test_end)
        except Exception as exc:
            log.error("Window {i} failed: {err}", i=i, err=exc)
            window_results.append({
                "window_id":   i,
                "train_end":   train_end,
                "test_start":  test_start,
                "test_end":    test_end,
                "metrics":     {},
                "passed":      False,
                "fail_reason": str(exc),
            })
            continue

        metrics = result.metrics
        profitable = metrics.get("total_return_pct", 0) > 0
        dd_ok = abs(metrics.get("max_drawdown_pct", 0)) <= max_dd_pct

        window_passed = profitable and dd_ok
        fail_reason = ""
        if not profitable:
            fail_reason += f"Unprofitable ({metrics.get('total_return_pct', 0):.1f}%). "
        if not dd_ok:
            fail_reason += f"DD exceeded limit ({abs(metrics.get('max_drawdown_pct', 0)):.1f}% > {max_dd_pct}%). "

        window_results.append({
            "window_id":   i,
            "train_end":   train_end,
            "test_start":  test_start,
            "test_end":    test_end,
            "metrics":     metrics,
            "trades":      result.total_trades,
            "passed":      window_passed,
            "fail_reason": fail_reason.strip(),
        })

        log.info(
            "  Window {i}: {status} | PF={pf:.2f} | Ret={ret:.1f}% | DD={dd:.1f}%",
            i=i,
            status="PASS" if window_passed else "FAIL",
            pf=metrics.get("profit_factor", 0),
            ret=metrics.get("total_return_pct", 0),
            dd=metrics.get("max_drawdown_pct", 0),
        )

    # ── Aggregate results ──────────────────────────────────────────────────────
    n_passed    = sum(1 for w in window_results if w["passed"])
    n_total     = len(window_results)
    overall_passed = n_passed >= min_profitable

    fail_reasons = []
    if n_passed < min_profitable:
        fail_reasons.append(
            f"Only {n_passed}/{n_total} windows profitable (need {min_profitable})"
        )
    for w in window_results:
        if w.get("fail_reason"):
            fail_reasons.append(f"Window {w['window_id']}: {w['fail_reason']}")

    # Summary stats across all windows
    all_pf  = [w["metrics"].get("profit_factor", 0) for w in window_results if w["metrics"]]
    all_ret = [w["metrics"].get("total_return_pct", 0) for w in window_results if w["metrics"]]
    all_dd  = [w["metrics"].get("max_drawdown_pct", 0) for w in window_results if w["metrics"]]

    summary = {
        "windows_passed":         n_passed,
        "windows_total":          n_total,
        "min_profitable_required": min_profitable,
        "avg_profit_factor":      round(sum(all_pf) / len(all_pf), 3) if all_pf else 0.0,
        "avg_return_pct":         round(sum(all_ret) / len(all_ret), 2) if all_ret else 0.0,
        "worst_drawdown_pct":     round(min(all_dd), 2) if all_dd else 0.0,
        "best_profit_factor":     round(max(all_pf), 3) if all_pf else 0.0,
        "worst_profit_factor":    round(min(all_pf), 3) if all_pf else 0.0,
    }

    log.info(
        "Walk-forward complete: {n}/{total} windows passed | "
        "Avg PF={pf:.2f} | Overall: {result}",
        n=n_passed, total=n_total,
        pf=summary["avg_profit_factor"],
        result="PASS" if overall_passed else "FAIL",
    )

    return {
        "windows":      window_results,
        "passed":       overall_passed,
        "summary":      summary,
        "fail_reasons": fail_reasons,
    }
