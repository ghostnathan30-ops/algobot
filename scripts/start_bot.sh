#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  AlgoBot — Quick Start  (dashboard + bot, no tunnel management)
#
#  Use this when Tailscale Funnel is already running and you just
#  need to restart the dashboard and/or bot mid-day.
#
#  Usage: bash scripts/start_bot.sh
#
#  For full startup (first thing in the morning), use:
#    bash scripts/start_trading_day.sh
# ═══════════════════════════════════════════════════════════════

set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

# ── Locate conda env ──────────────────────────────────────────
CONDA_BASE="${CONDA_PREFIX:-${HOME}/miniconda3}"
for candidate in \
    "${HOME}/miniconda3" \
    "${HOME}/anaconda3" \
    "/opt/homebrew/opt/miniconda3" \
    "/opt/miniconda3"; do
  if [ -d "$candidate/envs/algobot_env" ]; then
    CONDA_BASE="$candidate"
    break
  fi
done

PYTHON="$CONDA_BASE/envs/algobot_env/bin/python"
UVICORN="$CONDA_BASE/envs/algobot_env/bin/uvicorn"

if [ ! -f "$PYTHON" ]; then
  echo ""
  echo "  ✗ algobot_env not found at $CONDA_BASE/envs/algobot_env"
  echo ""
  echo "  Run the one-time setup:"
  echo "    ~/miniconda3/bin/conda create -n algobot_env python=3.11 -y"
  echo "    ~/miniconda3/envs/algobot_env/bin/pip install -r requirements.txt"
  exit 1
fi

echo ""
echo "══════════════════════════════════════════════"
echo "  AlgoBot — Quick Start"
echo "══════════════════════════════════════════════"
echo "  Repo:   $REPO"
echo "  Python: $PYTHON"

# ── Show tunnel status ────────────────────────────────────────
TAILSCALE_CLI=$(command -v tailscale 2>/dev/null \
  || echo "/Applications/Tailscale.app/Contents/MacOS/Tailscale")
if [ -x "$TAILSCALE_CLI" ]; then
  TS_URL=$("$TAILSCALE_CLI" funnel status 2>/dev/null \
    | grep -o 'https://[a-zA-Z0-9._-]*\.ts\.net' | head -1)
  [ -z "$TS_URL" ] && TS_URL=$(cat "$REPO/.tailscale_url" 2>/dev/null || echo "")
fi
if [ -n "$TS_URL" ]; then
  echo "  Tunnel: $TS_URL  ✓"
else
  echo "  Tunnel: not active (run setup_tailscale.sh)"
fi

echo ""

# ── Stop stale processes ──────────────────────────────────────
pkill -f "uvicorn dashboard.server" 2>/dev/null && sleep 1 || true

# ── Start dashboard server ────────────────────────────────────
echo "Starting dashboard → http://localhost:8000"
"$UVICORN" dashboard.server:app \
  --host 127.0.0.1 --port 8000 \
  --log-level warning \
  > /tmp/algobot_server.log 2>&1 &
DASHBOARD_PID=$!
sleep 2

if ! curl -s http://localhost:8000/api/status >/dev/null 2>&1; then
  echo "  ✗ Dashboard failed to start. Check /tmp/algobot_server.log"
  exit 1
fi
echo "  ✓ Dashboard running (PID $DASHBOARD_PID)"
echo ""

# ── Start TV paper trading loop ───────────────────────────────
echo "Starting TradingView paper trading bot..."
echo "(Polls webhook queue every 5s — runs until 4:05 PM ET)"
echo "Press Ctrl+C to stop."
echo ""

cleanup() {
  echo ""
  echo "Stopping dashboard (PID $DASHBOARD_PID)..."
  kill "$DASHBOARD_PID" 2>/dev/null || true
  echo "Done."
}
trap cleanup INT TERM EXIT

# Run in foreground so Ctrl+C cleanly stops both
"$PYTHON" scripts/run_tv_paper_trading.py
