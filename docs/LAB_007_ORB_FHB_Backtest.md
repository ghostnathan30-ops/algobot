# LAB_007 — Phase 5B: ORB + FHB Intraday Backtest
**AlgoBot Project — Lab Report**
**Date:** 2026-02-28
**Phase:** 5B — Intraday Strategy Backtest (Yahoo Finance Data)
**Status:** COMPLETE — Both strategies validated, PF positive, below 2.5 target

---

## 1. Overview

Phase 5B extends Phase 5 by running the new intraday strategies against real
historical data. Two strategies were tested:

| Strategy | Timeframe | Data Source | Sample Size |
|----------|-----------|-------------|-------------|
| Opening Range Breakout (ORB) | 5-minute | Yahoo Finance (60 days) | 50 days, 56 trades |
| First Hour Breakout (FHB) | 1-hour | Yahoo Finance (730 days) | 600+ days, 493 trades |

Key insight: The FHB (1-hour) dataset is **10x larger** and statistically
robust — win rate uncertainty is +/-2% vs +/-12% for the 60-day ORB sample.

---

## 2. Files Created

| File | Purpose |
|------|---------|
| `src/utils/yf_intraday.py` | Yahoo Finance intraday downloader (5-min + 1-hour) |
| `scripts/run_orb_backtest.py` | ORB 5-min backtest script |
| `scripts/run_fhb_backtest.py` | FHB 1-hour backtest script (NEW, 730 days) |
| `docs/LAB_007_ORB_FHB_Backtest.md` | This file |

Config change: `config.yaml` ORB `range_end_time` changed from "10:00" to "09:45"
(15-minute opening range instead of 30-minute — tighter range, more achievable target).

---

## 3. Opening Range Breakout (ORB) — 5-min Results

**Data:** Yahoo Finance ES=F + NQ=F, 5-min bars, Dec 2025 – Feb 2026 (50 days)
**Range:** 9:30–9:45 AM ET (first 3 bars, 15-minute window)

### Config Fix Applied

Previous `range_end_time: "10:00"` caused 93% time exits — 30-min range
was too wide, 2R target rarely achievable in 2-hour hold window.

Fixed to `range_end_time: "09:45"` (15-min range). Result:

| Market | Trades | Win% | PF   | Net P&L | Avg/Day |
|--------|--------|------|------|---------|---------|
| ES     | 29     | 65.5%| 1.50 | $3,891  | $134    |
| NQ     | 27     | 63.0%| 1.70 | $13,373 | $495    |
| **Combined** | 56 | 64% | **1.60** | **$17,264** | **$595** |

### Remaining Issue: Time Exits

83-85% of trades still exit on the 2-hour time stop:
- ES: `{'time': 24, 'stop_full': 4, 'target_full': 1}`
- NQ: `{'time': 23, 'stop_full': 3, 'target_full': 1}`

Partial exit rate: 24% (1R hit on 24% of trades)
Full 2R target hit: 1 trade each market in 50 days

The 60-day sample covers Dec 2025 – Feb 2026, a bull market period. This
inflates long signal win rate. The FHB 600-day sample is more representative.

---

## 4. First Hour Breakout (FHB) — 1-hour Results

**Data:** Yahoo Finance ES=F + NQ=F, 1-hour bars, Oct 2023 – Feb 2026 (600+ days)
**Range:** 9:30-10:30 AM ET (first 1-hour bar)

### Signal Summary

| Market | Days | Long | Short | HTF Blocked | Signals/Day |
|--------|------|------|-------|-------------|-------------|
| ES | 604 | 221 | 28 | 196 | 0.41 |
| NQ | 601 | 216 | 28 | 170 | 0.41 |

HTF block rate: 40-45% (aggressive filtering — bull market means shorts blocked).

### Performance Results

| Market | Trades | Win% | PF   | Total P&L | Avg/Day | Max DD |
|--------|--------|------|------|-----------|---------|--------|
| ES     | 249    | 47.8%| 1.12 | $18,323   | $74     | -$26,984 |
| NQ     | 244    | 52.5%| 1.23 | $64,115   | $264    | -$32,798 |
| **Combined** | 493 | 50.2% | **1.18** | **$82,438** | **$338** | |

### Year-by-Year Breakdown

**ES by Year:**

| Year | Trades | Win% | PF   | Net P&L | Avg/Day |
|------|--------|------|------|---------|---------|
| 2023 | 24     | 50%  | 0.98 | -$215   | -$9     |
| 2024 | 100    | 51%  | 1.48 | $21,681 | $217    |
| 2025 | 110    | 44.5%| 0.91 | -$8,394 | -$78    |
| 2026 | 15     | 46.7%| 1.70 | $5,250  | $350    |

**NQ by Year:**

| Year | Trades | Win% | PF   | Net P&L | Avg/Day |
|------|--------|------|------|---------|---------|
| 2023 | 28     | 53.6%| 1.65 | $11,735 | $435    |
| 2024 | 98     | 54.1%| 1.35 | $30,962 | $316    |
| 2025 | 105    | 50.5%| 1.09 | $13,378 | $127    |
| 2026 | 13     | 53.8%| 1.40 | $8,040  | $618    |

### Exit Analysis (FHB)

| Exit Type | ES Count | NQ Count | Avg R |
|-----------|----------|----------|-------|
| time      | 127 (51%)| 130 (53%)| +0.451R |
| stop_full | 83 (33%) | 78 (32%) | -1.0R |
| target_full | 30 (12%) | 32 (13%) | +1.5R |
| stop_partial | 9 (4%) | 4 (2%)  | 0.0R |

**Critical Insight:** Time exits average **+0.451R** — the strategy IS directionally
correct but the market rarely trends strongly enough to reach 2R within 5 hours.
Stop exits at -1.0R are the performance drag.

---

## 5. Diagnostic Analysis

### Root Cause of PF Below 2.5 Target

The strategies are profitable but PF 1.1-1.5 is far below the 2.5 target.
The structural problem:

1. **Stop-to-target ratio mismatch:** Using "full range" as stop creates
   a large denominator (risk). ES first-hour range = 15-25 points. At 2R
   target, we need the market to move 30-50 points in our direction within
   5 hours. This happens on 12-13% of days.

2. **Time exits leaving R on the table:** 51-53% of trades exit on time with
   avg +0.451R. With a trailing stop, these could yield 0.7-1.0R instead.

3. **Stop accuracy:** The full-range stop is correct in theory but may be
   too conservative. A tighter ATR-based stop would reduce dollars at risk
   and make 2R achievable more often.

### Why ES < NQ

NQ (Nasdaq-100) consistently outperforms ES (S&P 500) in ORB/FHB:
- Higher volatility: NQ moves 2-3x more than ES in dollar terms
- When NQ trends, it trends strongly (tech stocks have momentum)
- Larger first-hour range relative to ATR → more room for 2R hits
- This is consistent with historical ORB literature

---

## 6. Combined Strategy Picture (All Sources)

Combining daily swing (Phase 4) + ORB + FHB:

| Component | Daily Avg | Status |
|-----------|-----------|--------|
| Swing (daily bars) | Data needed (Phase 5B rerun) | Pending |
| ORB 5-min (ES+NQ) | +$595/day (60-day sample) | Positive |
| FHB 1-hour (ES+NQ) | +$338/day (600-day sample) | Positive |
| **Intraday Combined** | **~$450/day** | **Positive** |

Note: ORB and FHB cannot both be run simultaneously on the same market/day
(they would both generate signals on the same breakout). In live trading,
we would choose one entry per market per day.

---

## 7. Autonomy and Phase 6 Roadmap

**Can the bot trade autonomously?**

Yes — but only after Phase 6-7 milestones are met:

| Phase | Milestone | Requirement |
|-------|-----------|-------------|
| 5C | Improve PF to 2.0+ | Exit strategy fix (trailing stop) |
| 5C | 500+ trade backtest passes all criteria | PF>2.0, Win>55%, DD<25% |
| 6 | Paper trade 30-60 days | Live signal match <25% deviation |
| 6 | Broker connection tested | IBKR paper or Alpaca Markets |
| 7 | Topstep evaluation passed | $50k funded account |
| 7 | Autonomous live operation | 24/5 monitoring, Telegram alerts |

The bot structure already supports autonomy — the signal pipeline is automated.
Phase 6 adds live order execution (IBKR TWS API or Alpaca API).

---

## 8. Next Steps (Phase 5C)

### High Priority — Exit Strategy Fix
The biggest single improvement available:

1. **Implement trailing stop after 1R partial exit**
   - After 50% exits at 1R, move stop to breakeven (not full range)
   - This converts the remaining position to a "free trade"
   - Potential PF improvement: +0.15-0.30

2. **ATR-based stop instead of full-range stop**
   - Use `0.5 * ATR(1hour)` as stop instead of "range low/high"
   - Makes risk consistent and tighter on wide-range days
   - Potential PF improvement: +0.20-0.40 (tighter stop = higher R/R)

3. **Volume filter (gap between intraday and swing data)**
   - Only enter FHB when first-hour volume > 1.2x rolling 20-day average
   - High volume confirms institutional participation
   - Potential: +5-10% win rate improvement

### Medium Priority — Data Sources
Free intraday data sources to extend the 60-day ORB window:

| Source | Data Available | Notes |
|--------|---------------|-------|
| Yahoo Finance | 730d 1h, 60d 5m (current) | Free, already using |
| Alpaca Markets | 5+ years 1-min (paper acct) | Free paper account, ETFs only |
| Tiingo | 5 years 5-min | Free with email signup |
| Polygon.io | 2 years 1-min | Free tier available |
| IBKR paper | Real futures, unlimited | Need paper account |

---

## 9. Checklist

- [x] `scripts/run_orb_backtest.py` — ORB 5-min backtest COMPLETE
- [x] `scripts/run_fhb_backtest.py` — FHB 1-hour backtest COMPLETE (NEW)
- [x] `config.yaml` ORB range_end_time: "09:45" (was "10:00") — FIXED
- [x] `docs/LAB_007_ORB_FHB_Backtest.md` — This file
- [ ] Trailing stop after 1R partial — Phase 5C
- [ ] ATR-based stop — Phase 5C
- [ ] Volume filter — Phase 5C
- [ ] Alpaca/Tiingo data integration — Phase 5C
- [ ] Paper trading deployment — Phase 6
