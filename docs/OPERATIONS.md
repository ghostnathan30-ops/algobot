# AlgoBot — Operations Manual

> Last updated: 2026-03-19
> Version: Phase 6 + SC Validation (Paper Trading Ready)

---

## Table of Contents

1. [What This Bot Does](#1-what-this-bot-does)
2. [Project Structure](#2-project-structure)
3. [How the Strategies Work](#3-how-the-strategies-work)
4. [Backtest Results](#4-backtest-results)
5. [Prerequisites](#5-prerequisites)
6. [Daily Startup — Step by Step](#6-daily-startup--step-by-step)
7. [Starting from Any Terminal](#7-starting-from-any-terminal)
8. [Dashboard](#8-dashboard)
9. [Configuration](#9-configuration)
10. [Risk Controls](#10-risk-controls)
11. [Running Backtests](#11-running-backtests)
12. [Backup and Restore](#12-backup-and-restore)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. What This Bot Does

AlgoBot is a systematic futures trading bot that connects to Interactive Brokers (IBKR) TWS and places bracket orders automatically, based on two proven intraday setups on ES and NQ futures, plus a Gold (GC) mean-reversion sub-bot.

**It is NOT a continuous scanner.** It checks for setups at two specific times each morning, places orders if conditions are met, manages those orders through the day, and exits at 4:00 PM ET.

**Trading schedule (all times US Eastern):**

| Time  | Action |
|-------|--------|
| 09:00 | Start the bot (before market open) |
| 09:45 | ORB check — ES + NQ opening range breakout |
| 10:30 | FHB check — ES + NQ first-hour bar breakout + GC mean-reversion |
| 16:00 | Auto-cancel all open orders, print daily summary, exit |

---

## 2. Project Structure

```
AlgoBot/
├── config/
│   └── config.yaml              # All strategy parameters (single source of truth)
│
├── src/
│   ├── strategy/
│   │   ├── orb_signal.py        # Opening Range Breakout signal (ES, NQ)
│   │   ├── htf_bias.py          # Higher timeframe bias filter (weekly/monthly)
│   │   ├── gc_signal.py         # Gold mean-reversion signal
│   │   ├── london_open_signal.py# 6E London Open (parked — needs retune)
│   │   ├── signal_combiner.py   # Combines ORB + FHB signals
│   │   ├── indicators.py        # EMA, ATR, RSI, ADX, Donchian calculations
│   │   ├── regime_classifier.py # Market regime detection
│   │   └── position_sizer.py    # Risk-based position sizing
│   │
│   ├── execution/
│   │   ├── ibkr_bridge.py       # IBKR TWS connection + order submission
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
│   ├── run_paper_trading.py     # MAIN SCRIPT — run this every morning
│   ├── run_fhb_backtest.py      # FHB strategy backtest
│   ├── run_orb_backtest.py      # ORB strategy backtest
│   ├── run_gc_backtest.py       # GC mean-reversion backtest
│   ├── run_6e_backtest.py       # 6E London Open backtest
│   ├── run_combined_backtest.py # All strategies combined backtest
│   ├── run_signal_replay.py     # Replay historical signals for review
│   ├── generate_dashboard_data.py # Refresh dashboard JSON cache
│   ├── create_backup.py         # Backup trades DB + config
│   ├── restore_backup.py        # Restore from backup
│   ├── setup_dashboard_auth.py  # Set dashboard username/password
│   └── setup_task_scheduler.ps1 # Windows Task Scheduler automation
│
├── dashboard/
│   ├── server.py                # FastAPI dashboard server
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
│   └── validation/              # Walk-forward validation reports
│
├── docs/                        # All documentation
├── logs/                        # Rotating application logs
├── config/config.yaml           # Strategy configuration
├── requirements.txt             # Python dependencies
└── .env                         # IBKR credentials (never commit this)
```

---

## 3. How the Strategies Work

### ORB — Opening Range Breakout (09:45 ET)
- Measures the high/low of the first 30 minutes of trading (09:30–10:00)
- At 09:45, checks if price has broken above (LONG) or below (SHORT) that range
- HTF bias filter: only takes longs in bull trends, shorts in bear trends
- Filters: VIX level, economic calendar (FOMC/NFP/CPI days skipped), GreenLight score

### FHB — First Hour Bar Breakout (10:30 ET)
- Uses the 09:30–10:30 hourly candle as the setup bar
- At 10:30, checks if price is breaking out of that bar's high or low
- Same filters as ORB plus additional institutional filters
- Partial exit at 1R, trail remainder

### GC — Gold Mean Reversion (10:30 ET)
- Gold (GC futures) trends strongly — this bot fades short-term pullbacks in the direction of the weekly trend
- Inverts the standard FHB signal logic (buys dips in uptrends, not breakouts)
- HTF bias required to be BULL for longs, BEAR for shorts
- Backtest results: PF=1.73, Win%=49%, avg win $591 vs avg loss $329

### Filters Applied to All Strategies
- **HTF Bias**: Weekly + monthly EMA structure must align with trade direction
- **VIX Filter**: Blocks trades when volatility is extreme (VIX > threshold in config)
- **EconCalendar**: Skips FOMC, NFP, CPI, ECB announcement days
- **GreenLight Score**: Composite market health score must meet minimum threshold
- **Daily Hard Stop**: Auto-halts trading if daily loss exceeds $2,500

---

## 4. Backtest Results

### Historical (Yahoo Finance in-sample, 2022–2026)

| Strategy | Markets | Period | Trades | Win% | Profit Factor | Max Drawdown |
|----------|---------|--------|--------|------|--------------|--------------|
| ORB | ES + NQ | 2022–2026 | ~280 | 62.6% | 2.19 | -$8k |
| FHB | ES + NQ | 2022–2026 | ~180 | 63.1% | 2.31 | -$6k |
| GC Rev | GC | 2023–2026 | 51 | 49% | 1.73 | -$4k |
| 6E LON | 6E | 2023–2026 | — | 33–39% | 0.58 | — |

> **6E is parked.** EUR/USD ranged 43% of the test period with no edge. Will revisit with 5-year IB data.

### Sierra Charts Real-Data OOS Validation (Nov 2025 – Mar 2026)

| Strategy | Markets | Days | Trades | Win% | Profit Factor | Total P&L | Max DD |
|----------|---------|------|--------|------|--------------|-----------|--------|
| FHB | NQ, MNQ, GC, MGC, CL | 18 | 27 | 59.3% | 2.97 | $3,629 | -$957 |
| ORB | NQ | 14 | 15 | 53.3% | 2.51 | $4,573 | -$1,243 |
| **Combined** | All | **25** | **42** | **57.1%** | **2.69** | **$8,201** | **-$1,874** |

> **6/6 checks PASSED.** Edge confirmed on real Sierra Charts futures data. Ready for TopStep paper trading.
> TopStep $50k projections: **+$6,889/month** · **+165% annualised** — see SC Validation tab in dashboard.

---

## 5. Prerequisites

### One-Time Setup

**A. Conda environment**
```bash
conda create -n algobot_env python=3.11
conda activate algobot_env
pip install -r requirements.txt
```

**B. TWS API configuration** (do this once in TWS)
```
TWS → Edit → Global Configuration → API → Settings
  Enable ActiveX and Socket Clients = ON
  Read-Only API                     = OFF
  Port                              = 7497  (paper) / 7496 (live)
  Trusted IP                        = 127.0.0.1
```

**C. Market data subscription** (in TWS)
```
TWS → Account → Settings → Market Data Subscriptions
  → Add: "US Futures (CME, CBOT, NYMEX, COMEX)"
```

**D. Dashboard password** (first time only)
```bash
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts/setup_dashboard_auth.py
```

---

## 6. Daily Startup — Step by Step

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
> `"C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"`
> PowerShell also requires the `&` call operator before any executable path that contains spaces.

---

### PowerShell ✅ (recommended)

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

The dashboard shows live bot status, daily P&L, open positions, backtest performance, and Sierra Charts real-data validation.

**URL:** `http://127.0.0.1:8000`

**Tabs:** Overview · SC Validation · Control Center · Terminal · System Status

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
risk:
  risk_pct: 0.005              # 0.5% of account per trade
  daily_loss_alert_usd: 1500   # Warning threshold
  daily_loss_hard_stop_usd: 2500 # Hard stop — no more trades today

orb:
  lookback_bars: 6             # 30-minute opening range (6 x 5-min bars)
  atr_multiplier_stop: 1.0
  target_r: 2.0                # 2R target

fhb:
  target_r: 2.0
  partial_exit_r: 1.0          # Take partial profit at 1R

gc_reversion:
  partial_exit_r: 0.3          # GC partial exit (tuned)
  target_r: 2.0
```

> Never edit config.yaml while the bot is running. Changes take effect on next startup.

---

## 10. Risk Controls

| Control | Value | Behavior |
|---------|-------|----------|
| Risk per trade | 0.5% | Max loss per trade ~$500 on $100k account |
| Max contracts | 1 | Never trades more than 1 contract at a time |
| Daily loss alert | $1,500 | Prints warning, continues trading |
| Daily hard stop | $2,500 | Cancels all orders, halts trading for the day |
| FOMC/NFP/CPI/ECB | Skip | No trades on major announcement days |
| VIX filter | Configurable | Blocks trades in extreme volatility |

---

## 11. Running Backtests

Always `cd` to the project root first. Use `$env:PYTHONUTF8 = "1"` (PowerShell) or `set PYTHONUTF8=1` (cmd) to prevent encoding errors.

### PowerShell — all backtest commands

```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
```

**SC real-data backtest** (Sierra Charts — OOS validation, recommended first):
```powershell
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_sc_backtest.py
```

**FHB backtest (historical Yahoo data):**
```powershell
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_fhb_backtest.py
```

**ORB backtest:**
```powershell
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_orb_backtest.py
```

**GC mean-reversion backtest:**
```powershell
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_gc_backtest.py
```

**Combined backtest (all strategies):**
```powershell
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_combined_backtest.py
```

### cmd.exe — all backtest commands

```cmd
cd /d "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
set PYTHONUTF8=1
```

**SC real-data backtest:**
```cmd
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_sc_backtest.py
```

**FHB backtest:**
```cmd
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_fhb_backtest.py
```

**ORB backtest:**
```cmd
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_orb_backtest.py
```

**Combined backtest:**
```cmd
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_combined_backtest.py
```

Results are saved to `reports/backtests/`. SC results also update the dashboard SC Validation tab automatically.

---

## 12. Backup and Restore

**Create a backup** (trades DB + config):
```bash
"C:/Users/ghost/miniconda3/envs/algobot_env/python.exe" scripts/create_backup.py
```

**Restore from backup:**
```bash
"C:/Users/ghost/miniconda3/envs/algobot_env/python.exe" scripts/restore_backup.py
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

### Bot started after 09:45 or 10:30
```
NOTE: Started after ORB window -- ORB skipped for today
NOTE: Started after FHB/GC window -- FHB + GC skipped for today
```
This is expected. Those windows are gone for today. Start tomorrow before 09:30 ET.

### Dashboard shows "Bot not running" but bot is active
Run `generate_dashboard_data.py` to refresh the cache, or wait — the bot pushes state updates every 15 loop ticks (~5 minutes).

### No trades placed even though bot ran all day
Either:
- No setup formed (market didn't break the ORB/FHB level cleanly)
- HTF bias was NEUTRAL (bot only trades with the trend)
- A filter blocked the signal (VIX too high, economic event day, GLS score too low)

This is normal and expected. The bot is selective by design.

### Check the live log

PowerShell (live tail):
```powershell
Get-Content "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot\data\bot.log" -Wait
```

Git Bash (live tail):
```bash
tail -f "C:/Users/ghost/Documents/Claude Workflow/Trading/AlgoBot/data/bot.log"
```

cmd (static dump — no live tail in cmd):
```cmd
type "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot\data\bot.log"
```
