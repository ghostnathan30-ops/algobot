#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  AlgoBot — Trading Day Startup
#
#  Usage:  bash scripts/start_trading_day.sh
#
#  What it does:
#    1. Verifies Tailscale Funnel is active (permanent tunnel)
#    2. Starts the FastAPI dashboard server
#    3. Starts the TradingView paper trading bot
#    4. Prints your fixed webhook URL
#
#  First-time setup (run ONCE before using this script):
#    bash scripts/setup_tailscale.sh
# ═══════════════════════════════════════════════════════════════

CONDA_ENV="algobot_env"
BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# ── Locate conda Python / uvicorn ────────────────────────────
CONDA_BASE="${HOME}/miniconda3"
[ ! -d "$CONDA_BASE" ] && CONDA_BASE="${HOME}/anaconda3"
[ ! -d "$CONDA_BASE" ] && CONDA_BASE="/opt/homebrew/opt/miniconda3"
[ ! -d "$CONDA_BASE" ] && CONDA_BASE="/opt/miniconda3"

PYTHON="$CONDA_BASE/envs/$CONDA_ENV/bin/python"
UVICORN="$CONDA_BASE/envs/$CONDA_ENV/bin/uvicorn"

echo ""
echo "══════════════════════════════════════════════"
echo "  ALGOBOT — TRADING DAY STARTUP"
echo "══════════════════════════════════════════════"

# ── 1. Verify Tailscale Funnel is active ─────────────────────
echo ""
echo "▶ [1/3] Checking Tailscale Funnel..."

# Find tailscale CLI
TAILSCALE_CLI=$(command -v tailscale 2>/dev/null \
  || echo "/Applications/Tailscale.app/Contents/MacOS/Tailscale")

FUNNEL_URL=""

if [ -x "$TAILSCALE_CLI" ]; then
  # Try to get URL from running funnel
  FUNNEL_URL=$("$TAILSCALE_CLI" funnel status 2>/dev/null \
    | grep -o 'https://[a-zA-Z0-9._-]*\.ts\.net' | head -1)

  # Fallback: read from cached file written by setup_tailscale.sh
  if [ -z "$FUNNEL_URL" ] && [ -f "$BOT_DIR/.tailscale_url" ]; then
    FUNNEL_URL=$(cat "$BOT_DIR/.tailscale_url")
  fi

  # Funnel not active — re-enable it automatically
  if [ -z "$FUNNEL_URL" ]; then
    echo "  Funnel not active — re-enabling..."
    "$TAILSCALE_CLI" funnel --bg 8000 2>/dev/null || \
      "$TAILSCALE_CLI" funnel 8000 --bg 2>/dev/null || true
    sleep 2
    FUNNEL_URL=$("$TAILSCALE_CLI" funnel status 2>/dev/null \
      | grep -o 'https://[a-zA-Z0-9._-]*\.ts\.net' | head -1)
    [ -z "$FUNNEL_URL" ] && FUNNEL_URL=$(cat "$BOT_DIR/.tailscale_url" 2>/dev/null || echo "")
  fi
fi

if [ -z "$FUNNEL_URL" ]; then
  echo "  ✗ Tailscale Funnel not configured."
  echo "    Run: bash scripts/setup_tailscale.sh"
  echo ""
  echo "  Continuing without tunnel (dashboard local-only)."
  WEBHOOK_URL="http://localhost:8000/api/webhook/signal  [tunnel not active]"
else
  WEBHOOK_URL="${FUNNEL_URL}/api/webhook/signal"
  echo "  ✓ Tunnel active: $FUNNEL_URL"
fi

# ── 2. Start dashboard server ─────────────────────────────────
echo ""
echo "▶ [2/3] Starting dashboard server..."

# Stop any existing instance
pkill -f "uvicorn dashboard.server" 2>/dev/null && sleep 1 || true

cd "$BOT_DIR"

if [ -f "$UVICORN" ]; then
  "$UVICORN" dashboard.server:app \
    --host 0.0.0.0 --port 8000 \
    --log-level warning \
    > /tmp/algobot_server.log 2>&1 &
  SERVER_PID=$!
else
  # Fallback: use conda run
  conda run -n "$CONDA_ENV" uvicorn dashboard.server:app \
    --host 0.0.0.0 --port 8000 \
    --log-level warning \
    > /tmp/algobot_server.log 2>&1 &
  SERVER_PID=$!
fi

# Wait for server to be ready (up to 10s)
SERVER_OK=false
for i in $(seq 1 10); do
  if curl -s http://localhost:8000/api/status >/dev/null 2>&1; then
    SERVER_OK=true
    break
  fi
  sleep 1
done

if [ "$SERVER_OK" = false ]; then
  echo "  ✗ Dashboard failed to start. Check:"
  echo "    tail /tmp/algobot_server.log"
  exit 1
fi
echo "  ✓ Dashboard running (PID $SERVER_PID) → http://localhost:8000"

# ── 3. Start paper trading bot ────────────────────────────────
echo ""
echo "▶ [3/3] Starting paper trading bot..."

if [ -f "$PYTHON" ]; then
  "$PYTHON" "$BOT_DIR/scripts/run_tv_paper_trading.py" \
    > /tmp/algobot_bot.log 2>&1 &
  BOT_PID=$!
else
  conda run -n "$CONDA_ENV" python "$BOT_DIR/scripts/run_tv_paper_trading.py" \
    > /tmp/algobot_bot.log 2>&1 &
  BOT_PID=$!
fi

sleep 2

if kill -0 "$BOT_PID" 2>/dev/null; then
  echo "  ✓ Bot running (PID $BOT_PID)"
else
  # Check if it exited cleanly (weekend/holiday) vs crashed
  if grep -q "not a trading day" /tmp/algobot_bot.log 2>/dev/null; then
    echo "  ℹ Bot exited — today is not a trading day (weekend/holiday)."
    echo "    Run this script again on a weekday morning."
    BOT_PID=""
  else
    echo "  ✗ Bot failed to start. Check:"
    echo "    tail /tmp/algobot_bot.log"
  fi
fi

# Update config.yaml tunnel_url if we have the URL
if [ -n "$FUNNEL_URL" ]; then
  python3 - <<PYEOF 2>/dev/null || true
import re
path = "${BOT_DIR}/config/config.yaml"
with open(path, encoding="utf-8") as f:
    content = f.read()
updated = re.sub(r'tunnel_url:.*', f'tunnel_url: "${FUNNEL_URL}"', content)
with open(path, "w", encoding="utf-8") as f:
    f.write(updated)
PYEOF
fi

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════"
echo "  ✅  ALGOBOT IS RUNNING"
echo "══════════════════════════════════════════════"
echo ""
echo "  Dashboard : http://localhost:8000"
echo ""
echo "  ┌──────────────────────────────────────────────────────────┐"
echo "  │  TRADINGVIEW WEBHOOK URL:                                │"
echo "  │                                                          │"
echo "  │  ${WEBHOOK_URL}"
echo "  │                                                          │"
echo "  └──────────────────────────────────────────────────────────┘"
echo ""

if [ -n "$FUNNEL_URL" ] && [ -f "$BOT_DIR/.tailscale_url" ]; then
  SAVED_URL=$(cat "$BOT_DIR/.tailscale_url")
  if [ "$FUNNEL_URL" = "$SAVED_URL" ]; then
    echo "  ★ This URL is PERMANENT — TradingView alerts are already set."
  else
    echo "  ⚠ URL changed — update all 4 TradingView alerts with the URL above."
    echo "$FUNNEL_URL" > "$BOT_DIR/.tailscale_url"
  fi
else
  echo "  □ Paste this URL into all 4 TradingView alerts if not already done."
fi

echo ""
echo "  Logs:"
echo "    tail -f /tmp/algobot_server.log"
echo "    tail -f /tmp/algobot_bot.log"
echo ""
echo "  Press Ctrl+C to stop everything"
echo "══════════════════════════════════════════════"

# ── Keep alive, clean up on exit ─────────────────────────────
cleanup() {
  echo ""
  echo "Shutting down..."
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  [ -n "$BOT_PID"    ] && kill "$BOT_PID"    2>/dev/null || true
  exit 0
}
trap cleanup INT TERM
wait
