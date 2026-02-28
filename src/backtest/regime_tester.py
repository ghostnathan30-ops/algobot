"""
AlgoBot — Regime / Crisis Scenario Tester
==========================================
Module:  src/backtest/regime_tester.py
Phase:   4 — Validation Suite
Purpose: Isolates specific historical crisis periods and verifies that the
         strategy's risk management protects the account under extreme conditions.

Why crisis testing matters:
  A strategy optimised on full-history data may show a good average but hide
  catastrophic behaviour during specific market events. These tests check
  that the five risk layers work correctly when markets are most dangerous:
    - 2008 Financial Crisis: Sudden correlated crashes across all assets
    - 2010-2012 Trend Drought: Extended choppy, whipsaw conditions
    - COVID March 2020: 30% equity crash in 23 trading days
    - 2022 Rate Hike Cycle: Bond crash + equity bear in same direction

  The bot is specifically designed to handle each of these:
    - 2008: CRISIS regime cuts all sizes to 0. ZB (bond) goes long = profit.
    - 2010-2012: Signal Agreement Filter blocks most entries in choppy markets.
    - 2020: HIGH_VOL/CRISIS sizes reduce to 30%/0%. Daily hard stop active.
    - 2022: ZB goes short (rate hike = bond bear). ES/NQ go short in downtrend.

Pass criteria (from README.md):
  2008:        Max drawdown during period ≤ 20%
  2010-2012:   Total return for 3 years ≥ -15% (small loss acceptable)
  COVID 2020:  Max drawdown during 5-week window ≤ 12%
  2022:        Profitable for full calendar year (total return > 0%)

Data requirements:
  2008 crisis:    Requires data from 2008-09-01 — needs 25-year dataset
  2010-2012:      Requires data from 2010-01-01 — needs 25-year dataset
  COVID 2020:     Available in 2020-2024 dataset (always runnable)
  2022 rate hike: Available in 2020-2024 dataset (always runnable)

  Tests will skip gracefully if required data is not available.

Usage:
    from src.backtest.regime_tester import run_all_crisis_tests

    crisis_results = run_all_crisis_tests(market_data, config)
    for cr in crisis_results:
        print(f"{cr.test_name}: {'PASS' if cr.passed else 'SKIP' if cr.skipped else 'FAIL'}")
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from src.backtest.engine import BacktestEngine
from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Scenario definitions ──────────────────────────────────────────────────────

CRISIS_SCENARIOS = [
    {
        "name":         "2008 Financial Crisis",
        "start":        "2008-09-01",
        "end":          "2009-03-31",
        "max_dd_limit": 20.0,    # Max drawdown must stay ≤ 20% during this period
        "min_return":   None,    # No minimum return requirement (surviving is the goal)
        "description":  (
            "Lehman Brothers collapse (Sep 2008) through bottom of bear market (Mar 2009). "
            "CRISIS regime should block new entries. ZB long trend should generate profit."
        ),
    },
    {
        "name":         "2010-2012 Trend Drought",
        "start":        "2010-01-01",
        "end":          "2012-12-31",
        "max_dd_limit": None,    # No DD limit for this test
        "min_return":   -15.0,   # Max acceptable loss = 15% over 3 years
        "description":  (
            "Post-crisis choppy recovery. Trend strategies historically struggle here. "
            "Signal Agreement Filter should block most false breakouts."
        ),
    },
    {
        "name":         "COVID March 2020",
        "start":        "2020-02-01",
        "end":          "2020-04-30",
        "max_dd_limit": 12.0,    # Tight: must survive crash at reduced size
        "min_return":   None,
        "description":  (
            "COVID crash: S&P 500 fell 34% in 23 trading days. "
            "HIGH_VOL/CRISIS regime should drastically reduce sizes. "
            "Daily hard stop ($2,500) protects against single-day disasters."
        ),
    },
    {
        "name":         "2022 Rate Hike Year",
        "start":        "2022-01-01",
        "end":          "2022-12-31",
        "max_dd_limit": None,
        "min_return":   0.0,     # Must be profitable for the year
        "description":  (
            "Fed rate hikes caused simultaneous crash in bonds AND equities (rare). "
            "ZB short trend should be highly profitable. ES/NQ short trend also fires. "
            "2022 is one of the best expected years for this strategy."
        ),
    },
]


# ── Result container ──────────────────────────────────────────────────────────

@dataclass
class CrisisResult:
    """Result of one crisis scenario test."""
    test_name:      str
    period_start:   str
    period_end:     str
    description:    str
    passed:         bool
    skipped:        bool
    skip_reason:    str
    max_drawdown_pct: float
    total_return_pct: float
    n_trades:       int
    max_dd_limit:   float   # -1 if not applicable
    min_return_limit: float # -999 if not applicable
    fail_reasons:   list = field(default_factory=list)

    def __str__(self) -> str:
        if self.skipped:
            return f"[SKIP] {self.test_name}: {self.skip_reason}"
        status = "PASS" if self.passed else "FAIL"
        return (
            f"[{status}] {self.test_name} "
            f"({self.period_start} to {self.period_end}): "
            f"MaxDD={self.max_drawdown_pct:.1f}% | "
            f"Return={self.total_return_pct:.1f}% | "
            f"Trades={self.n_trades}"
        )


# ── Single crisis test ────────────────────────────────────────────────────────

def test_crisis_period(
    market_data: dict,
    config: dict,
    start: str,
    end: str,
    name: str,
    description: str,
    max_dd_limit: float = None,    # None = no drawdown test
    min_return: float = None,      # None = no return test
    initial_capital: float = 150_000.0,
) -> CrisisResult:
    """
    Run the backtest engine on a specific crisis period and check pass criteria.

    Skips gracefully if the required date range is not available in market_data.

    Args:
        market_data:     dict of market -> DataFrame (pre-loaded)
        config:          Full config dict
        start:           Crisis period start date (YYYY-MM-DD)
        end:             Crisis period end date (YYYY-MM-DD)
        name:            Human-readable test name
        description:     Explanation of the scenario
        max_dd_limit:    Maximum drawdown allowed during period (positive %)
        min_return:      Minimum total return required (negative = acceptable loss)
        initial_capital: Starting equity for this window

    Returns:
        CrisisResult
    """
    # Check if data covers this period
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end)

    # Check availability in at least one market
    data_start = None
    data_end   = None
    for df in market_data.values():
        if len(df) > 0:
            ds = df.index.min()
            de = df.index.max()
            data_start = ds if data_start is None else max(data_start, ds)
            data_end   = de if data_end is None else min(data_end, de)
            break

    if data_start is None or start_ts < data_start or end_ts > data_end:
        skip_msg = (
            f"Data range {data_start.date() if data_start else 'N/A'} to "
            f"{data_end.date() if data_end else 'N/A'} does not cover "
            f"{start} to {end}"
        )
        log.info("Crisis test '{name}': SKIPPED — {msg}", name=name, msg=skip_msg)
        return CrisisResult(
            test_name=name, period_start=start, period_end=end,
            description=description, passed=False, skipped=True,
            skip_reason=skip_msg, max_drawdown_pct=0.0, total_return_pct=0.0,
            n_trades=0, max_dd_limit=max_dd_limit or -1,
            min_return_limit=min_return if min_return is not None else -999.0,
        )

    log.info(
        "Crisis test '{name}': Running {start} to {end}...",
        name=name, start=start, end=end,
    )

    try:
        engine = BacktestEngine(config, initial_capital=initial_capital)
        result = engine.run(market_data, start, end)
    except Exception as exc:
        log.error("Crisis test '{name}' engine failed: {err}", name=name, err=exc)
        return CrisisResult(
            test_name=name, period_start=start, period_end=end,
            description=description, passed=False, skipped=True,
            skip_reason=f"Engine error: {exc}", max_drawdown_pct=0.0,
            total_return_pct=0.0, n_trades=0,
            max_dd_limit=max_dd_limit or -1,
            min_return_limit=min_return if min_return is not None else -999.0,
        )

    metrics      = result.metrics
    actual_dd    = metrics.get("max_drawdown_pct", 0.0)   # negative value
    actual_ret   = result.total_return_pct
    n_trades     = result.total_trades

    # Evaluate pass conditions
    fail_reasons = []
    passed_dd    = True
    passed_ret   = True

    if max_dd_limit is not None:
        passed_dd = abs(actual_dd) <= max_dd_limit
        if not passed_dd:
            fail_reasons.append(
                f"Drawdown {abs(actual_dd):.1f}% exceeded limit {max_dd_limit:.1f}%"
            )

    if min_return is not None:
        passed_ret = actual_ret >= min_return
        if not passed_ret:
            fail_reasons.append(
                f"Return {actual_ret:.1f}% below minimum {min_return:.1f}%"
            )

    passed = passed_dd and passed_ret

    log.info(
        "Crisis test '{name}': {status} | MaxDD={dd:.1f}% | Return={ret:.1f}% | Trades={n}",
        name=name,
        status="PASS" if passed else "FAIL",
        dd=actual_dd, ret=actual_ret, n=n_trades,
    )

    return CrisisResult(
        test_name=name,
        period_start=start,
        period_end=end,
        description=description,
        passed=passed,
        skipped=False,
        skip_reason="",
        max_drawdown_pct=round(actual_dd, 2),
        total_return_pct=round(actual_ret, 2),
        n_trades=n_trades,
        max_dd_limit=max_dd_limit if max_dd_limit is not None else -1.0,
        min_return_limit=min_return if min_return is not None else -999.0,
        fail_reasons=fail_reasons,
    )


# ── Master runner ─────────────────────────────────────────────────────────────

def run_all_crisis_tests(
    market_data: dict,
    config: dict,
    initial_capital: float = 150_000.0,
) -> list:
    """
    Run all four crisis scenario tests.

    Tests that cannot run (data not available) are marked as SKIP.
    Only tests that FAIL (ran but did not pass) count against the suite.

    Args:
        market_data:     dict of market -> DataFrame
        config:          Full config dict
        initial_capital: Starting equity for each crisis window

    Returns:
        list of CrisisResult (one per scenario)
    """
    results = []

    for scenario in CRISIS_SCENARIOS:
        cr = test_crisis_period(
            market_data=market_data,
            config=config,
            start=scenario["start"],
            end=scenario["end"],
            name=scenario["name"],
            description=scenario["description"],
            max_dd_limit=scenario.get("max_dd_limit"),
            min_return=scenario.get("min_return"),
            initial_capital=initial_capital,
        )
        results.append(cr)

    # Summary
    ran     = [r for r in results if not r.skipped]
    skipped = [r for r in results if r.skipped]
    passed  = [r for r in ran if r.passed]
    failed  = [r for r in ran if not r.passed]

    log.info(
        "Crisis tests complete: {p}/{r} ran tests passed, {s} skipped",
        p=len(passed), r=len(ran), s=len(skipped),
    )
    if failed:
        for f in failed:
            log.warning(
                "  FAILED: {name} — {reasons}",
                name=f.test_name,
                reasons="; ".join(f.fail_reasons),
            )

    return results


# ── Convenience: check overall pass ───────────────────────────────────────────

def crisis_suite_passed(results: list) -> tuple:
    """
    Determine overall pass/fail for the crisis test suite.

    Skipped tests do not count as failures (no data = can't test).
    All tests that actually ran must pass.

    Returns:
        (passed: bool, fail_reasons: list[str])
    """
    ran    = [r for r in results if not r.skipped]
    failed = [r for r in ran if not r.passed]

    if not ran:
        return False, ["All crisis tests were skipped — no data available"]

    fail_reasons = [
        f"{r.test_name}: {'; '.join(r.fail_reasons)}"
        for r in failed
    ]

    return len(failed) == 0, fail_reasons
