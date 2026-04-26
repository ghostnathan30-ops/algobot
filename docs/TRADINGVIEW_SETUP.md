# TradingView Alert Setup Guide

> Last updated: 2026-04-03 · Version: v4

Complete step-by-step setup so alerts fire automatically to the bot the moment a signal triggers.

---

## Active Strategies (as of v4 — 2026-04-03)

| Strategy | Chart | Timeframe | Direction | PF (TV) | PF (Python) | Status |
|----------|-------|-----------|-----------|---------|-------------|--------|
| **FHB v4** | NQ1! (Nasdaq 100 Futures) | **15m** | **Long only** | **1.73** | **2.87** | ✅ Active |
| **GC v3** | MGC1! (Micro Gold Futures) | **30m** | Both (fade) | — | 1.22 (YF) | ✅ Active |
| ORB | — | — | — | 0.95 | — | ❌ Disabled |
| CL | — | — | — | <1.0 | — | ❌ Disabled |

> **FHB v4 change (2026-04-03):** Short side disabled — TV backtest showed PF=0.918 (net loser, 103 trades). Long only from now on.
> **Always use MGC** (Micro Gold, 10 oz), NOT GC. GC MaxDD ~41% on $50K. MGC MaxDD ~4%.

---

## Step 1 — Start the Bot & Get Webhook URL

Run the startup script each morning before market open:

```bash
bash scripts/start_trading_day.sh
```

This starts:
- Dashboard server at `http://localhost:8000`
- cloudflared tunnel → gives you a `https://*.trycloudflare.com` URL

The tunnel URL is automatically written to `config/config.yaml` under `tv_paper.tunnel_url`.

**Copy the tunnel URL** — you need it for TradingView alerts.

To find it at any time:
```bash
grep tunnel_url config/config.yaml
```

---

## Step 2 — Add Pine Scripts to TradingView

### FHB Strategy (NQ1! 15m)

1. Open **NQ1!** chart, set timeframe to **15m**
2. Open Pine Editor → New script → paste contents of `pine/fhb_strategy.pine`
3. Click **Add to chart**
4. In **Inputs** panel, set:
   - **Webhook Secret**: your secret from `config/config.yaml` → `webhook_secret` field
   - **Market**: `NQ`
   - **Risk Mode**: `safe`
   - **Max Contracts**: `1`
   - All other parameters: leave at defaults (v3 calibrated values)

### GC Strategy (MGC1! 30m)

1. Open **MGC1!** chart (Micro Gold Futures), set timeframe to **30m**
2. Pine Editor → paste `pine/gc_strategy.pine`
3. In **Inputs** panel, set:
   - **Webhook Secret**: same secret as above
   - **Market**: `MGC` (already the default in v3)
   - **Risk Mode**: `safe`
   - All other parameters: leave at defaults

---

## Step 3 — Create Alerts

For **each strategy** (FHB and GC), create one alert:

1. Right-click the chart → **Add Alert**, or press `Alt+A`
2. **Condition**: Select your strategy → `Alert()` trigger
3. **Alert actions**: Check `Webhook URL`
4. **Webhook URL**: `https://YOUR-TUNNEL.trycloudflare.com/api/webhook/signal`
   - Replace `YOUR-TUNNEL` with your actual tunnel subdomain
5. **Message**: Leave blank — the Pine Script sends the JSON payload automatically
6. **Expiration**: Set to maximum (1 month or as long as available)
7. **Frequency**: `Once Per Bar Close` (critical — do NOT use "Once Per Bar")
8. Click **Create**

> **Important:** The alert fires on the bar CLOSE, not intrabar. This is correct and intentional.

---

## Step 4 — Verify the Webhook Secret

The webhook secret must match between TradingView Pine Script inputs and your config:

1. In `config/config.yaml`, look for the `webhook_secret` value (under `bot:`)
2. In TradingView Pine Script inputs for each strategy, paste that exact same secret
3. If you use the placeholder `CHANGE_ME_RANDOM_SECRET_HERE`, replace it with a strong random string (e.g., `python -c "import secrets; print(secrets.token_hex(32))"`)

---

## Step 5 — Start Paper Trading Bot

```bash
# In a new terminal:
conda run -n algobot_env python scripts/run_tv_paper_trading.py
```

Or from the dashboard:
1. Open `http://localhost:8000`
2. Log in → **Control** tab
3. Set Trading Mode to **TV Paper**
4. Click **Start Bot**

---

## Step 6 — Test the Connection

Send a test webhook manually to verify the pipeline works:

```bash
# Replace URL and secret with your values
curl -X POST https://YOUR-TUNNEL.trycloudflare.com/api/webhook/signal \
  -H "Content-Type: application/json" \
  -d '{
    "secret":        "YOUR_WEBHOOK_SECRET",
    "market":        "NQ",
    "strategy":      "FHB",
    "direction":     "LONG",
    "entry":         20000.00,
    "stop":          19950.00,
    "target":        20125.00,
    "size_mult":     1.0,
    "gls_score":     75,
    "htf_bias":      "BULL",
    "risk_mode":     "safe",
    "max_contracts": 1
  }'
```

Expected response: `{"ok": true, "queued": true, "queue_depth": 1}`

Check the bot terminal — you should see `[FHB] NQ LONG` logged within 5 seconds.

---

## Daily Workflow

```
Before 9:00 ET:
  1. bash scripts/start_trading_day.sh  ← get new tunnel URL
  2. Update TradingView alert webhook URLs with new tunnel URL
  3. python scripts/run_tv_paper_trading.py

During market hours (9:30–16:05 ET):
  - FHB signal fires 10:30–12:00 ET on NQ 15m bar close
  - GC signal fires 10:30–13:00 ET on MGC 30m bar close
  - Monitor dashboard at http://localhost:8000

After 16:05 ET:
  - Bot auto-settles all open positions
  - Check EOD summary in terminal
  - Review P&L in dashboard Overview tab
```

---

## Tunnel URL Changes Each Session

The cloudflared URL changes every time you restart the tunnel. You **must** update the alert webhook URLs in TradingView each morning.

**Quick update flow:**
1. Run `bash scripts/start_trading_day.sh`
2. Copy the new URL printed in the terminal
3. In TradingView: open each alert → edit → paste new URL → save

**Future improvement:** Use a fixed domain (e.g., Cloudflare named tunnel with free account) to avoid this daily update.

---

## Signal JSON Format

The Pine Script sends this JSON to the bot on each alert:

```json
{
  "secret":        "your-webhook-secret",
  "market":        "NQ",
  "strategy":      "FHB",
  "direction":     "LONG",
  "entry":         20100.25,
  "stop":          19940.50,
  "target":        20500.75,
  "size_mult":     1.0,
  "gls_score":     75,
  "htf_bias":      "BULL",
  "risk_mode":     "safe",
  "max_contracts": 1
}
```

The `secret` is validated server-side before queuing the signal. Unknown or disabled strategies are silently dropped (see `config/config.yaml` → `enabled_signals`).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Alert fires but no trade | Strategy disabled in config | Check `enabled_signals` in config.yaml |
| Webhook returns 403 | Wrong secret | Match secret in Pine inputs and config.yaml |
| Webhook returns 409 | Bot not running or wrong mode | Start bot, set mode to `tv_paper` |
| Webhook returns 503 | Signal queue full (>10) | Bot is not draining — check terminal |
| No alerts firing | Wrong timeframe or bad chart | Use NQ1! 15m for FHB, MGC1! 30m for GC |
| Tunnel URL expired | cloudflared restarted | Re-run start_trading_day.sh, update TV alerts |
| 0 trades in Strategy Tester | Using 1H timeframe | Switch to 15m (FHB) or 30m (GC) |
