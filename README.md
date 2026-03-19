# AlgoBot

Systematic futures trading bot for ES, NQ, and GC futures via Interactive Brokers TWS.

## Overview

AlgoBot runs two intraday breakout strategies (ORB + FHB) on ES/NQ futures and a Gold mean-reversion sub-bot (GC), with institutional-grade filters for trend, volatility, and economic events.

| Strategy | Markets | Win% | Profit Factor |
|----------|---------|------|--------------|
| ORB — Opening Range Breakout | ES, NQ | 62.6% | 2.19 |
| FHB — First Hour Bar Breakout | ES, NQ | 63.1% | 2.31 |
| GC — Gold Mean Reversion | GC | 49% | 1.73 |

**Trading windows:** 09:45 ET (ORB) and 10:30 ET (FHB + GC). Start before 09:30 ET.

## Quick Start

1. Open TWS (paper trading account)
2. Open a terminal and run:

**Git Bash:**
```bash
cd "C:/Users/ghost/Documents/Claude Workflow/Trading/AlgoBot"
"C:/Users/ghost/miniconda3/envs/algobot_env/python.exe" -u scripts/run_paper_trading.py > data/bot.log 2>&1 &
```

**PowerShell:**
```powershell
cd "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
& "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" -u scripts\run_paper_trading.py
```

3. Open the dashboard: `http://127.0.0.1:8000`

## Full Documentation

See [`docs/OPERATIONS.md`](docs/OPERATIONS.md) for complete instructions including:
- How each strategy works
- Starting from Git Bash, PowerShell, or Command Prompt
- TWS API setup
- Configuration reference
- Risk controls
- Backtesting
- Troubleshooting

## Requirements

- Python 3.11 (conda env: `algobot_env`)
- Interactive Brokers TWS with API enabled on port 7497 (paper)
- US Futures market data subscription in TWS
