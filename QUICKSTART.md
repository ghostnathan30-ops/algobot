# AlgoBot — Quick Start Guide
**Version 4.0 · TradingView Paper Trading (NQ + MGC)**
Last updated: 2026-04-03

---

## What This Is

AlgoBot uses **TradingView Pine Scripts** to detect signals and send them via webhook to a local bot server. The bot simulates trades (paper mode) and tracks P&L. No Interactive Brokers connection is needed for paper trading.

**Four active strategies:**
- **FHB v4** — NQ1! 15m, Long only, fires 10:30–12:00 ET (PF=1.73 TV / 2.87 Python)
- **GC v3** — MGC1! 30m, Mean-reversion fade, fires 10:30–13:00 ET (PF=1.22 Python)
- **VWAP Pullback v1** — NQ1!/ES1! 15m, Trend direction, PF pending backtest
- **VWAP Mean Reversion v1** — NQ1!/ES1!/MGC1!/MCL1! 30m, Both (fade extremes), PF pending backtest

---

## Prerequisites

### 1. Conda Environment
```bash
conda env list | grep algobot      # verify environment exists
conda activate algobot_env
```

### 2. TradingView Setup
- **Chart 1:** NQ1! on 15m → add `pine/fhb_strategy.pine` as strategy
- **Chart 2:** MGC1! on 30m → add `pine/gc_strategy.pine` as strategy
- See [`docs/TRADINGVIEW_SETUP.md`](docs/TRADINGVIEW_SETUP.md) for full alert setup

### 3. Cloudflare Tunnel (for TradingView webhooks)
TradingView needs a public URL to send webhooks. Use cloudflared:
```bash
# One-time install (Mac)
brew install cloudflare/cloudflare/cloudflared

# Start tunnel (do this each morning before 09:15 ET)
cloudflared tunnel --url http://localhost:8000
# → copies output URL like https://abc-def.trycloudflare.com
```
Paste that URL into your TradingView alert webhook fields.

---

## Starting the Bot

### Full Daily Start (Mac — recommended)
```bash
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"
bash scripts/start_trading_day.sh
```
This opens a new terminal for the cloudflared tunnel and starts the dashboard at `http://localhost:8000`.

### Start Components Separately

**Dashboard only:**
```bash
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"
PYTHONUTF8=1 conda run -n algobot_env uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
```

**First-time dashboard login setup:**
```bash
conda run -n algobot_env python scripts/setup_dashboard_auth.py
```

**Generate dashboard data (backtest cache):**
```bash
PYTHONUTF8=1 conda run -n algobot_env python scripts/generate_dashboard_data.py
# Takes ~90 seconds. Run before opening dashboard.
```

**TV paper trading loop:**
```bash
PYTHONUTF8=1 conda run -n algobot_env python scripts/run_tv_paper_trading.py
```

---

## Trading Schedule (US Eastern Time)

| Time | Action |
|------|--------|
| Before 09:15 | Run `start_trading_day.sh` — starts tunnel + dashboard |
| 09:15 | Copy tunnel URL → paste into TradingView alert webhook URLs |
| 09:30–10:30 | Opening range captured by Pine Scripts |
| 10:30–12:00 | **FHB signal window** — NQ1! 15m |
| 10:30–13:00 | **GC signal window** — MGC1! 30m |
| 16:05 | EOD: bot settles all open positions at last price |

> **Important:** The tunnel URL changes each session. Update both TradingView alert webhook URLs before each trading day.

---

## Dashboard Features

| Tab | What It Shows |
|-----|---------------|
| **Overview** | Equity curve, KPIs, monthly heatmap, recent trades |
| **Control Center** | Start/stop bot, risk mode, strategy filters, position sizing |
| **Terminal** | Run backtests live, stream output to browser |
| **System Status** | Tunnel status, cache state, contract expiry alerts |

**Control Center lets you:**
- Click **▶ Start** to launch the paper trading loop
- Click **■ Stop** to terminate gracefully
- Change **Risk Mode**: Safe (1ct) · Medium (3ct) · Hardcore (5ct)
- Toggle filters: VIX, Econ calendar, GLS gate, VWAP, delta

---

## Backtest Commands

```bash
cd "/Users/nathanmihindu/Documents/Claude Workflow/Trading/AlgoBot"
PYTHON="conda run -n algobot_env python"

# FHB backtest (NQ, 2.4 years) ~90s
$PYTHON scripts/run_fhb_backtest.py

# Combined 6-layer validation ~2min
$PYTHON scripts/run_comprehensive_backtest.py

# Sierra Charts OOS validation (real futures data)
$PYTHON scripts/run_sc_backtest.py

# VWAP Pullback + Mean Reversion backtest (both modes)
conda run -n algobot_env python scripts/run_vwap_backtest.py
```

---

## Current Performance (as of 2026-04-03)

| Strategy | WR | PF (TV) | PF (Python) | Trades | Sample |
|----------|----|---------|-------------|--------|--------|
| FHB (NQ1!, Long Only) | 63.5% | **1.73** | 2.87 | 105 (TV) / 208 (Python) | 2.4 years |
| GC Mean Reversion (MGC1!) | 45.9% | — | 1.22 | 673 | 2.4 years |
| VWAP Pullback (NQ1!/ES1!) | — | pending | pending | — | backtest not yet run |
| VWAP Mean Reversion (NQ1!/ES1!/MGC1!/MCL1!) | — | pending | pending | — | backtest not yet run |

**Targets:** Win Rate > 60%, Profit Factor ≥ 2.0 (Python), ≥ 1.5 (TV after costs)

---

## Risk Controls (Safe Mode — $50K Account)

| Control | Limit | Why |
|---------|-------|-----|
| Daily loss cap | $900 | TopStep $50K limit = $1,000 |
| Trailing drawdown | $1,800 | TopStep $50K limit = $2,000 |
| Per-trade max loss | $400 | ~0.8% of $50K |
| Max contracts | 1 (NQ) or 1 (MGC) | Safe for $50K |
| VIX crisis filter | Skip if VIX > 35 | Avoid black swan entries |

---

## Contract Expiry — Quarterly Roll

| Market | Current Active | Next Roll |
|--------|---------------|-----------|
| NQ / ES | 202606 (Jun 2026) | ~Jun 12, 2026 → 202609 |
| MGC / GC | 202606 (Jun 2026) | ~Jun 12, 2026 → 202609 |
| CL | 202505 (May 2026) | ~Apr 20, 2026 → 202506 |

To update: edit `CONTRACT_EXPIRY` dict in `src/execution/ibkr_bridge.py`

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No webhook received | Tunnel URL changed — re-run `start_trading_day.sh`, update TradingView alert |
| Dashboard shows no trades | Run `generate_dashboard_data.py` first |
| "Module not found" | Run `conda activate algobot_env` first |
| Strategy not firing | Check TradingView chart — is market correct (NQ1! or MGC1!)? Is TF set correctly? |
| Wrong fills | Check `logs/webhook_signals.jsonl` for raw webhook payloads |
| Dashboard login fails | Run `scripts/setup_dashboard_auth.py` to set credentials |

---

## Key Files

| File | Purpose |
|------|---------|
| `scripts/start_trading_day.sh` | **Main launcher** — tunnel + dashboard |
| `scripts/run_tv_paper_trading.py` | TV paper trading loop (receives webhooks) |
| `scripts/generate_dashboard_data.py` | Regenerate dashboard charts |
| `config/config.yaml` | All strategy parameters (single source of truth) |
| `pine/fhb_strategy.pine` | FHB Pine Script v4 (NQ1! 15m, Long only) |
| `pine/gc_strategy.pine` | GC Pine Script v3 (MGC1! 30m) |
| `src/execution/paper_simulator.py` | Paper trade simulator (yfinance-based) |
| `dashboard/server.py` | FastAPI web server |
| `dashboard/cache/bot_state.json` | Live risk mode + daily P&L state |
| `data/trades.db` | SQLite trade log (full audit trail) |
| `logs/webhook_signals.jsonl` | All received TradingView webhooks |
