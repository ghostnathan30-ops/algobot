# LAB_006 — Phase 5: Multi-Timeframe Architecture
**AlgoBot Project — Lab Report**
**Date:** 2026-02-28
**Phase:** 5 — MTF Strategy Architecture
**Status:** COMPLETE (24/24 tests PASS)

---

## 1. Overview

Phase 5 is the most significant architectural upgrade since the project began.
The Phase 4 validation revealed the core problem: the original bot produced a
Profit Factor of 0.96 (IS) and 1.03 (OOS) — both losing money after costs.

Phase 5 addresses the root causes with a top-down Multi-Timeframe approach:

| Root Cause (Phase 4) | Phase 5 Fix |
|----------------------|-------------|
| VMR SHORT PF=0.76 — main drag | VMR SHORT disabled in config |
| Counter-trend entries in bull market | HTF Bias Gate blocks against-trend signals |
| ADX 25 threshold too loose — low quality entries | ADX threshold raised to 28 |
| No awareness of weekly/monthly trend context | HTF Bias Engine added (weekly + monthly) |
| Daily-only, no intraday income | ORB intraday strategy added (5-min ES/NQ) |
| No real intraday data | QuantConnect API downloader added |

---

## 2. Files Created / Modified

| File | Type | Lines | Purpose |
|------|------|-------|---------|
| `config/config.yaml` | MODIFIED | +120 | v2.0: HTF, intraday, QC, VMR SHORT off, ADX 28 |
| `src/strategy/htf_bias.py` | NEW | ~310 | Weekly/monthly bias engine |
| `src/strategy/signal_combiner.py` | REWRITTEN | ~280 | HTF gate + VMR SHORT removal |
| `src/strategy/orb_signal.py` | NEW | ~330 | Opening Range Breakout intraday strategy |
| `src/utils/qc_downloader.py` | NEW | ~360 | QuantConnect API data downloader |
| `src/backtest/data_loader.py` | MODIFIED | +4 | HTF bias + updated combine_signals call |
| `test_phase5.py` | NEW | ~360 | 10-test, 24-assertion Phase 5 suite |
| `docs/LAB_006_MTF_Architecture.md` | NEW | this | Lab report |

**Total new code: ~1,760 lines**

---

## 3. Architecture Changes

### 3.1 HTF Bias Engine (`htf_bias.py`)

Resamples existing daily OHLCV data to weekly and monthly bars.
No new data required — uses what we already have from Yahoo Finance.

**Weekly bias:**
- EMA(8) and EMA(21) on weekly bars
- ADX(14) on weekly bars >= 18 required to confirm directional bias
- EMA_fast > EMA_slow + ADX OK = BULL
- EMA_fast < EMA_slow + ADX OK = BEAR
- Otherwise = NEUTRAL

**Monthly bias:**
- SMA(6) on monthly bars (6-month moving average)
- Price > SMA × 1.015 = BULL
- Price < SMA × 0.985 = BEAR
- Within ±1.5% of SMA = NEUTRAL

**Combined bias (default: relaxed mode):**
- Weekly leads. Monthly only vetoes direct contradictions.
- Weekly=BULL, Monthly=BEAR -> NEUTRAL (cancel out)
- Weekly=BULL, Monthly=NEUTRAL -> BULL (weekly wins)
- Weekly=NEUTRAL -> Monthly takes over

**New daily columns:**
```
htf_weekly_bias    BULL / BEAR / NEUTRAL
htf_monthly_bias   BULL / BEAR / NEUTRAL
htf_combined_bias  BULL / BEAR / NEUTRAL
```

### 3.2 Signal Combiner — HTF Bias Gate

The updated `combine_signals()` now accepts an optional `config` parameter.
When config is provided AND htf_bias columns exist:

| Signal | Gate Rule |
|--------|-----------|
| AGREE_LONG  | Blocked when `htf_combined_bias == BEAR` |
| AGREE_SHORT | Blocked when `htf_combined_bias == BULL` |
| VMR_LONG    | Blocked when `htf_weekly_bias == BEAR` |
| VMR_SHORT   | **Permanently disabled** (config flag) |

New column: `combined_htf_blocked` (bool) — True when gate suppressed a signal.

**Backward compatibility:** Old tests (Phase 2-4) call `combine_signals(df, market)`
without config. Gate is skipped in that case — all old tests pass unchanged.

### 3.3 Config v2.0 Changes

```yaml
# Key parameter changes from v1.0:
strategy.vmr.vmr_short_enabled:   false   # was: true  (VMR SHORT disabled)
regime.threshold_trending:         28      # was: 25    (stricter ADX)

# New sections added:
htf_bias:           weekly/monthly bias configuration
intraday:           ORB strategy parameters
quantconnect:       API configuration + market ticker mapping
```

### 3.4 ORB Intraday Strategy (`orb_signal.py`)

**Markets:** ES and NQ only (most liquid, best intraday edge)
**Timeframe:** 5-minute bars (requires QC data)

**Opening Range:** 9:30 AM – 10:00 AM ET (first 6 bars)
- Range = high/low of all bars in this window
- This is the "institutional auction zone" for the day

**Entry:**
- Long: Close > range_high + 1 tick
- Short: Close < range_low - 1 tick
- Only the FIRST breakout per direction per day is taken

**Exit:**
- 50% off at 1R (partial profit lock)
- Remaining 50% trails, target 2R
- Hard close after 24 bars (2 hours = no midday chop exposure)

**HTF filter:**
- Long ORB: only when weekly_bias != BEAR
- Short ORB: only when weekly_bias != BULL
- NEUTRAL allows both directions

### 3.5 QuantConnect API Downloader (`qc_downloader.py`)

**How to connect your QuantConnect account:**
1. Go to quantconnect.com > Account > API Access
2. Copy your User ID (a number) and generate an API Token
3. Add to `AlgoBot/.env`:
   ```
   QC_USER_ID=12345
   QC_API_TOKEN=your_token_here
   ```
4. Test: `from src.utils.qc_downloader import check_qc_credentials; check_qc_credentials()`

Downloads 1-minute futures data by day, decompresses ZIP files, converts to OHLCV Parquet.
`load_intraday()` handles resampling to any resolution (1min, 5min, 15min, 60min).

---

## 4. Test Results (2026-02-28)

All 24 assertions across 10 test functions passed:

| Test | Name | Result |
|------|------|--------|
| 1 | Config v2.0 loads — all 3 new sections present | PASS (3 assertions) |
| 2 | VMR SHORT disabled in config | PASS |
| 3 | ADX trending threshold = 28 | PASS |
| 4 | HTF bias computes on 600 synthetic daily bars | PASS (4 assertions) |
| 5 | HTF gate blocks 100% of longs in BEAR bias market | PASS (2 assertions) |
| 6 | VMR SHORT = 0, VMR LONG still fires | PASS (2 assertions) |
| 7 | HTF bias integrated in full indicator pipeline | PASS (2 assertions) |
| 8 | QC downloader structure + graceful no-credentials | PASS (3 assertions) |
| 9 | ORB signals on 15 synthetic intraday days | PASS (3 assertions) |
| 10 | ORB HTF gate blocks shorts in BULL, longs in BEAR | PASS (3 assertions) |

---

## 5. Expected Performance Impact

Based on Phase 4 analysis, these Phase 5 changes should improve PF:

| Change | Estimated PF Improvement | Basis |
|--------|--------------------------|-------|
| VMR SHORT disabled | +0.15 to +0.25 | VMR SHORT was PF=0.76 on IS data |
| HTF bias gate | +0.20 to +0.40 | Eliminates ~30-40% of counter-trend entries |
| ADX threshold 25->28 | +0.05 to +0.15 | Fewer marginal trend entries, higher quality |
| ORB intraday (ES/NQ) | +0.10 to +0.30 | New source of daily income (QC data needed) |

**Projected combined PF: 1.4 to 2.0 on daily-bar swing + intraday ORB**

This is still below the 2.5-3.0 backtest target — but it is the first version
that should produce positive real-world expectancy. The next validation run
(Phase 5B) will measure the actual improvement on the 2020-2024 OOS period.

---

## 6. Next Steps

### Immediate (can do now, with existing data)
1. Run full validation on 2020-2024 OOS with Phase 5 improvements
2. Compare PF before/after: baseline (OOS PF=1.03) vs improved
3. Confirm HTF bias gate reduces losing trades without killing winners

### Requires QC credentials
4. Set up QC_USER_ID and QC_API_TOKEN in .env
5. Download 5 years of ES and NQ 1-minute data
6. Run ORB backtest on 5-min bars
7. Compute combined (swing + ORB) equity curve

### Phase 6 — Paper Trading
8. Deploy improved bot in paper mode
9. Monitor for 30-60 days
10. Compare live signals to backtest signals (deviation < 25%)

---

## 7. Checklist

- [x] `config/config.yaml` v2.0 — COMPLETE
- [x] `src/strategy/htf_bias.py` — COMPLETE
- [x] `src/strategy/signal_combiner.py` updated — COMPLETE
- [x] `src/strategy/orb_signal.py` — COMPLETE
- [x] `src/utils/qc_downloader.py` — COMPLETE
- [x] `src/backtest/data_loader.py` updated — COMPLETE
- [x] `test_phase5.py` — 24/24 PASS (2026-02-28)
- [x] `docs/LAB_006_MTF_Architecture.md` — this file
- [ ] QC credentials configured (.env) — PENDING (user action)
- [ ] Intraday data downloaded — PENDING (requires QC credentials)
- [ ] Full OOS validation re-run — PENDING (Phase 5B)
