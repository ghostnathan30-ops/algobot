# AlgoBot — Quick Start Guide
**Version 2.1 · Paper Trading (ES + NQ + GC)**
Last updated: 2026-03-08

---

## Prerequisites

### 1. TWS (Trader Workstation) — Paper Mode
- Download: https://www.ibkr.com/en/trading/tws
- Log in with your **paper trading** credentials (separate login from live)
- Enable API access:
  ```
  TWS → Edit → Global Configuration → API → Settings
    ✔ Enable ActiveX and Socket Clients  →  ON
    ✔ Read-Only API                      →  OFF
    ✔ Socket port                        →  7497
    ✔ Trusted IP Addresses               →  127.0.0.1
  ```
- Click **OK**, restart TWS if prompted

### 2. Market Data (free with paper account)
```
TWS → Account → Market Data Subscriptions
  → Add "US Futures (CME, CBOT, NYMEX, COMEX)"
```

### 3. Conda Environment
- Requires: `algobot_env` conda environment
- Verify it exists: `conda env list | findstr algobot`
- Python executable: `C:\Users\ghost\miniconda3\envs\algobot_env\python.exe`

---

## Starting the Bot (Recommended — PowerShell)

Open **PowerShell** (not cmd) and run:

```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
.\scripts\start_bot.ps1
```

This script:
1. Opens a new PowerShell window running the dashboard server at `http://localhost:8000`
2. Starts the paper trading loop in the current window (logs to `logs\bot_YYYY-MM-DD.log`)
3. The bot runs until 4:00 PM ET then exits cleanly

> **Important:** Start the bot **after 9:00 AM ET** on a US trading day.

---

## Starting Components Separately

### Dashboard only
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
```
Open: http://localhost:8000

### First-time dashboard login setup
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\setup_dashboard_auth.py
```

### Generate dashboard data (backtest cache)
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\generate_dashboard_data.py
```
Takes ~90 seconds. Run this before opening the dashboard to populate charts.

### Paper trading loop only
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_paper_trading.py
```

---

## Trading Schedule (US Eastern Time)

| Time  | Action |
|-------|--------|
| 09:00 | Bot starts, connects to IBKR, loads HTF bias data |
| 09:45 | ORB signal check (ES + NQ — 15-min opening range) |
| 10:30 | FHB signal check (ES + NQ — first-hour breakout) |
| 10:30 | GC mean-reversion check (Gold) |
| 16:00 | EOD: cancel open orders, print summary, exit |

---

## Dashboard Features

| Tab | What it shows |
|-----|---------------|
| **Overview** | Equity curve, KPIs, monthly heatmap, recent trades |
| **Control Center** | Start/stop bot, risk mode, strategy filters, position sizing |
| **Terminal** | Run backtests live, stream output to browser |
| **System Status** | TWS connection, cache state, contract expiry alerts |

**Control Center** lets you:
- Click **▶ Start** to launch the trading bot (requires TWS open)
- Click **■ Stop** to terminate gracefully
- Change **Risk Mode**: Safe (1ct) · Medium (3ct) · Hardcore (5ct)
- Toggle strategy filters (VIX, Econ calendar, GLS gate, VWAP, etc.)

---

## Backtest Commands

```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$env:PYTHONUTF8 = "1"
$PYTHON = "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe"

# FHB backtest (ES + NQ, 603 days)  ~90s
& $PYTHON scripts\run_fhb_backtest.py

# ORB backtest (~30s)
& $PYTHON scripts\run_orb_backtest.py

# Combined ORB + FHB + Swing (~2min)
& $PYTHON scripts\run_combined_backtest.py

# GC mean-reversion backtest
& $PYTHON scripts\run_gc_backtest.py

# Swing strategy backtest
& $PYTHON scripts\run_swing_backtest.py
```

---

## Current Performance (as of 2026-03-08)

| Strategy | Win Rate | Profit Factor | Trades/Day | Sample |
|----------|----------|---------------|------------|--------|
| FHB (ES+NQ) | 63.9% | 2.63 | 1.52/day | 604 days |
| ORB (ES+NQ) | 72.5% | 3.37 | 1.60/day | 50 days |
| Combined ORB+FHB | ~65% | ~2.7 | 3.1/day | 604 days |
| GC Mean Reversion | 49% | 1.73 | 0.08/day | 2 yrs |
| Swing (OOS) | 57.1% | 1.61 | 0.12/day | 5 years |

**Targets: Win Rate > 60%, Profit Factor ≥ 2.0**

---

## Contract Expiry — Quarterly Roll

Current active contracts (updated 2026-03-08):

| Market | Expiry | Next Roll |
|--------|--------|-----------|
| ES / NQ | **202606** (Jun 2026) | ~Jun 12, 2026 → update to 202609 |
| GC / 6E | **202606** (Jun 2026) | ~Jun 12, 2026 → update to 202609 |
| CL | **202605** (May 2026) | ~Apr 20, 2026 → update to 202606 |
| ZB | **202606** (Jun 2026) | ~Jun 12, 2026 → update to 202609 |

To update: edit `CONTRACT_EXPIRY` dict in `src/execution/ibkr_bridge.py`

---

## Daily Automation (Windows Task Scheduler)

```powershell
# Run as Administrator — sets up a weekday 9:00 AM ET task
powershell -ExecutionPolicy Bypass -File "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot\scripts\setup_task_scheduler.ps1"
```

Or use the batch file directly in Task Scheduler:
```
Action: Start a program
Program: C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot\scripts\run_bot_daily.bat
Start in: C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot
```

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Could not connect to TWS" | TWS must be open, API enabled on port 7497, 127.0.0.1 trusted |
| "No market data" | Subscribe to US Futures in TWS → Account → Market Data |
| Garbled text in log | Use `$env:PYTHONUTF8='1'` in PowerShell before running |
| Wrong contract month | Update `CONTRACT_EXPIRY` in `src/execution/ibkr_bridge.py` |
| Dashboard shows no trades | Run `generate_dashboard_data.py` first, or use Terminal tab → Signal Replay |
| Bot starts then exits | Check `logs\bot_YYYY-MM-DD.log` for the error message |
| "clientId already in use" | TWS had a stale session — bot auto-rotates IDs (22→23→24→25) |
| Dashboard login fails | Run `scripts\setup_dashboard_auth.py` to create credentials |

---

## File Reference

| File | Purpose |
|------|---------|
| `scripts\start_bot.ps1` | **Main launcher** — starts dashboard + trading loop |
| `scripts\run_paper_trading.py` | Paper trading loop (core logic) |
| `scripts\generate_dashboard_data.py` | Regenerates dashboard charts from backtests |
| `scripts\run_bot_daily.bat` | Batch launcher for Task Scheduler |
| `config\config.yaml` | All strategy parameters (single source of truth) |
| `src\execution\ibkr_bridge.py` | IBKR order submission, contract expiry dict |
| `src\execution\live_signal_engine.py` | Live ORB/FHB/GC signal generation |
| `dashboard\server.py` | FastAPI web server |
| `dashboard\cache\trades.json` | Generated chart data |
| `dashboard\cache\bot_state.json` | Live risk mode + daily P&L state |
| `logs\bot_YYYY-MM-DD.log` | Daily trading log |
