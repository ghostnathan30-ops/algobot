# AlgoBot — Complete Security & File Protection Guide
_Last updated: 2026-02-28_

---

## 1. COMPLETE FILE INVENTORY & SENSITIVITY LEVELS

### CRITICAL — Treat like a bank password (back up offline, never share)

| File | Location | What It Contains | Risk If Lost/Stolen |
|------|----------|-----------------|---------------------|
| `auth.json` | `dashboard/config/auth.json` | Dashboard bcrypt hash + JWT signing secret | Someone logs into your dashboard |
| `.env` | `.env` (project root) | IBKR host/port, API keys (FRED, Alpha Vantage, Telegram, Topstep) | API abuse, trading account access |
| Backup key files | `D:\ghost\AlgoBot_Backups\keys\*.key` | AES-256-GCM decryption key for each backup | Can't decrypt backups if PC dies |
| Backup key hex string | Saved in your password manager | Same as above | Same |

### SENSITIVE — Protect but lower risk (no financial access)

| File | Location | What It Contains |
|------|----------|-----------------|
| `config.yaml` | `config/config.yaml` | Strategy parameters, position sizing rules |
| `trades.db` | `data/trades.db` | Historical trade records (no credentials) |
| `trades.json` | `dashboard/cache/trades.json` | Performance data shown on dashboard |
| `*.parquet` | `data/raw/` | Cached market data (public data, replaceable) |

### LOW RISK — Already excluded from git, can be regenerated

| File | Location | Notes |
|------|----------|-------|
| `*.parquet` data files | `data/raw/` | Redownloaded from Yahoo Finance automatically |
| Log files | `logs/` | Bot activity logs |
| Report JSONs | `reports/` | Backtest outputs, regenerable |

---

## 2. YOUR ENCRYPTION KEYS — SAVE THESE NOW

### Dashboard JWT Secret
- **Location**: `dashboard/config/auth.json` → field `secret_key`
- **What to do**: Run `python scripts/setup_dashboard_auth.py` to see/reset it
- **Save to**: Bitwarden or KeePass under entry "AlgoBot Dashboard"

### Backup Encryption Keys (AES-256-GCM)
You have two backups on D drive. Each has its own key file:

| Backup File | Key File | Key Size |
|------------|---------|---------|
| `AlgoBot_2026-02-28_171404.zip.enc` | `AlgoBot_2026-02-28_171404.key` | 256-bit |
| `AlgoBot_2026-02-28_172538.zip.enc` | `AlgoBot_2026-02-28_172538.key` | 256-bit |

**To read your key** (run as Administrator in PowerShell):
```powershell
Get-Content "D:\ghost\AlgoBot_Backups\keys\AlgoBot_2026-02-28_172538.key"
```

**Save the 64-character hex key in Bitwarden** under:
```
Entry:    AlgoBot Backup Key (2026-02-28)
Username: (leave blank)
Password: 4033e18474ff4397ca512de97ad8437e2b2362a5b4b5b833ccf87674e806ddf8
Notes:    Backup file: D:\ghost\AlgoBot_Backups\AlgoBot_2026-02-28_172538.zip.enc
```

---

## 3. COMPLETE DIRECTORY MAP

```
C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot\
│
├── .env                          ← API KEYS — never commit, never share
├── .env.example                  ← Safe template (no real values)
├── .gitignore                    ← Excludes .env, auth.json, *.key, __pycache__
├── README.md                     ← Full specification (safe to share)
├── QUICKSTART.md                 ← How to run everything
├── requirements.txt              ← Python dependencies (safe)
│
├── config/
│   └── config.yaml               ← Strategy parameters (safe, no credentials)
│
├── dashboard/
│   ├── server.py                 ← FastAPI backend (safe)
│   ├── auth.py                   ← Auth logic (safe — no credentials in code)
│   ├── config/
│   │   └── auth.json             ← SENSITIVE: bcrypt hash + JWT secret
│   ├── cache/
│   │   └── trades.json           ← Performance data (regenerable)
│   └── static/
│       ├── index.html            ← Dashboard UI (safe)
│       └── login.html            ← Login page (safe)
│
├── data/
│   ├── trades.db                 ← SQLite trade history (sensitive, back up)
│   └── raw/                      ← Cached market data (regenerable)
│       ├── SPY_2000-*.parquet    ← 24 years daily data
│       ├── QQQ_2000-*.parquet
│       ├── GC_F_2000-*.parquet
│       ├── CL_F_2000-*.parquet
│       ├── TLT_2000-*.parquet
│       ├── EURUSD_X_2000-*.parquet
│       └── intraday/
│           ├── yf_ES_1h_730d.parquet   ← 2-year ES intraday
│           ├── yf_NQ_1h_730d.parquet
│           ├── yf_ES_5m_recent.parquet ← 60-day ES 5-min
│           └── yf_NQ_5m_recent.parquet
│
├── docs/
│   ├── LAB_001 through LAB_011.md  ← Lab reports (safe)
│   └── SECURITY_GUIDE.md           ← This file
│
├── scripts/
│   ├── create_backup.py            ← AES-256 encrypted backup
│   ├── restore_backup.py           ← Decrypt and restore
│   ├── setup_dashboard_auth.py     ← Change dashboard password
│   ├── generate_dashboard_data.py  ← Regenerate dashboard cache
│   ├── start_trading_day.sh        ← Mac daily launcher (tunnel + dashboard)
│   ├── start_bot.ps1               ← Windows launcher
│   ├── run_tv_paper_trading.py     ← TradingView paper trading (no IBKR)
│   ├── run_paper_trading.py        ← IBKR paper trading (requires TWS)
│   ├── run_comprehensive_backtest.py ← 6-layer validation
│   ├── run_validation_suite.py     ← Monte Carlo + walk-forward
│   ├── run_sc_backtest.py          ← Sierra Charts OOS backtest
│   ├── run_fhb_backtest.py         ← FHB strategy backtest
│   └── run_signal_replay.py        ← Historical signal replay
│
├── src/
│   ├── strategy/                   ← Signal logic (safe)
│   ├── backtest/                   ← Engine and metrics (safe)
│   ├── execution/
│   │   ├── paper_simulator.py      ← TV paper mode (no IBKR)
│   │   ├── tv_data_feed.py         ← yfinance live data
│   │   ├── ibkr_bridge.py          ← IBKR connection (reads .env)
│   │   └── live_signal_engine.py   ← Real-time signals
│   └── utils/                      ← Data pipeline (safe)
│
└── D:\ghost\AlgoBot_Backups\       ← ENCRYPTED BACKUPS (external drive = D)
    ├── AlgoBot_2026-02-28_171404.zip.enc  ← Backup 1
    ├── AlgoBot_2026-02-28_172538.zip.enc  ← Backup 2 (KEEP THIS)
    └── keys\
        ├── AlgoBot_2026-02-28_171404.key  ← Key for backup 1
        └── AlgoBot_2026-02-28_172538.key  ← Key for backup 2 (CRITICAL)
```

---

## 4. WHAT IS ALREADY PROTECTED

| Protection | Status | Detail |
|-----------|--------|--------|
| `.gitignore` | ACTIVE | Excludes `.env`, `auth.json`, `*.key`, `__pycache__`, `*.db` |
| Dashboard auth | ACTIVE | bcrypt (cost=12) + JWT httpOnly SameSite=Strict cookie |
| Security headers | ACTIVE | CSP, X-Frame-Options:DENY, nosniff, XSS protection |
| CORS | ACTIVE | Localhost only — no external origin allowed |
| Server binding | ACTIVE | 127.0.0.1 only — unreachable from other machines |
| Backup encryption | ACTIVE | AES-256-GCM per backup file |
| Key file ACL | ACTIVE | Administrators-only NTFS permission on key files |
| Swagger UI | DISABLED | `docs_url=None` — API not browseable |

---

## 5. HOW TO CREATE A NEW BACKUP (RUN REGULARLY)

```bash
# Creates a new timestamped AES-256-GCM encrypted backup on D drive
python scripts/create_backup.py
```

Output example:
```
Backup: D:\ghost\AlgoBot_Backups\AlgoBot_2026-02-28_172538.zip.enc
Key:    D:\ghost\AlgoBot_Backups\keys\AlgoBot_2026-02-28_172538.key
Files:  94 files, 2.6 MB
Integrity: PASSED
```

**Run this backup weekly** or after any major code change.

---

## 6. HOW TO RESTORE FROM BACKUP

```bash
python scripts/restore_backup.py
```

You will need:
1. The `.zip.enc` backup file
2. The matching `.key` file (or the 64-char hex key from your password manager)

---

## 7. PORT PROTECTION

### Port 8000 (Dashboard)
- Bound to `127.0.0.1` — **only your PC can reach it**
- Nobody on your Wi-Fi, your ISP, or the internet can connect
- The only risk: malware already running on your PC

### Port 7497 (IBKR TWS API)
- Also localhost-only by default in TWS settings
- **Verify in TWS**: Edit → Global Config → API → Trusted IPs = `127.0.0.1` only

### How to verify no ports are exposed to the network:
```bash
netstat -ano | grep LISTEN
```
All AlgoBot-related ports (8000, 7497) should show `127.0.0.1:PORT`, **not** `0.0.0.0:PORT`.
If you ever see `0.0.0.0:8000` that means the dashboard is exposed — restart with `--host 127.0.0.1`.

### Windows Firewall Rule (add this now for extra protection):
Open PowerShell as Administrator:
```powershell
# Block port 8000 from all external connections at the firewall level
New-NetFirewallRule -DisplayName "Block AlgoBot Dashboard External" `
  -Direction Inbound -Protocol TCP -LocalPort 8000 `
  -RemoteAddress Internet -Action Block

# Block TWS API port from external access
New-NetFirewallRule -DisplayName "Block TWS API External" `
  -Direction Inbound -Protocol TCP -LocalPort 7497 `
  -RemoteAddress Internet -Action Block
```

---

## 8. PROTECTING YOUR .env API KEYS

Your `.env` file contains:
- `FRED_API_KEY` — Federal Reserve data API
- `ALPHA_VANTAGE_KEY` — Market data API
- `IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID` — Trading connection
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` — Alerts
- `TOPSTEP_ACCOUNT_ID` — Funded account reference

**Rules:**
1. Never paste `.env` content into any chat, email, or forum
2. Never commit it to GitHub (already in `.gitignore`)
3. Store a copy of all key values in Bitwarden/KeePass
4. If any key is compromised: revoke it immediately from the provider's website

---

## 9. RECOMMENDED BACKUP SCHEDULE

| Frequency | Action |
|-----------|--------|
| **Weekly** | Run `python scripts/create_backup.py` |
| **Monthly** | Copy latest `.zip.enc` + `.key` to a USB drive, store offline |
| **After big changes** | Run backup immediately after major code updates |
| **Right now** | Copy the 64-char key hex to Bitwarden |

---

## 10. CHECKLIST — DO THESE NOW

- [ ] Save backup key hex `4033e18...` to Bitwarden/KeePass
- [ ] Run `setup_dashboard_auth.py` to set a strong personal password
- [ ] Save dashboard credentials to Bitwarden
- [ ] Copy latest `.zip.enc` to a USB drive
- [ ] Add Windows Firewall rules (Section 7 above)
- [ ] Enable Windows screen lock (5-minute timeout)
- [ ] Verify no ports show `0.0.0.0` in `netstat -ano | grep LISTEN`

---

## 11. IF SOMETHING GOES WRONG

| Scenario | Fix |
|----------|-----|
| Forgot dashboard password | Run `scripts/setup_dashboard_auth.py` |
| Dashboard won't start | Check port: `netstat -ano \| grep 8000`, kill old process |
| Backup key lost | Use the other backup + its key, or restore from hex in password manager |
| `.env` leaked | Immediately regenerate all API keys from each provider's website |
| PC dies completely | Restore from `.zip.enc` backup using any other PC + key hex |
| IBKR disconnects mid-trade | TWS auto-reconnects; all open orders persist in TWS independently |
