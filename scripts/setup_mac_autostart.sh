#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  AlgoBot — Mac Auto-Start Setup (launchd)
#
#  Installs a LaunchAgent that starts the AlgoBot dashboard
#  automatically when you log in to your Mac.
#
#  Usage:
#    bash scripts/setup_mac_autostart.sh          # install
#    bash scripts/setup_mac_autostart.sh uninstall  # remove
#
#  What gets installed:
#    ~/Library/LaunchAgents/com.algobot.dashboard.plist
#    → Starts: uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
#    → Restarts automatically if it crashes
#    → Logs to: /tmp/algobot_server.log
#
#  The paper trading bot itself is NOT auto-started (it should
#  be launched manually each morning via start_trading_day.sh
#  so you can review the tunnel URL and update TradingView alerts).
# ─────────────────────────────────────────────────────────────

set -e

ACTION="${1:-install}"
PLIST_NAME="com.algobot.dashboard"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONDA_ENV="algobot_env"

# Detect conda prefix
CONDA_PREFIX=""
for candidate in \
    "$HOME/miniconda3" \
    "$HOME/anaconda3" \
    "/usr/local/anaconda3" \
    "/opt/homebrew/anaconda3" \
    "/opt/miniconda3"; do
  if [ -d "$candidate/envs/$CONDA_ENV" ]; then
    CONDA_PREFIX="$candidate"
    break
  fi
done

if [ -z "$CONDA_PREFIX" ] && [ "$ACTION" = "install" ]; then
  echo ""
  echo "  ✗ Could not find conda environment '${CONDA_ENV}'."
  echo "    Make sure you have run: conda create -n ${CONDA_ENV} ..."
  exit 1
fi

PYTHON_BIN="${CONDA_PREFIX}/envs/${CONDA_ENV}/bin/python"
UVICORN_BIN="${CONDA_PREFIX}/envs/${CONDA_ENV}/bin/uvicorn"

# ── Uninstall ─────────────────────────────────────────────────
if [ "$ACTION" = "uninstall" ]; then
  echo ""
  echo "▶ Removing AlgoBot auto-start..."
  launchctl unload "$PLIST_PATH" 2>/dev/null && echo "  ✓ LaunchAgent unloaded" || true
  rm -f "$PLIST_PATH" && echo "  ✓ Plist removed" || true
  echo ""
  echo "  AlgoBot will no longer start automatically at login."
  exit 0
fi

# ── Install ───────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════"
echo "  ALGOBOT — Mac Auto-Start Setup"
echo "══════════════════════════════════════════════"
echo ""
echo "  Bot directory : ${BOT_DIR}"
echo "  Conda env     : ${CONDA_ENV}"
echo "  Python        : ${PYTHON_BIN}"
echo "  Uvicorn       : ${UVICORN_BIN}"
echo ""

# Verify uvicorn exists
if [ ! -f "$UVICORN_BIN" ]; then
  echo "  ✗ uvicorn not found at ${UVICORN_BIN}"
  echo "    Run: conda run -n ${CONDA_ENV} pip install uvicorn"
  exit 1
fi

# ── Write the plist ───────────────────────────────────────────
echo "▶ Writing LaunchAgent plist to ${PLIST_PATH}..."
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_NAME}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${UVICORN_BIN}</string>
    <string>dashboard.server:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8000</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${BOT_DIR}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key>
    <string>${BOT_DIR}</string>
    <key>PATH</key>
    <string>${CONDA_PREFIX}/envs/${CONDA_ENV}/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>

  <!-- Restart automatically on crash -->
  <key>KeepAlive</key>
  <true/>

  <!-- Start at login -->
  <key>RunAtLoad</key>
  <true/>

  <!-- Throttle rapid restarts (wait 10s before retry) -->
  <key>ThrottleInterval</key>
  <integer>10</integer>

  <key>StandardOutPath</key>
  <string>/tmp/algobot_server.log</string>

  <key>StandardErrorPath</key>
  <string>/tmp/algobot_server.log</string>
</dict>
</plist>
PLISTEOF

echo "  ✓ Plist written"

# ── Load the agent ────────────────────────────────────────────
echo ""
echo "▶ Loading LaunchAgent..."
# Unload any existing version first (ignore errors)
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "  ✓ LaunchAgent loaded"

# ── Verify it started ─────────────────────────────────────────
sleep 3
if curl -s http://localhost:8000/api/status > /dev/null 2>&1; then
  echo "  ✓ Dashboard server is responding at http://localhost:8000"
else
  echo "  ⚠ Dashboard not responding yet — check logs:"
  echo "    tail /tmp/algobot_server.log"
fi

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════"
echo "  ✅  AUTO-START CONFIGURED"
echo "══════════════════════════════════════════════"
echo ""
echo "  The dashboard server will now start automatically"
echo "  every time you log in to your Mac."
echo ""
echo "  Dashboard:  http://localhost:8000"
echo "  Log:        tail -f /tmp/algobot_server.log"
echo ""
echo "  Control commands:"
echo "    Stop:    launchctl unload ${PLIST_PATH}"
echo "    Start:   launchctl load ${PLIST_PATH}"
echo "    Remove:  bash scripts/setup_mac_autostart.sh uninstall"
echo ""
echo "  NOTE: The paper trading bot still requires a manual"
echo "  daily launch via: bash scripts/start_trading_day.sh"
echo "══════════════════════════════════════════════"
