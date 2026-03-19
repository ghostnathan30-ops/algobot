# LAB_011 — Performance Dashboard
**Phase**: 5F
**Date**: 2026-02-28
**Status**: COMPLETE

---

## Objective

Build a professional-grade, secured performance dashboard accessible at `http://localhost:8000`.
Requirements:
- Real-time (cached) performance metrics from ORB + FHB backtests
- Premium dark UI with Apple-quality animations and Plotly charts
- Full security: JWT auth, bcrypt passwords, security headers, localhost-only binding
- Ability to trigger data regeneration from the browser
- Persistent login session (10 hours)

---

## Architecture

```
Browser
  └─► http://localhost:8000 (127.0.0.1 only)
        ├── GET /            → index.html (requires JWT cookie)
        ├── GET /login       → login.html (public)
        ├── POST /auth/login → validates credentials, sets httpOnly cookie
        ├── POST /auth/logout → clears cookie, redirects to /login
        └── GET /api/*       → JSON data endpoints (require JWT cookie)
              ├── /api/summary      → all performance metrics
              ├── /api/equity       → daily equity curve + drawdown
              ├── /api/monthly      → monthly P&L heatmap matrix
              ├── /api/daily        → raw daily P&L
              ├── /api/by_strategy  → per-strategy breakdown
              ├── /api/by_market    → per-market breakdown
              ├── /api/trades       → trade log (filterable, paginated)
              ├── /api/distribution → P&L histogram data
              ├── /api/run_backtest → trigger async data regeneration
              └── /api/backtest_status → is regeneration running?
```

---

## Files Created / Modified

| File | Purpose |
|------|---------|
| `dashboard/server.py` | FastAPI backend — auth middleware, security headers, all API routes |
| `dashboard/auth.py` | JWT token creation/verification + bcrypt password checking |
| `dashboard/static/index.html` | Premium dark dashboard UI with Plotly charts |
| `dashboard/static/login.html` | Animated login page |
| `dashboard/config/auth.json` | Credentials (bcrypt hash + JWT secret) — auto-created, never committed |
| `dashboard/cache/trades.json` | Pre-generated trade data cache |
| `scripts/generate_dashboard_data.py` | Runs ORB+FHB replay, exports to cache |
| `scripts/setup_dashboard_auth.py` | Interactive one-time credential setup |
| `scripts/_create_default_auth.py` | Non-interactive bootstrap (creates admin/AlgoBot2026 defaults) |

---

## Security Implementation

### Authentication Flow
1. User navigates to `/` → middleware checks for `algobot_token` cookie
2. No valid token → redirect to `/login`
3. User submits login form → `POST /auth/login` (form data, URL-encoded)
4. Server validates username + bcrypt hash → creates JWT → sets httpOnly cookie
5. Subsequent requests include cookie automatically → middleware extracts + verifies JWT
6. Token expires after 10 hours → automatic redirect to login

### Security Headers Applied to All Responses
```
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.plot.ly ...
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
X-XSS-Protection: 1; mode=block
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: geolocation=(), microphone=(), camera=()
Cache-Control: no-store
```

### Network Isolation
- Server binds to `127.0.0.1` only (not `0.0.0.0`) — never reachable from other machines
- CORS: only `http://localhost:8000` and `http://127.0.0.1:8000` allowed
- Swagger/ReDoc UI disabled (`docs_url=None`, `redoc_url=None`)

### Credential Storage
- Passwords stored as bcrypt hash (cost=12) — never stored in plain text
- JWT secret key: 384-bit random (`secrets.token_hex(48)`)
- Both stored in `dashboard/config/auth.json` (excluded from git via .gitignore)

---

## Dashboard UI Features

### Header Bar
- AlgoBot PRO logo with gradient mark
- Data period label (from cache metadata)
- Status pill (green "LIVE" dot pulsing)
- **▶ Run Backtest** button — triggers `POST /api/run_backtest`
- Refresh button
- Sign Out button

### KPI Cards (6, animated)
| Metric | Value |
|--------|-------|
| Total P&L | $103,302 |
| Win Rate | 57.0% |
| Profit Factor | 1.49 |
| Sharpe Ratio | 2.78 |
| Max Drawdown | -$14,268 (-13.8%) |
| Avg Daily P&L | $399/day |

### Secondary Stats Row (8 metrics)
Sortino, Calmar, Best Trade, Worst Trade, Best Day, Worst Day, Total Trades, Trading Days

### Charts (Plotly.js, dark theme)
1. **Equity Curve** — cumulative P&L with gradient fill, range selector (3M/6M/1Y/ALL)
2. **Drawdown** — red fill, % basis, synced to equity dates
3. **Monthly P&L Heatmap** — red/green matrix by year × month
4. **Strategy Breakdown** — horizontal bar chart (ORB vs FHB)
5. **Market Breakdown** — pie chart (ES vs NQ)
6. **Daily P&L** — bar chart (green positive, red negative)
7. **P&L Distribution** — histogram for all/wins/losses

### Trade Log
- Full filterable table (strategy, market, direction, win/loss)
- Pagination (25 trades per page)
- Sortable columns
- Color-coded P&L (green/red)

### Animations
- Loading screen with pulsing logo mark
- Card entry animations with stagger delays
- Spring physics hover (cubic-bezier(0.34, 1.56, 0.64, 1))
- Animated number counters on KPI cards (ease-out cubic, 1.2s duration)
- Backtest running banner with spinner

---

## Data Pipeline

### generate_dashboard_data.py
1. Downloads 730 days of 1-hour Yahoo Finance data (ES, NQ)
2. Downloads 60 days of 5-minute data (ES, NQ)
3. Runs FHB strategy: ATR stops (0.75x), trail-to-BE after 1R
4. Runs ORB strategy: 15-min opening range (09:30–09:45)
5. Normalizes all trades to common schema
6. Computes daily P&L aggregates
7. Writes to `dashboard/cache/trades.json`

### Cache Schema (trades.json)
```json
{
  "generated_at": "ISO timestamp",
  "period_start": "YYYY-MM-DD",
  "period_end": "YYYY-MM-DD",
  "trades": [
    {
      "date": "YYYY-MM-DD",
      "strategy": "ORB|FHB",
      "market": "ES|NQ",
      "direction": "LONG|SHORT",
      "entry": float,
      "stop": float,
      "target": float,
      "exit": float,
      "exit_reason": "target|stop|trail|time",
      "pnl_net": float,
      "risk_pts": float
    }
  ],
  "daily": [
    {"date": "YYYY-MM-DD", "pnl": float}
  ]
}
```

---

## Performance Results (Dashboard Data)

| Metric | Value |
|--------|-------|
| Period | Oct 2023 – Feb 2026 (867 days, 259 trading days) |
| Total P&L | +$103,302 |
| Trades | 467 (ORB 54 + FHB 413) |
| Win Rate | 57.0% |
| Profit Factor | 1.49 |
| Sharpe Ratio | 2.78 |
| Sortino | 6.17 |
| Calmar | 7.04 |
| Max Drawdown | -$14,268 (-13.8%) |
| Avg Daily P&L | +$399/day |
| Annualized | +$100,510 |

---

## How to Use

### First-time setup
```bash
# 1. Create your login credentials (interactive)
conda run -n algobot_env python scripts/setup_dashboard_auth.py

# 2. Start the server
conda run -n algobot_env uvicorn dashboard.server:app --host 127.0.0.1 --port 8000

# 3. Open browser
# http://localhost:8000
```

### Refresh data
```bash
# Option A: from command line
conda run -n algobot_env python scripts/generate_dashboard_data.py

# Option B: click "▶ Run Backtest" in the dashboard
# → triggers async subprocess, shows progress banner, auto-refreshes when done
```

### Change password
```bash
conda run -n algobot_env python scripts/setup_dashboard_auth.py
# Answer "y" to overwrite
```

---

## Dependencies Added
```
fastapi>=0.109.0
uvicorn[standard]>=0.27.0
python-jose[cryptography]>=3.3.0
bcrypt>=4.0.0
python-multipart>=0.0.9
```

---

## Known Limitations
- Single-user only (one set of credentials)
- No HTTPS (add mkcert + `--ssl-keyfile`/`--ssl-certfile` flags for local HTTPS)
- Dashboard data is cached (not streaming live trades)
- ORB sample (60 days) is bull-market biased (Dec 2025 – Feb 2026 all-LONG period)

---

## Next Steps
- Phase 6: Run `run_paper_trading.py` during market hours to generate real live trades
- Future: Stream live trade events to dashboard via WebSocket (`/ws/trades`)
- Future: Multi-account support (Topstep + personal)
