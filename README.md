# AlgoBot — v4

Systematic futures trading bot for NQ (Nasdaq 100) and MGC (Micro Gold) via TradingView webhook signals.

## Active Strategies

| Strategy | Chart | TF | Direction | WR | PF (TV) | PF (Python) | Status |
|----------|-------|----|-----------|----|---------|-------------|--------|
| **FHB — First Hour Breakout** | NQ1! | 15m | Long only | 63.5% | 1.73 | 2.87 | ✅ ACTIVE |
| **GC — Mean Reversion** | MGC1! | 30m | Both | 45.9% | — | 1.22 | ✅ ACTIVE (MGC only) |
| ORB — Opening Range Breakout | NQ1! | 15m | — | — | 0.95 | — | ❌ DISABLED |
| CL — Crude Oil FHB | CL1! | 15m | — | — | <1.0 | — | ❌ DISABLED |

**FHB v4 changes (2026-04-03):** Short side disabled (PF=0.918, net loser). Pullback/limit entry disabled (drops WR from 63.5% → 22.97%). Long only, market entry at bar close.

## Trading Windows (US Eastern Time)

| Time | Action |
|------|--------|
| Before 09:15 | Start dashboard + cloudflared tunnel |
| 09:15 | Paste new tunnel URL into TradingView alert webhook fields |
| 09:30–10:30 | Pine Scripts capture opening range |
| 10:30–12:00 | FHB signal window (NQ1! 15m) |
| 10:30–13:00 | GC signal window (MGC1! 30m) |
| 16:05 | EOD — bot settles all positions |

## Quick Start

```bash
git clone https://github.com/ghostnathan30/algobot.git
cd algobot
bash scripts/start_trading_day.sh
```

This starts the cloudflared tunnel + dashboard. Copy the tunnel URL into your TradingView alert webhook URLs before 09:15 ET.

Dashboard: `http://localhost:8000`

## Performance (as of 2026-04-03)

| Metric | FHB (NQ1!) | GC (MGC1!) |
|--------|-----------|-----------|
| TV Backtest PF | 1.73 | — |
| Python Backtest PF | 2.87 | 1.22 |
| Win Rate | 63.5% | 45.9% |
| Max Drawdown | -$6,572 | -$24,801 (GC full) |
| Sample Size | 105 trades (TV) / 208 (Python) | 673 trades |
| Data Period | 2.4 years | 2.4 years |

**Always use MGC (micro, 10 oz) for gold — full GC (100 oz) has ~41% MaxDD on $50K.**

## Full Documentation

- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — Complete operations manual
- [`docs/TRADINGVIEW_SETUP.md`](docs/TRADINGVIEW_SETUP.md) — TradingView alert setup
- [`QUICKSTART.md`](QUICKSTART.md) — Quick start guide
- [`docs/EDGE_VALIDATION.md`](docs/EDGE_VALIDATION.md) — How the edge was validated

## Requirements

- Python 3.11+ (conda env: `algobot_env`)
- TradingView Pro (for alerts + webhook)
- Cloudflare account (for tunnel to receive webhooks)
- Optional: Interactive Brokers TWS on port 7497 (paper or live)
