# AlgoBot — Operations Manual

> Last updated: 2026-04-04
> Version: v4.1 — TradingView Paper Trading (FHB Long-Only + GC/MGC + VWAP Strategies added)

---

## Table of Contents

1. [What This Bot Does](#1-what-this-bot-does)
2. [Project Structure](#2-project-structure)
3. [How the Strategies Work](#3-how-the-strategies-work)
4. [Backtest Results](#4-backtest-results)
5. [Prerequisites](#5-prerequisites)
6. [Daily Startup — Step by Step](#6-daily-startup--step-by-step)
7. [Starting from Any Terminal — Mac & Windows](#7-starting-from-any-terminal)
8. [Dashboard](#8-dashboard)
9. [Configuration](#9-configuration)
10. [Risk Controls](#10-risk-controls)
11. [Running Backtests](#11-running-backtests)
12. [Backup and Restore](#12-backup-and-restore)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. What This Bot Does

AlgoBot is a systematic futures trading bot for NQ (Nasdaq 100) and MGC (Micro Gold) futures. It uses **TradingView Pine Script strategies** to detect signals and deliver them via webhook — no Interactive Brokers connection is required for paper trading.

**Two active strategies in v4:**
- **FHB v4 (First Hour Breakout)** — NQ1! 15m chart, Long only (shorts disabled as of 2026-04-03, PF=0.918), signal fires 10:30–12:00 ET
- **GC Mean Reversion** — MGC1! 30m chart, fades first-hour breakouts, signal fires 10:30–13:00 ET

**It is NOT a continuous scanner.** Pine Scripts on TradingView monitor the charts and fire a webhook on bar close when conditions are met. The bot receives the webhook, simulates the trade, and monitors stops/targets via yfinance.

**Trading schedule (all times US Eastern):**

| Time | Action |
|------|--------|
| Before 09:15 | Run `bash scripts/start_trading_day.sh` — starts dashboard + cloudflared tunnel |
| 09:15 | Copy tunnel URL from terminal → paste into TradingView alert webhook URLs |
| 09:30–10:30 | Opening range captured by Pine Scripts on TradingView |
| 10:30–12:00 | **FHB signal window** — NQ1! 15m bar close fires webhook if breakout + HTF aligned |
| 10:30–13:00 | **GC signal window** — MGC1! 30m bar close fires webhook if mean-reversion fade |
| 16:05 | EOD: bot settles all open positions at last price, prints summary, exits |

---

## 2. Project Structure

```
AlgoBot/
├── config/
│   └── config.yaml              # All strategy parameters (single source of truth)
│
├── pine/                        # TradingView Pine Script v6 strategies
│   ├── fhb_strategy.pine        # FHB — NQ1! 15m (ACTIVE)
│   ├── gc_strategy.pine         # GC mean-reversion fade — MGC1! 30m (ACTIVE)
│   ├── orb_strategy.pine        # ORB — archived, disabled in v3
│   └── cl_strategy.pine         # CL FHB — archived, disabled in v3
│
├── src/
│   ├── strategy/
│   │   ├── gc_signal.py         # Gold mean-reversion signal (ACTIVE)
│   │   ├── htf_bias.py          # Higher timeframe bias filter (weekly/monthly)
│   │   ├── orb_signal.py        # Opening Range Breakout signal (disabled in v3)
│   │   ├── cl_signal.py         # Crude Oil FHB signal (disabled in v3)
│   │   ├── london_open_signal.py# 6E London Open (parked — no edge confirmed)
│   │   ├── signal_combiner.py   # Routes signals through all gates
│   │   ├── indicators.py        # EMA, ATR, RSI, ADX, Donchian calculations
│   │   ├── regime_classifier.py # Market regime detection (ADX-based)
│   │   └── position_sizer.py    # Risk-based position sizing
│   │
│   ├── execution/
│   │   ├── paper_simulator.py   # PaperSimulator — TV paper mode (no IBKR needed)
│   │   ├── tv_data_feed.py      # yfinance live data feed for PaperSimulator
│   │   ├── ibkr_bridge.py       # IBKR TWS connection + order submission (live/IBKR mode)
│   │   └── live_signal_engine.py# Real-time signal generation using IBKR data
│   │
│   ├── backtest/
│   │   ├── engine.py            # Core backtest loop
│   │   ├── metrics.py           # Profit factor, win rate, drawdown
│   │   ├── trade.py             # Trade object
│   │   ├── walk_forward.py      # Walk-forward validation
│   │   └── monte_carlo.py       # Monte Carlo simulation
│   │
│   └── utils/
│       ├── data_downloader.py   # Yahoo Finance data fetch
│       ├── econ_calendar.py     # FOMC, NFP, CPI, ECB event filter
│       ├── vix_filter.py        # VIX-based volatility filter
│       ├── trade_readiness.py   # GreenLight score gate
│       ├── trade_db.py          # SQLite trade log
│       └── logger.py            # Structured logging
│
├── scripts/
│   ├── start_trading_day.sh     # MAC LAUNCHER — Cloudflare tunnel + dashboard (run daily)
│   ├── start_bot.sh             # Mac simple launcher — dashboard + TV paper loop
│   ├── start_bot.ps1            # Windows launcher — dashboard + paper trading loop
│   ├── run_tv_paper_trading.py  # TV paper trading loop (no IBKR required)
│   ├── run_paper_trading.py     # IBKR paper trading loop (requires TWS)
│   ├── run_fhb_backtest.py      # FHB strategy backtest (NQ/ES)
│   ├── run_sc_backtest.py       # Sierra Charts OOS validation backtest
│   ├── run_comprehensive_backtest.py # 6-layer statistical validation
│   ├── run_validation_suite.py  # Full Monte Carlo + walk-forward suite
│   ├── run_signal_replay.py     # Replay historical signals for review
│   ├── generate_dashboard_data.py # Refresh dashboard JSON cache (~90s)
│   ├── create_backup.py         # Backup trades DB + config
│   ├── restore_backup.py        # Restore from backup
│   └── setup_dashboard_auth.py  # Set dashboard username/password (first time)
│
├── dashboard/
│   ├── server.py                # FastAPI dashboard server (JWT auth, WebSocket)
│   ├── auth.py                  # Login / session management
│   ├── bot_state.py             # Live bot state (running, P&L, positions)
│   ├── static/
│   │   ├── index.html           # Main dashboard page
│   │   ├── login.html           # Login page
│   │   └── control.html         # Bot control panel
│   └── cache/
│       ├── trades.json          # Generated trade data for dashboard
│       └── bot_state.json       # Live bot status
│
├── data/
│   ├── trades.db                # SQLite database of all live trades
│   ├── bot.log                  # Live bot output log
│   └── raw/                     # Cached historical price data (parquet)
│
├── reports/
│   ├── backtests/               # Backtest result CSVs and JSONs
│   │   ├── comprehensive_latest.json  # Latest 6-layer validation results
│   │   └── sc_backtest_latest.json    # Latest Sierra Charts OOS results
│   └── validation/              # Walk-forward validation reports
│
├── docs/                        # All documentation
├── logs/                        # Rotating application logs
├── config/config.yaml           # Strategy configuration
├── requirements.txt             # Python dependencies
└── .env                         # API credentials (never commit this)
```

---

## 3. How the Strategies Work

### Signal Delivery — TradingView Webhooks

In v3, signals are generated by Pine Script strategies running on TradingView charts. When a signal fires (on bar close), TradingView sends a JSON webhook to the bot via a Cloudflare tunnel. The bot's `PaperSimulator` receives the signal, records the entry, and monitors stops/targets every 60 seconds via yfinance.

This means **TWS is not required for paper trading.** The IBKR bridge (`ibkr_bridge.py`) is only needed for live brokerage order submission.

---

### FHB — First Hour Breakout ✅ Active (v4 — Long Only)
**Chart:** NQ1! (Nasdaq 100 Futures) · **Timeframe:** 15m · **Signal window:** 10:30–12:00 ET

- Captures the high/low of the 09:30–10:30 first hour on the 15m chart
- At bar close after 10:30, checks if price has broken above (LONG only) the range AND HTF EMA(50) is bullish
- **Short side disabled** as of 2026-04-03 — short PF=0.918 (net loser over 103 trades on TV backtest)
- **Market entry only** — pullback/limit entry disabled; it dropped WR from 63.5% → 22.97%
- Breakeven stop at +1R (protects profit, no partial exit)
- ATR × 0.8 stop, 2.5R target
- Green Light Score gate: < 40 skip, 40–59 half size, ≥ 60 full size

**TradingView backtest (2026-04-03):** 105 trades · WR=63.5% · PF=**1.73** · MaxDD=−$6,572
**Python backtest (2026-03-27):** 208 trades · WR=63.5% · PF=**2.87** · MaxDD=−$7,565
> PF gap (1.73 TV vs 2.87 Python) is due to TV's simpler fill model. Live paper trading will clarify which is closer to reality.

---

### GC — Gold Mean Reversion Fade ✅ Active
**Chart:** MGC1! (Micro Gold Futures, 10 oz) · **Timeframe:** 30m · **Signal window:** 10:30–13:00 ET

- **FADES first-hour breakouts** — the opposite of FHB. ~49% of GC first-hour breakouts fail and reverse due to VWAP magnetism.
- FHB LONG signal → GC goes SHORT (fade the upside breakout)
- FHB SHORT signal → GC goes LONG (fade the downside breakout)
- HTF fade filter: if HTF=BULL and FHB=LONG (confirmed bull breakout), skip the fade — too dangerous
- ATR × 1.0 stop placed above the breakout extreme, target = range midpoint or VWAP
- Always use **MGC** (Micro, 10 oz) — GC contract (100 oz) is too large for a $50K account (MaxDD 41% vs ~4% on MGC)
- Skips HIGH and MEDIUM impact calendar events (CPI, PPI, PCE, NFP, FOMC)

**Latest results (comprehensive backtest, 2026-03-27):** 673 trades · WR=45.9% · PF=1.22 (YF in-sample) · MaxDD=−$24,801
> **Note:** Sierra Charts OOS (small N=4 GC trades) showed PF=2.55. The YF in-sample figure of 1.22 is the larger, more reliable sample. MGC required; tracking ongoing.

---

### VWAP Pullback — NQ1!/ES1! 15m ✅ Active
**Chart:** NQ1! or ES1! · **Timeframe:** 15m · **Mode:** A — Trend Pullback

- Trades pullbacks to VWAP in the direction of the dominant intraday trend
- Long when price dips to VWAP and trend is bullish; short when price rallies to VWAP and trend is bearish
- Volume-weighted SD bands provided by `add_vwap_sd_bands()` in `src/execution/orderflow.py`
- Signal computed by `compute_vwap_signals()` in `src/strategy/vwap_signal.py`
- Volume profile context from `add_volume_profile_columns()` in `src/strategy/volume_profile.py`
- Config section: `strategy.vwap_pullback`
- SignalDirection enum values: `VWAP_PB_LONG`, `VWAP_PB_SHORT`
- Pine Script: `pine/vwap_pullback_strategy.pine`

---

### VWAP Mean Reversion — NQ1!/ES1!/MGC1!/MCL1! 30m ✅ Active
**Charts:** NQ1!, ES1!, MGC1!, MCL1! · **Timeframe:** 30m · **Mode:** B — Mean Reversion

- Fades price when it reaches VWAP ±2 SD bands, expecting reversion back toward VWAP
- Goes SHORT when price reaches VWAP +2SD (overbought extreme); LONG when price reaches VWAP −2SD (oversold extreme)
- Broader market coverage than Pullback mode — includes gold (MGC1!) and crude oil micro (MCL1!)
- Config section: `strategy.vwap_reversion`
- SignalDirection enum values: `VWAP_MR_LONG`, `VWAP_MR_SHORT`
- Pine Script: `pine/vwap_reversion_strategy.pine`

---

### Disabled / Parked Strategies

| Strategy | Status | Reason |
|----------|--------|--------|
| **ORB** — Opening Range Breakout | ❌ Disabled | Bar-close entry on 5m chart causes structural whipsaws; PF=0.95. Needs redesign. |
| **CL** — Crude Oil FHB | ❌ Disabled | Net loser at all tested parameter sets. EIA report Wednesdays are particularly disruptive. |
| **6E** — London Open | ❌ Parked | PF=0.575 across all tuning attempts. EUR/USD has been RANGING 43% of the test period — no directional edge available. Revisit when EUR re-enters a trending regime. |

---

### Filters Applied to All Active Strategies
- **HTF Bias**: Weekly + monthly EMA structure must align with trade direction
- **VIX Filter**: Blocks trades in extreme volatility (VIX thresholds in config)
- **EconCalendar**: Skips FOMC, NFP, CPI, ECB, and other HIGH/MEDIUM impact days
- **GreenLight Score**: Composite market health score gate (≥ 40 to trade)
- **Daily Hard Stop**: Auto-halts trading if daily loss exceeds $2,500

---

## VWAP Strategies

Two VWAP-based strategies were added on 2026-04-04. Both use volume-weighted average price bands computed by `add_vwap_sd_bands()` in `src/execution/orderflow.py` and volume profile context from `src/strategy/volume_profile.py`.

### Mode A — VWAP Pullback
- **Markets:** NQ1!, ES1!
- **Timeframe:** 15m
- **Logic:** Buys pullbacks to VWAP in an uptrend; sells rallies to VWAP in a downtrend. Requires price to approach VWAP from the correct side with trend alignment.
- **TradingView chart:** NQ1! 15m or ES1! 15m with `pine/vwap_pullback_strategy.pine`

### Mode B — VWAP Mean Reversion
- **Markets:** NQ1!, ES1!, MGC1!, MCL1!
- **Timeframe:** 30m
- **Logic:** Fades price when it reaches VWAP ±2 SD bands. Goes short at +2SD, long at −2SD, targeting VWAP as the mean-reversion destination.
- **TradingView chart:** NQ1!/ES1!/MGC1!/MCL1! 30m with `pine/vwap_reversion_strategy.pine`

### Running the VWAP Backtest

```bash
conda run -n algobot_env python scripts/run_vwap_backtest.py
```

> Results will include both Pullback and Reversion modes. PF is pending first full backtest run.

---

## 4. Backtest Results

### Current Strategy Status (as of 2026-04-04)

| Strategy | Version | Direction | Chart | TF | Status |
|----------|---------|-----------|-------|----|--------|
| FHB | v4 | **Long only** | NQ1! | 15m | ✅ Active |
| GC Mean Reversion | v3 | Both (fade) | MGC1! | 30m | ✅ Active (MGC only) |
| VWAP Pullback | v1 | Trend direction | NQ1!/ES1! | 15m | ✅ Active |
| VWAP Mean Reversion | v1 | Both (fade extremes) | NQ1!/ES1!/MGC1!/MCL1! | 30m | ✅ Active |
| ORB | v2 | — | NQ1! | 15m | ❌ Disabled |
| CL | v2 | — | CL1! | 15m | ❌ Disabled |
| 6E London Open | v1 | — | 6E1! | 15m | ❌ Parked |

---

### FHB — NQ1! 15m

**TradingView Backtest (2026-04-03, with commission + slippage):**

| Metric | Value |
|--------|-------|
| Profit Factor | **1.73** |
| Win Rate | **63.5%** |
| Total Trades | 105 |
| Max Drawdown | −$6,572 |
| Direction | Long only |
| Entry type | Market at bar close |

**Python Backtest (2026-03-27, Yahoo Finance, 2.4 years):**

| Metric | Value |
|--------|-------|
| Profit Factor | **2.87** |
| Win Rate | **63.5%** |
| Total Trades | 208 |
| Total Net P&L | +$60,757 |
| Max Drawdown | −$7,565 (−12.5% of equity) |
| Sharpe (daily) | 5.61 |

> **Why the gap (PF 1.73 TV vs 2.87 Python)?** TradingView fills at the next available bar tick. The Python engine models more precise fill timing. Live paper trading will tell us which is realistic.

**Monte Carlo Validation (10,000 iterations):**

| Metric | Value | Pass? |
|--------|-------|-------|
| P05 PF (worst 5th percentile) | 1.924 | ✅ > 1.5 |
| Median PF | 2.966 | ✅ |
| Ruin probability (−$6K limit) | 2.35% | ✅ < 10% |
| Walk-forward windows profitable | 6/7 (85.7%) | ✅ |
| Avg OOS/IS ratio | 1.723 | ✅ > 0.6 |
| Stress PF (wins −30%, losses +30%) | 1.546 | ✅ > 1.0 |

---

### GC — MGC1! 30m (Mean Reversion Fade)

**Python Backtest (2026-03-27, Yahoo Finance in-sample, 2.4 years):**

| Metric | Value |
|--------|-------|
| Profit Factor | **1.22** |
| Win Rate | **45.9%** |
| Total Trades | 673 |
| Total Net P&L | +$28,380 |
| Max Drawdown (full GC) | −$24,801 (41.7%) — **use MGC only** |
| Max Drawdown (MGC micro) | ~−$2,480 (~4%) |
| Sharpe (daily) | 1.00 |

> **Always use MGC (10 oz micro) — never GC (100 oz full).** MGC MaxDD is ~4% on a $50K account. GC MaxDD is ~41% and prohibited on TopStep $50K.

**Sierra Charts OOS (Mar 2025–Mar 2026, N=4 — too small to rely on):**
- 4 trades · WR=100% · All winners · P&L=+$3,088
- ⚠ Sample size too small for statistical confidence — use Python figures.

---

### Disabled Strategies

| Strategy | Last Tested | PF | Reason |
|----------|------------|-----|--------|
| ORB | 2026-03-27 | 0.95 | Bar-close entry structurally too late |
| CL | 2026-03-27 | <1.0 | Net loser all timeframes; EIA disruption |
| 6E London Open | 2026-03-27 | 0.58 | EUR/USD ranging 43% of period; no edge |
| FHB Short | 2026-04-03 | 0.918 | Net loser (103 trades TV backtest) |

---

### Sierra Charts Real-Data OOS Validation (Mar 2025–Mar 2026)

Run with `scripts/run_sc_backtest.py` — real front-month futures data, never seen during development.

| Strategy | Markets | Trades | Win% | PF | Total P&L |
|----------|---------|--------|------|-----|-----------|
| FHB | NQ, MNQ | 13 | 61.5% | 2.97 | +$353 |
| GC Rev | GC, MGC | 4 | 100% | — (all wins) | +$3,088 |
| CL | CL | 10 | 40% | 1.22 | +$187 |
| **Combined** | All | **42** | **57.1%** | **2.69** | **+$8,201** |

---

## 5. Prerequisites

### One-Time Setup

**A. Conda environment**
```bash
conda create -n algobot_env python=3.11
conda activate algobot_env
pip install -r requirements.txt
pip install fastapi uvicorn ib_insync bcrypt "python-jose[cryptography]"
```

**B. Cloudflared (Mac — required for TradingView webhook tunnel)**
```bash
brew install cloudflare/cloudflare/cloudflared
```
This is needed by `start_trading_day.sh` to create the public tunnel URL that TradingView sends webhooks to.

**C. TWS API configuration** (only needed for IBKR live/paper order mode, not required for TV paper mode)
```
TWS → Edit → Global Configuration → API → Settings
  Enable ActiveX and Socket Clients = ON
  Read-Only API                     = OFF
  Port                              = 7497  (paper) / 7496 (live)
  Trusted IP                        = 127.0.0.1
```

**D. Market data subscription** (in TWS — IBKR mode only)
```
TWS → Account → Settings → Market Data Subscriptions
  → Add: "US Futures (CME, CBOT, NYMEX, COMEX)"
```

**E. Dashboard password** (first time only)

macOS:
```zsh
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"
conda activate algobot_env
python scripts/setup_dashboard_auth.py
```

Windows (PowerShell):
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts/setup_dashboard_auth.py
```

---

## 6. Daily Startup — Step by Step

### macOS (TradingView webhook flow — no TWS required)

1. Open Terminal
2. Run: `bash scripts/start_trading_day.sh` (from the project directory)
3. Copy the printed TradingView webhook URL → paste into all 4 TV alerts
4. Open dashboard → Control Center → click **LAUNCH BOT**
5. Leave the terminal open — the bot runs until 4:00 PM ET automatically

### Windows (IBKR TWS flow)

1. Open TWS and log into your **paper trading** account
2. Make sure TWS is fully loaded and showing market data
3. Open a terminal (Git Bash, PowerShell, or Command Prompt)
4. Run the commands in **Section 7** below for your terminal type
5. Leave the terminal open — the bot runs until 4:00 PM ET automatically
6. Optionally open the dashboard in your browser: `http://127.0.0.1:8000`

> **Start before 09:30 ET** to catch both the ORB (09:45) and FHB (10:30) windows.
> If you start after a window has passed, that window is skipped for the day.

---

## 7. Starting from Any Terminal

> **IMPORTANT — path has spaces.**
> Always wrap the project path in double-quotes. The path is:
> - **Mac:** `"/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"`
> - **Windows:** `"C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"`

---

### macOS Terminal / zsh ✅ (Mac — recommended)

> The TradingView workflow (no IBKR required) is the standard Mac flow.
> Signals come in via Pine Script webhooks; TWS is not needed on Mac.

**Option A — Full daily startup (Cloudflare tunnel + dashboard, recommended)**

This is the one command to run every morning. It starts the Cloudflare tunnel, spins up the dashboard, prints the TradingView webhook URL to paste into your alerts, and keeps everything running until you press `Ctrl+C`.

```zsh
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"
bash scripts/start_trading_day.sh
```

After it prints the tunnel URL, update that URL in all 4 TradingView alerts, then go to the dashboard → Control Center → click **LAUNCH BOT**.

**Option B — Simple launcher (dashboard + paper trading, no tunnel)**

Use this if you don't need the Cloudflare tunnel (e.g., running locally only):

```zsh
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"
bash scripts/start_bot.sh
```

This starts the dashboard at `http://localhost:8000` and the TV paper trading loop together. Press `Ctrl+C` to stop both.

**Manual step-by-step (if you need to run components separately):**

```zsh
# 1. Navigate to project
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"

# 2. Activate environment
conda activate algobot_env

# 3. Start dashboard (Terminal window 1)
uvicorn dashboard.server:app --host 127.0.0.1 --port 8000

# 4. Open a second Terminal tab/window, start the bot
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"
conda activate algobot_env
python scripts/run_tv_paper_trading.py
```

**Open the dashboard in your browser:**
```zsh
open http://localhost:8000
```

**Watch the live logs:**
```zsh
# Dashboard server log
tail -f /tmp/algobot_server.log

# Cloudflare tunnel log
tail -f /tmp/cloudflared.log
```

**Set / change dashboard password (first time only):**
```zsh
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"
conda activate algobot_env
python scripts/setup_dashboard_auth.py
```

**Run backtests on Mac:**
```zsh
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"
conda activate algobot_env

# SC real-data backtest (recommended first)
python scripts/run_sc_backtest.py

# Comprehensive backtest (all strategies)
python scripts/run_comprehensive_backtest.py

# Generate dashboard data
python scripts/generate_dashboard_data.py
```

---

### PowerShell ✅ (Windows — recommended)

> **Rule:** Use `&` before any `.exe` path that contains spaces. Use `$env:PYTHONUTF8=1` to prevent encoding errors on Windows.

**Step 1 — navigate to the project:**
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
```

**Step 2 — set encoding (prevents Windows cp1252 errors):**
```powershell
$env:PYTHONUTF8 = "1"
```

**Start the dashboard** (keep this window open, then open browser):
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
```
Then open your browser to: **http://127.0.0.1:8000**

Or auto-open the browser in a second PowerShell window:
```powershell
Start-Process "http://127.0.0.1:8000"
```

**Start the bot** (open a second PowerShell window for this):
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" -u scripts\run_paper_trading.py
```

**Watch the live log:**
```powershell
Get-Content "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot\data\bot.log" -Wait
```

**Run the SC backtest from PowerShell:**
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_sc_backtest.py
```

---

### Command Prompt (cmd.exe)

> **Rule:** Use `cd /d` so Windows switches both drive and directory. Use `set PYTHONUTF8=1` for encoding.

**Start the dashboard:**
```cmd
cd /d "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
set PYTHONUTF8=1
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
```
Then open your browser to: **http://127.0.0.1:8000**

Auto-open browser in a second cmd window:
```cmd
start http://127.0.0.1:8000
```

**Start the bot** (new cmd window):
```cmd
cd /d "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
set PYTHONUTF8=1
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" -u scripts\run_paper_trading.py
```

**Watch the live log:**
```cmd
type "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot\data\bot.log"
```
_(cmd does not have a live `tail`. Use PowerShell `Get-Content -Wait` for live tailing.)_

---

### Git Bash

**Start the dashboard:**
```bash
cd "C:/Users/ghost/Documents/Claude Workflow/Trading/AlgoBot"
PYTHONUTF8=1 "C:/Users/ghost/miniconda3/envs/algobot_env/python.exe" -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
```

**Start the bot (background):**
```bash
cd "C:/Users/ghost/Documents/Claude Workflow/Trading/AlgoBot"
PYTHONUTF8=1 "C:/Users/ghost/miniconda3/envs/algobot_env/python.exe" -u scripts/run_paper_trading.py > data/bot.log 2>&1 &
echo "Bot started. PID: $!"
```

**Watch the live log:**
```bash
tail -f "C:/Users/ghost/Documents/Claude Workflow/Trading/AlgoBot/data/bot.log"
```

---

### Windows Task Scheduler (fully automated — every weekday 09:00 ET)

```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
powershell -ExecutionPolicy Bypass -File scripts\setup_task_scheduler.ps1
```

This registers a Windows task that starts the bot automatically every weekday morning.

---

## 8. Dashboard

The dashboard shows live bot status, daily P&L, open positions, backtest performance, and Sierra Charts real-data validation. Live data is pushed via WebSocket — no manual refresh needed.

**URL:** `http://localhost:8000`

**Tabs:** Overview · SC Validation · Control Center · Terminal · System Status

**Webhook endpoint** (receives TradingView signals): `POST /api/webhook/signal`

---

### Start the dashboard — PowerShell (recommended)

```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
```

Then in a second window, open the browser:
```powershell
Start-Process "http://127.0.0.1:8000"
```

### Start the dashboard — Command Prompt

```cmd
cd /d "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
set PYTHONUTF8=1
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
```

Then open browser:
```cmd
start http://127.0.0.1:8000
```

---

**Refresh historical backtest data** (regenerates dashboard charts):

PowerShell:
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\generate_dashboard_data.py
```

cmd:
```cmd
cd /d "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
set PYTHONUTF8=1
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\generate_dashboard_data.py
```

**Run SC real-data backtest** (updates SC Validation tab):

PowerShell:
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_sc_backtest.py
```

**Set / change dashboard password** (first-time setup):

PowerShell:
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\setup_dashboard_auth.py
```

---

## 9. Configuration

All strategy parameters live in `config/config.yaml`. Key sections:

```yaml
bot:
  webhook_secret: "CHANGE_ME_RANDOM_SECRET_HERE"  # Must match TradingView Pine inputs

tv_paper:
  tunnel_url: "https://xxxx.trycloudflare.com"    # Auto-written by start_trading_day.sh
  enabled_signals:
    FHB: true   # NQ/MNQ First Hour Breakout
    GC: true    # MGC Mean Reversion Fade
    ORB: false  # Disabled — bar-close entry broken
    CL: false   # Disabled — net loser

fhb:
  stop_atr_mult: 0.8            # ATR × 0.8 stop
  target_r: 2.5                 # 2.5R target
  breakeven_r: 1.0              # Slide stop to entry at +1R (no partial exit in v3)
  gls_min: 40                   # GreenLight Score gate

gc_reversion:
  stop_atr_mult: 1.0            # ATR × 1.0 stop (above breakout extreme)
  target_r: 1.0                 # 1.0R target (VWAP or range midpoint)
  gls_min: 70                   # Strict quality gate for GC

risk:
  daily_loss_alert_usd: 1500    # Warning threshold
  daily_loss_hard_stop_usd: 2500 # Hard stop — no more trades today
```

**Risk modes** (set in Control Center or Pine Script inputs):

| Mode | Risk/Trade | Max Contracts | Daily Cap | Notes |
|------|-----------|---------------|-----------|-------|
| Safe | 0.5% | 1 | $900 | TopStep $50K default |
| Medium | 1.0% | 3 | $2,500 | |
| Hardcore | 2.0% | 5 | $3,800 | Aggressive |

> Never edit config.yaml while the bot is running. Changes take effect on next startup.

---

## 10. Risk Controls

| Control | Value (Safe mode) | Behavior |
|---------|------------------|----------|
| Risk per trade | 0.5% | Max loss per trade ~$250 on $50K account |
| Max contracts | 1 | Never trades more than 1 contract at a time |
| Daily loss alert | $1,500 | Prints warning, continues trading |
| Daily hard stop | $2,500 | Cancels all orders, halts trading for the day |
| GreenLight Score | < 40 = skip | Below 40: no trade; 40–59: half size; ≥ 60: full size |
| FOMC/NFP/CPI/ECB | Skip | No trades on major announcement days |
| VIX filter | Configurable | Blocks trades in extreme volatility regimes |
| EOD flat | 16:05 ET | All open positions settled at last price — no overnight holds |

---

## 11. Running Backtests

Results are saved to `reports/backtests/`. SC results auto-update the dashboard SC Validation tab.

### macOS (zsh)

```zsh
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"
conda activate algobot_env
```

**Comprehensive 6-layer validation** (FHB + GC — recommended, ~3–5 min):
```zsh
python scripts/run_comprehensive_backtest.py
```

**Full statistical validation suite** (Monte Carlo + walk-forward, ~3 min):
```zsh
python scripts/run_validation_suite.py
```

**SC real-data OOS backtest** (Sierra Charts real futures data):
```zsh
python scripts/run_sc_backtest.py
```

**FHB strategy backtest only** (Yahoo Finance historical data, ~90s):
```zsh
python scripts/run_fhb_backtest.py
```

**Refresh dashboard charts** (~90s):
```zsh
python scripts/generate_dashboard_data.py
```

---

### Windows PowerShell

```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
$PYTHON = "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe"
```

**Comprehensive 6-layer validation:**
```powershell
& $PYTHON scripts\run_comprehensive_backtest.py
```

**Full validation suite:**
```powershell
& $PYTHON scripts\run_validation_suite.py
```

**SC real-data OOS backtest:**
```powershell
& $PYTHON scripts\run_sc_backtest.py
```

**FHB backtest:**
```powershell
& $PYTHON scripts\run_fhb_backtest.py
```

**Refresh dashboard charts:**
```powershell
& $PYTHON scripts\generate_dashboard_data.py
```

---

### Windows cmd.exe

```cmd
cd /d "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
set PYTHONUTF8=1
```

**Comprehensive validation:**
```cmd
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_comprehensive_backtest.py
```

**SC real-data OOS backtest:**
```cmd
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_sc_backtest.py
```

**FHB backtest:**
```cmd
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_fhb_backtest.py
```

---

## 12. Backup and Restore

**Create a backup** (trades DB + config):

macOS:
```zsh
conda activate algobot_env && python scripts/create_backup.py
```

Windows:
```powershell
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\create_backup.py
```

**Restore from backup:**

macOS:
```zsh
conda activate algobot_env && python scripts/restore_backup.py
```

Windows:
```powershell
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\restore_backup.py
```

---

## 13. Troubleshooting

### "Client ID already in use"
TWS has a stale connection from a previous session. In `scripts/run_paper_trading.py`, find `client_id=` and increment it by 1 (e.g., 22 → 23). TWS releases old IDs after a few minutes.

Alternatively, restart TWS — this clears all client IDs.

### Bot crashes with "ConnectionError: Socket disconnect"
TWS had a momentary connectivity blip. The bot now handles this automatically (fixed 2026-03-06). Just restart it.

### "Today is not a trading day"
The bot checks if today is Monday–Friday. If you run it on a weekend it exits immediately.

### Bot started after 10:30
```
NOTE: Started after FHB/GC window -- FHB + GC skipped for today
```
This is expected. The signal windows are gone for today. Start tomorrow before 09:15 ET.

### Dashboard shows "Bot not running" but bot is active
Run `generate_dashboard_data.py` to refresh the cache, or wait — the bot pushes state updates via WebSocket.

### No trades placed even though bot ran all day
Either:
- No setup formed (market didn't break the FHB range cleanly)
- HTF bias was NEUTRAL (bot only trades with the trend)
- A filter blocked the signal (VIX too high, economic event day, GLS score too low)
- TradingView alert webhook URL was stale (re-run `start_trading_day.sh` to get new URL)

This is normal and expected. The bot is selective by design.

### Tunnel URL not working / "Connection refused" in TradingView
cloudflared failed to start or isn't installed.
```zsh
# Check if cloudflared is installed
which cloudflared

# Install if missing
brew install cloudflare/cloudflare/cloudflared

# Check tunnel log
tail /tmp/cloudflared.log
```
After restarting the tunnel, update the webhook URL in all TradingView alerts.

### TradingView webhook not firing (no trades received)
Either:
1. Alert webhook URL is stale — re-run `start_trading_day.sh` and update all 4 alerts
2. Alert frequency is wrong — must be set to **"Once Per Bar Close"** (not "Once Per Bar")
3. Alert has expired — TradingView alerts expire after 1 month; recreate them

### Pine Script shows no signals / 0 trades in Strategy Tester
- FHB: chart must be **NQ1!** on **15m** timeframe (not 1H, not 5m)
- GC: chart must be **MGC1!** (Micro Gold) on **30m** timeframe (not GC1!)
- Wrong instrument (GC1! instead of MGC1!) is the most common mistake

### Check the live logs

macOS (live tail):
```zsh
tail -f /tmp/algobot_server.log
tail -f /tmp/cloudflared.log
```

PowerShell (live tail):
```powershell
Get-Content "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot\logs\bot_$(Get-Date -Format 'yyyy-MM-dd').log" -Wait
```

cmd (static dump — no live tail in cmd):
```cmd
type "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot\data\bot.log"
```
