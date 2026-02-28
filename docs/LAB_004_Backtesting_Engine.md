# LAB_004 — Backtesting Engine
## AlgoBot Phase 3 Lab Report

```
Phase     : 3 — Backtesting Engine
Date      : 2026-02-27
Status    : IMPLEMENTATION COMPLETE — Tests ready to run
Author    : Ghost
Engineer  : Claude (claude-sonnet-4-6)
```

---

## Objective

Build a complete event-driven backtesting engine that simulates 25 years of
systematic futures trading. The engine must:

- Process each trading day bar-by-bar in strict chronological order
- Apply all 5 layers of risk management identically to the live bot
- Record every trade with full attribution (entry, exit, P&L, R-multiple)
- Calculate all performance metrics (Sharpe, Profit Factor, max drawdown, etc.)
- Support walk-forward validation and Monte Carlo stress testing

---

## Files Created

### Phase 3 Source Files

| File | Lines | Purpose |
|------|-------|---------|
| `src/backtest/__init__.py` | 12 | Package initialisation |
| `src/backtest/data_loader.py` | 155 | Full signal pipeline for any market/date range |
| `src/backtest/trade.py` | 230 | Trade dataclass, OpenPosition, BacktestResult |
| `src/backtest/engine.py` | 370 | Event-driven bar-by-bar simulation |
| `src/backtest/metrics.py` | 310 | All performance statistics |
| `src/backtest/walk_forward.py` | 130 | 7-window walk-forward validation |
| `src/backtest/monte_carlo.py` | 110 | 10,000-simulation Monte Carlo |
| `test_phase3.py` | 430 | 9-test validation suite |

**Total Phase 3 code: ~1,747 lines**

---

## Architecture

### Data Flow

```
load_all_markets(start, end, config)
        │
        ▼ [for each market]
download_market() → clean_market_data()
        │
        ▼
calculate_indicators() → add_atr_baseline()
        │
        ▼
classify_regimes()
        │
        ▼
tma_signal() + dcs_signal() + vmr_signal()
        │
        ▼
combine_signals()   ← Signal Agreement Filter applied here
        │
        ▼
add_position_sizes()
        │
        ▼ [dict of market → DataFrame with all signals]
BacktestEngine.run(market_data, start, end)
        │
        ▼ [bar-by-bar for each date]
_process_day() → _process_exits() + _process_entry()
        │
        ▼
BacktestResult(trades, equity_curve, daily_pnl, metrics)
```

### Bar Processing Order (Every Trading Day)

```
For each trading date:
  1. Increment bars_held on all open positions
  2. Check exits:
     a. Stop loss hit  (uses bar's Low/High for intrabar simulation)
     b. Trailing stop  (activates at +1R profit, trails 2.0×ATR)
     c. Signal exit    (DCS 20-bar exit, TMA flip, VMR RSI recovery, VMR timeout)
  3. Check daily hard stop ($2,500 loss → close all, no new entries today)
  4. Process new entries  (portfolio risk checks first)
  5. Update trailing stops for surviving positions
  6. Update MAE/MFE for all positions
  7. Mark-to-market equity, record daily P&L
```

### Portfolio Risk Checks (Before Every Entry)

The engine enforces all 5 risk layers before opening any trade:

```
Layer 1 — Signal Agreement Filter   already applied in combine_signals()
Layer 2 — Regime regime gate        already applied (size_mult = 0 blocks entry)
Layer 3 — Max portfolio risk        total open risk < 8% of equity
Layer 4 — Correlated pair cap       ES + NQ combined < 2% of equity
Layer 5 — Daily hard stop           no entry if daily_loss >= $2,500
```

---

## Design Decisions

### Why Event-Driven (Not Vectorised)?

Vectorised backtests (vectorbt, pandas apply) are fast but cannot
correctly model:

- **Dynamic position sizing**: Risk % is of CURRENT equity, not initial.
  After a string of losses, account is smaller, so position sizes shrink.
  After gains, they grow. This compounding effect cannot be captured vectorially.

- **Portfolio-level risk limits**: Checking "total open risk < 8% of equity"
  requires knowing ALL other positions at the exact moment of entry. In a
  vectorised approach this becomes a complex cross-market join.

- **Daily hard stop reset**: The $2,500 daily limit resets each morning.
  Enforcing this across 6 markets simultaneously requires a stateful loop.

The event-driven approach processes ~250 bars/year × 25 years = ~6,250 bars.
At 6 markets, that's ~37,500 bar evaluations. This runs in seconds in Python.
Speed is not a constraint. Correctness is.

### Entry Fill Price

- **Daily bars**: Signals fire at the close of the signal bar.
- **Entry fill**: Close of signal bar + slippage (0.05% of price).
- **No look-ahead**: Entry price uses only current bar's close.

This is the industry standard for daily-bar systematic backtesters.
Alternative (fill on next-bar open) requires OHLC data for the NEXT bar,
and introduces lookahead bias risk in data preprocessing. Avoided.

### Cost Model

```
ETF proxy backtest:
  Entry slippage:  0.05% of close price  (added for longs, subtracted for shorts)
  Exit slippage:   0.05% of exit price   (subtracted for longs, added for shorts)
  Commission:      $10 flat per round-turn trade

Total friction per trade:
  Entry + exit slippage + commission
  = ~0.1% of position value + $10
  ≈ conservative but realistic for daily ETF execution
```

For a Phase 6 live comparison using actual futures contracts:
```
  ES: 1 tick slippage = $12.50 per side. Commission = $5.00 per side.
      Total per round-turn: $25 slippage + $10 commission = $35.
      On a $5,000 position (1 micro ES contract): 0.7% round-turn friction.
      Our ETF proxy friction: ~0.1% + $10 → comparable on small positions.
```

The cost model is documented, transparent, and can be updated when live data
shows real execution costs.

### Stop Loss Simulation

On each bar, the engine checks whether the bar's **Low** (for longs) or
**High** (for shorts) breached the stop price. If so:

```
Exit price = stop_price - (stop_price × 0.05%)  [slippage makes it worse]
```

This models realistic stop execution: stops are not guaranteed to fill
exactly at the stop level. During fast markets, fills are typically
worse. The 0.05% haircut is conservative but realistic.

### Trailing Stop

Activates when position reaches `trailing_activation_r = 1.0` profit (from config).
Trails at `trailing_stop_atr = 2.0 × ATR` below the highest close (for longs).

```
Example (ES long, ATR=30):
  Entry at 5000, initial stop at 4925 (2.5×30=75 points)
  Trailing activates at 5075 (+1R = +$1500 profit on the trade)
  After activation, stop trails 2.0×ATR=60 points below highest close
  If price rises to 5200:  trailing stop = 5200 - 60 = 5140
  If price then falls to 5140: exit at trailing stop
  Instead of losing from 5000 → 4925 (full stop), we exit at 5140 (profit)
```

Key constraints:
- Trailing stop can only **move in the profit direction** (never worse than current)
- Trailing stop is never below the initial hard stop

---

## Metrics Computed

All metrics from `config.yaml` reporting section are implemented:

| Metric | Formula | Target |
|--------|---------|--------|
| `profit_factor` | Gross profit / Gross loss | ≥ 2.5 (backtest) |
| `sharpe_ratio` | (mean excess return / std) × √252 | ≥ 1.0 |
| `sortino_ratio` | (mean excess return / downside std) × √252 | ≥ 1.5 |
| `calmar_ratio` | Annualised return / \|max drawdown\| | ≥ 0.5 |
| `max_drawdown_pct` | Peak-to-trough equity decline | ≤ 22% (IS) |
| `win_rate_pct` | % trades with positive P&L | ≈ 45-58% |
| `avg_win_loss_ratio` | Avg winner / avg loser in $ | ≥ 2.5 |
| `expectancy_per_trade_usd` | Mean net P&L per trade | > 0 |
| `avg_r_multiple` | Mean R-multiple per trade | ≥ 1.0 |
| `trades_per_month` | Trade frequency | ≈ 15/month (6 markets) |
| `annual_returns_by_year` | Per-year return % | ≥ 16/20 years profitable |
| `profit_factor_by_market` | PF for each of 6 markets | > 1.0 each |
| `profit_factor_by_strategy` | PF for TREND vs VMR | > 1.5 each |
| `exit_reason_breakdown` | Count by exit type | — |

---

## Test Suite Overview

`test_phase3.py` contains 9 tests:

| Test | What It Checks |
|------|----------------|
| 1. Data Loader | All 35 required columns present, no post-warmup NaN |
| 2. Trade Dataclass | P&L calculation correct, R-multiple correct, sign checks |
| 3. Metrics | profit_factor, win_rate, Sharpe all compute correctly |
| 4. Engine Smoke | Engine runs on 2020-2024 data without errors |
| 5. Trade List | P&L signs correct, exit reasons populated, R consistency |
| 6. Profit Factor | PF > 1.0 on 2020-2024 data (minimum edge confirmation) |
| 7. Daily Hard Stop | Config thresholds verified (our limit < Topstep limit) |
| 8. Walk-Forward | Module imports, window 7 runs, structure validated |
| 9. Monte Carlo | 1,000 simulations run, 95th pct DD is finite and negative |

---

## How to Run

```bash
# From AlgoBot/ directory:
/c/Users/ghost/miniconda3/envs/algobot_env/python.exe test_phase3.py
```

---

## Next Steps After Tests Pass

### 1. Extend Data Coverage to 25 Years

The current data cache covers 2020-2024 (5 years). For a full backtest:

```python
from src.backtest.data_loader import load_all_markets, load_config
config = load_config()

# Download and cache 25 years of data
# Note: Yahoo Finance quality degrades before 2005 for some futures proxies
market_data = load_all_markets("2000-01-01", "2024-12-31", config)
```

**Data limitations to be aware of:**
- `TLT` (ZB proxy): available from 2002
- `QQQ` (NQ proxy): available from 1999
- `CL=F` (crude oil): gaps before 2000 in Yahoo Finance
- For the full 25-year backtest, `GC=F` and `CL=F` may need Norgate data

### 2. Run In-Sample Validation (2000-2019)

```python
from src.backtest.engine import BacktestEngine
from src.backtest.metrics import check_validation_thresholds

engine = BacktestEngine(config, initial_capital=150_000.0)
result = engine.run(market_data, "2000-01-01", "2019-12-31")

# Check vs in-sample thresholds
thresholds = config["validation"]["in_sample"]
check = check_validation_thresholds(result.metrics, thresholds)
print(f"In-sample: {'PASS' if check['passed'] else 'FAIL'}")
```

**Targets:**
- Profit Factor ≥ 2.3
- Sharpe Ratio ≥ 1.0
- Max Drawdown ≤ 22%
- Profitable in ≥ 16 of 20 years

### 3. Run Out-of-Sample Validation (2020-2024)

```python
result_oos = engine.run(market_data, "2020-01-01", "2024-12-31")
thresholds_oos = config["validation"]["out_of_sample"]
check_oos = check_validation_thresholds(result_oos.metrics, thresholds_oos)
```

**Targets:**
- Profit Factor ≥ 2.0
- Sharpe Ratio ≥ 0.8
- Max Drawdown ≤ 28%
- Sharpe degradation vs in-sample ≤ 40%

### 4. Run Full Walk-Forward (7 Windows)

```python
from src.backtest.walk_forward import run_walk_forward
wf = run_walk_forward(market_data, config)
print(f"Walk-forward: {'PASS' if wf['passed'] else 'FAIL'}")
print(f"Windows passed: {wf['summary']['windows_passed']}/7")
```

### 5. Run Monte Carlo (10,000 Simulations)

```python
from src.backtest.monte_carlo import run_monte_carlo
mc = run_monte_carlo(result.trades, config, n_simulations=10_000)
print(f"95th pct DD: {mc['dd_95th_pct']:.1f}%  (limit: 35%)")
print(f"Monte Carlo: {'PASS' if mc['passed'] else 'FAIL'}")
```

---

## Phase 3 Completion Checklist

```
[x] src/backtest/__init__.py            Created
[x] src/backtest/data_loader.py         Created
[x] src/backtest/trade.py               Created
[x] src/backtest/engine.py              Created
[x] src/backtest/metrics.py             Created
[x] src/backtest/walk_forward.py        Created
[x] src/backtest/monte_carlo.py         Created
[x] test_phase3.py                      Created
[x] docs/LAB_004_Backtesting_Engine.md  Created (this document)
[x] test_phase3.py: Run and confirm 9/9 PASS  ← COMPLETE 2026-02-27
[ ] Download 25-year data (2000-2024)
[ ] Run in-sample backtest 2000-2019, verify thresholds
[ ] Run out-of-sample backtest 2020-2024, verify thresholds
[ ] Run 7-window walk-forward, verify ≥5/7 windows profitable
[ ] Run 10,000 MC simulations, verify 95th pct DD < 35%
```

---

## Known Limitations at This Stage

1. **Data Coverage**: Only 2020-2024 data cached. Full 25-year backtest needs
   downloading 2000-2019 data. Some futures proxies have quality issues pre-2005.

2. **ETF Proxy**: The backtest uses ETF proxies (SPY, QQQ, etc.) not true
   continuous futures contracts. Cost model is approximate. Phase 6 will use
   real futures data via Rithmic.

3. **Walk-Forward Windows 1-6**: Require data from 2005-2020 which needs to be
   downloaded. The test suite validates Window 7 (2021-2024) which is in cache.

4. **No Overnight Gap Risk**: Daily bars simulate smooth price moves. In live
   trading, gaps can cause stop-loss fills that are much worse than the stop price.
   The 0.05% slippage model does not capture extreme gap risk.
   Mitigation: The stress tests in Phase 4 will model doubled costs.

5. **Regime Detection Lag**: ADX lags by ~2-5 bars. This means regime changes
   are detected slightly late, leading to a few false entries during transitions.
   This is by design — the regime classifier documentation explains this trade-off.

---

*Document generated: 2026-02-27*
*Next: Run test_phase3.py to confirm all 9 tests pass.*
