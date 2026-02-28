# LAB_005 — Phase 4: Validation Framework
**AlgoBot Project — Lab Report**
**Date:** 2026-02-27
**Phase:** 4 — Validation Suite
**Status:** COMPLETE (9/9 tests PASS)

---

## 1. Overview

Phase 4 built a complete six-stage validation framework for AlgoBot, following the specification in `README.md`. The framework stress-tests the strategy against adverse conditions, crisis scenarios, and walk-forward windows — ensuring any strategy that passes is robust enough for live trading.

The validation framework also served as the first honest, end-to-end performance audit of the strategy across the full data history (2003–2024).

---

## 2. Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `src/backtest/stress_tester.py` | ~310 | Three robustness tests: double costs, remove best trades, risk scaling |
| `src/backtest/regime_tester.py` | ~230 | Four crisis scenario tests: 2008, 2010-2012, COVID 2020, 2022 |
| `src/backtest/validation_runner.py` | ~330 | Six-stage master orchestrator + JSON report |
| `test_phase4.py` | ~608 | Nine-test validation suite |
| `reports/validation/` | dir | Timestamped JSON validation reports |
| `docs/LAB_005_Validation_Report.md` | this | Lab report |

---

## 3. Validation Framework Architecture

### Six Stages

| Stage | Name | Criteria | Data Needed |
|-------|------|----------|-------------|
| 1 | In-Sample Backtest | PF >= 2.0, Sharpe >= 0.8, 12/16 years profitable | 2004-2019 |
| 2 | Out-of-Sample Backtest | PF >= 2.0, Sharpe >= 0.8, 3/5 years profitable | 2020-2024 |
| 3 | Walk-Forward (7 windows) | 5/7 windows profitable | Full dataset |
| 4 | Crisis Scenarios | See below | Varies |
| 5 | Stress Tests | PF >= 1.5 under all conditions | Any |
| 6 | Paper Trading (60 days) | Manual execution check | Live |

### Stress Tests (Stage 5)

| Test | What It Checks | Pass Threshold |
|------|---------------|----------------|
| Double Costs | Commission x2 ($20), slippage x2 (0.1%/side) | PF >= 1.5 |
| Remove Best 20 Trades | Edge not driven by lucky outliers | PF >= 1.5 |
| Risk Scale 80% | Viable at reduced position sizes | PF >= 1.5 |
| Cost Sweep (info) | Break-even cost multiplier | N/A |

### Crisis Scenarios (Stage 4)

| Period | Scenario | Pass Criteria |
|--------|----------|--------------|
| 2008-09 to 2009-03 | Lehman collapse + bear market | Max DD <= 20% |
| 2010-01 to 2012-12 | Post-crisis trend drought | Total return >= -15% |
| 2020-02 to 2020-04 | COVID crash (-34% in 23 days) | Max DD <= 12% |
| 2022-01 to 2022-12 | Fed rate hike (bonds + equities crash) | Total return > 0% |

---

## 4. Phase 4 Test Results

All 9 tests passed on 2026-02-27:

| Test | Name | Result |
|------|------|--------|
| 1 | Stress tester imports & dataclasses | PASS |
| 2 | Double costs on real 2020-2024 data | PASS |
| 3 | Remove best trades on real data | PASS |
| 4 | Risk scaling on real data | PASS |
| 5 | Cost sweep monotonic | PASS |
| 6 | Crisis scenario tests (COVID + 2022) | PASS |
| 7 | Validation runner structure | PASS |
| 8 | Stage 2 OOS runner | PASS |
| 9 | Full validation report (6 stages) | PASS |

**Bugs fixed during Phase 4:**
1. Unicode em-dash (`—`) in print() statements caused Windows cp1252 `UnicodeEncodeError` — replaced all em-dashes with ASCII hyphens in runtime strings
2. `numpy.bool_` returned from `float >= float` comparisons fails `isinstance(x, bool)` — fixed by wrapping `_metrics_from_pnl_list` return values in explicit `float()` calls

---

## 5. Full Strategy Validation Results (2003-2024)

### In-Sample (2004-2019): 16 years

| Metric | Value | Threshold | Status |
|--------|-------|-----------|--------|
| Total Trades | 480 | -- | -- |
| Profit Factor | 0.96 | >= 2.0 | FAIL |
| Sharpe Ratio | -0.64 | >= 0.8 | FAIL |
| Ann. Return | -0.6%/yr | >= 0% | FAIL |
| Max Drawdown | -32.4% | <= 22% | FAIL |
| Win Rate | 47.3% | -- | -- |
| E[trade] | -$28 | -- | -- |
| Profitable Years | 7/16 | 12/16 | FAIL |

**Annual P&L (IS period):**

| Year | Return |
|------|--------|
| 2004 | +8.9% ✓ |
| 2005 | -2.3% |
| 2006 | -4.1% |
| 2007 | +1.8% ✓ |
| 2008 | -5.2% |
| 2009 | -3.8% |
| 2010 | -1.9% |
| 2011 | -0.8% |
| 2012 | -2.1% |
| 2013 | -0.7% |
| 2014 | +1.1% ✓ |
| 2015 | +2.2% ✓ |
| 2016 | +2.7% ✓ |
| 2017 | +3.4% ✓ |
| 2018 | +6.8% ✓ |
| 2019 | -3.1% |

### Out-of-Sample (2020-2024): 5 years

| Metric | Value | Threshold | Status |
|--------|-------|-----------|--------|
| Total Trades | 175 | -- | -- |
| Profit Factor | 1.03 | >= 2.0 | FAIL |
| Sharpe Ratio | -0.52 | >= 0.8 | FAIL |
| Ann. Return | +0.5%/yr | >= 0% | borderline |
| Max Drawdown | -14.8% | <= 22% | PASS |
| Win Rate | ~50% | -- | -- |
| E[trade] | -$34 | -- | -- |
| Profitable Years | 2/5 | 3/5 | FAIL |

### Performance Breakdown by Market and Strategy

**PF by Market (IS period):**

| Market | PF | Comment |
|--------|-----|---------|
| GC (Gold) | 1.72 | Best - gold trends well |
| ES (S&P 500) | 1.04 | Marginally positive |
| CL (Crude Oil) | 1.05 | Marginally positive |
| 6E (EUR/USD) | 0.96 | Slight drag |
| ZB (Bonds) | 0.94 | Slight drag |
| NQ (Nasdaq) | 0.59 | Worst - VMR shorts hurt in bull mkt |

**PF by Strategy (IS period):**

| Strategy | PF | Comment |
|----------|-----|---------|
| TREND (TMA+DCS agree) | 1.06 | Positive but below target |
| VMR (Mean Reversion) | 0.76 | LOSING — primary drag |

---

## 6. Root Cause Analysis: Why the Strategy Is Underperforming

### Primary Issues

**1. VMR SHORT Signals Are Losing (PF=0.76)**

VMR SHORT fires when RSI5 > 75 (overbought conditions). In a sustained bull market (2009-2024), overbought readings are common and prices continue higher after the entry — making most VMR SHORT trades losers.

- ES VMR_SHORT: 75 entries in 2020-2024, most losing
- NQ VMR_SHORT: Even worse — strong bull trend overwhelms mean-reversion

**2. Data Proxy Limitation**

We used Yahoo Finance ETF data (SPY, QQQ, GC=F, etc.) instead of actual CME futures contracts. The proxy overstates the bull-market bias because:
- ETFs include dividends/distributions that inflate returns
- Missing the 2001-2003 dot-com crash (data starts 2003-12-01) — the ideal environment for this strategy
- Futures carry costs differ from ETF tracking

**3. Signal Frequency Too Low**

The strategy generates ~2.65 trades/month across 6 markets on daily bars. With so few trades:
- Small sample size makes statistics unreliable
- A handful of bad trades significantly impacts annual PF
- High fixed costs ($10 commission) matter more at low frequency

**4. 2022 Rate Hike Year Lost Money (-4.7%)**

Expected to be a profitable year (bonds AND equities trending down). The bot actually lost because:
- ZB trend signals fired but position sizes were reduced by HIGH_VOL regime
- CL and ES short entries were delayed by Signal Agreement Filter requiring both TMA+DCS agreement
- The simultaneous crash regime flagged as CRISIS early, cutting all sizes to 0%

---

## 7. Path to PF 3.0-3.5 and 87%+ Win Rate

This is the key question for Phase 5 design. Here is the honest assessment:

### Option A: Fix the Current Daily-Bar Strategy

**Changes needed:**
1. **Disable VMR SHORT** — remove RSI>75 overbought shorts completely. VMR LONG (oversold bounces in ranging markets) can stay. Estimated PF improvement: +0.15 to +0.25
2. **Tighten trend entries** — require ADX > 28 (not 25) for TRENDING regime. Fewer but higher-quality signals
3. **Add profit target at 3R** — exit 50% of position at 3× initial risk, let remainder ride
4. **Relax Signal Agreement** on GC — gold trends beautifully; allow DCS-only entries on GC

**Realistic outcome:** PF 1.3-1.6 on daily bars. Better, but still well below 3.0 target.

**Win rate:** Can realistically improve from 50% to 55-60% with changes. Getting to 87% is not realistic on daily-bar trend following (a 2-3 week hold has too much noise to hit 87%).

### Option B: Switch to Intraday (Recommended for high win rate / PF target)

**Architecture:**
- 5-minute bars during RTH (Regular Trading Hours)
- 60-90 minute sessions: 9:30am-11:00am and 1:30pm-3:30pm EST
- Target: 8-15 trades/day (not 50-70 — explained below)
- Strategy: scalping momentum off opening range breakouts

**Why 50-70 trades/day is unrealistic for this strategy:**
- At $10 commission per round-turn, 60 trades/day = $600/day in commissions alone on a $150k account — that's 0.4% of equity just in costs
- Win rate at 50-70 trades/day (very fast scalping) would be 55-65%, not 87%
- To achieve 87% win rate, you need: tight stops, wide targets, or a very specific market microstructure edge (like order flow)

**What 87% win rate actually looks like:**
- Strategies that win 87% of the time have SMALL wins and LARGE losses (negative skew)
- Examples: short gamma (options selling), martingale, very tight targets with wide stops
- PF of 3.0 with 87% win rate requires avg win/loss ratio of only 0.38 — meaning each win is $38 and each loss is $100
- This is NOT a trend-following architecture — it's a completely different strategy type

### Option C: Hybrid (Best of Both)

**Keep the daily-bar framework for swing positions** (3-20 day holds on GC, ZB, ES) + **Add an intraday module** for ES opening range breakouts on 5-min bars.

- Swing trades: 2-4/week, targeting $500-2000/trade
- Intraday scalps: 5-8/day, targeting $200-400/trade
- Combined daily P&L target: $300-800/day
- Win rate: 55-65% (achievable with improvements)
- PF: 1.8-2.5 (realistic with both improvements and intraday)

---

## 8. Validation Infrastructure Summary

The Phase 4 framework correctly identifies strategy failures and provides detailed breakdowns. When strategy improvements are made in Phase 5, the validation suite can be re-run to measure improvement quantitatively.

### Validation Report JSON

All runs save to `reports/validation/validation_YYYY-MM-DD_HH-MM-SS.json` with full metrics, per-stage results, and fail reasons.

### Running the Validation

```python
from src.backtest.validation_runner import run_full_validation, save_validation_report
from src.backtest.data_loader import load_all_markets, load_config

config = load_config()
market_data = load_all_markets("2003-12-01", "2024-12-31", config)

report = run_full_validation(
    market_data=market_data,
    config=config,
    initial_capital=150_000.0,
    is_start="2004-01-01",
    is_end="2019-12-31",
    oos_start="2020-01-01",
    oos_end="2024-12-31",
)

filepath = save_validation_report(report)
print(report)
```

---

## 9. Checklist

- [x] `src/backtest/stress_tester.py` — COMPLETE
- [x] `src/backtest/regime_tester.py` — COMPLETE
- [x] `src/backtest/validation_runner.py` — COMPLETE
- [x] `test_phase4.py` — 9/9 PASS (2026-02-27)
- [x] Full 21-year backtest run and analyzed
- [x] Root cause analysis documented
- [x] Path to improvement documented
- [x] `docs/LAB_005_Validation_Report.md` — this file

---

## 10. Next Steps

**Immediate (Phase 5 planning):**
1. Decision required: Continue with daily-bar fixes OR pivot to intraday architecture
2. Disable VMR SHORT in config.yaml as a quick win
3. Run validation again with VMR SHORT disabled to measure improvement
4. Download real futures data (Norgate ~$270/yr) for accurate historical testing

**Phase 5 options:**
- A: Paper trade the current bot (even if PF < 2.0) to get real execution data
- B: Redesign the strategy with intraday component first, then paper trade the improved version
- C: Both — paper trade current bot while building intraday module in parallel
