#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  AlgoBot — Tailscale Funnel Setup  (run ONCE)
#
#  Replaces cloudflared with Tailscale Funnel:
#    • WireGuard-encrypted — nobody can read your webhook traffic
#    • Fixed permanent URL — set TradingView alerts once, never again
#    • Free forever — no account limits
#    • No QUIC/UDP issues — pure HTTPS over TCP 443
#    • Open-source client — fully auditable
#
#  Usage:
#    bash scripts/setup_tailscale.sh
#
#  After setup your webhook URL looks like:
#    https://your-mac.your-tailnet.ts.net/api/webhook/signal
# ═══════════════════════════════════════════════════════════════

set -e

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$BOT_DIR/config/config.yaml"

echo ""
echo "══════════════════════════════════════════════════════"
echo "  AlgoBot — Tailscale Funnel Setup"
echo "══════════════════════════════════════════════════════"

# ── 1. Install Tailscale ──────────────────────────────────────
echo ""
echo "▶ [1/5] Checking Tailscale installation..."

if command -v tailscale &>/dev/null; then
  echo "  ✓ tailscale CLI already installed"
else
  echo "  Installing Tailscale via Homebrew..."
  if ! command -v brew &>/dev/null; then
    echo "  ✗ Homebrew not found. Install it first:"
    echo "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
    exit 1
  fi
  brew install --cask tailscale
  echo "  ✓ Tailscale installed"
fi

# ── 2. Start the Tailscale daemon ────────────────────────────
echo ""
echo "▶ [2/5] Starting Tailscale service..."

# macOS app bundle exposes the CLI at a known path
TAILSCALE_CLI=""
for candidate in \
    "$(command -v tailscale 2>/dev/null)" \
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale" \
    "/usr/local/bin/tailscale"; do
  if [ -x "$candidate" ]; then
    TAILSCALE_CLI="$candidate"
    break
  fi
done

if [ -z "$TAILSCALE_CLI" ]; then
  echo "  ✗ Cannot find tailscale binary. Try opening the Tailscale app manually."
  exit 1
fi

# Start the background service if not running
if ! "$TAILSCALE_CLI" status &>/dev/null 2>&1; then
  echo "  Starting Tailscale daemon..."
  open -a Tailscale 2>/dev/null || true
  sleep 3
fi

echo "  ✓ Tailscale daemon running"

# ── 3. Authenticate ──────────────────────────────────────────
echo ""
echo "▶ [3/5] Checking authentication..."

TS_STATUS=$("$TAILSCALE_CLI" status 2>&1 || true)

if echo "$TS_STATUS" | grep -q "Logged out\|not running\|NeedsLogin"; then
  echo "  Opening browser to log in to Tailscale..."
  echo "  (Use Google, GitHub, or Microsoft — takes ~30 seconds)"
  echo ""
  "$TAILSCALE_CLI" up
  sleep 2
else
  TS_SELF=$("$TAILSCALE_CLI" status --json 2>/dev/null | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(d.get('Self',{}).get('DNSName','unknown'))" \
    2>/dev/null || echo "connected")
  echo "  ✓ Already authenticated as: $TS_SELF"
fi

# ── 4. Enable Funnel on port 8000 ────────────────────────────
echo ""
echo "▶ [4/5] Enabling Tailscale Funnel on port 8000..."
echo "  (This exposes http://localhost:8000 to the public internet via HTTPS)"
echo ""

# Enable funnel in background (--bg makes it persistent across reboots)
"$TAILSCALE_CLI" funnel --bg 8000 2>/dev/null || \
  "$TAILSCALE_CLI" funnel 8000 --bg 2>/dev/null || \
  "$TAILSCALE_CLI" serve --bg --https=443 http://localhost:8000 2>/dev/null || true

sleep 2

# ── 5. Extract permanent webhook URL ─────────────────────────
echo ""
echo "▶ [5/5] Getting your permanent webhook URL..."

TS_URL=$("$TAILSCALE_CLI" funnel status 2>/dev/null \
  | grep -o 'https://[a-zA-Z0-9._-]*\.ts\.net' | head -1)

if [ -z "$TS_URL" ]; then
  # Fallback: build URL from DNS name
  DNS_NAME=$("$TAILSCALE_CLI" status --json 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); \
      n=d.get('Self',{}).get('DNSName',''); print(n.rstrip('.'))" 2>/dev/null || echo "")
  if [ -n "$DNS_NAME" ]; then
    TS_URL="https://${DNS_NAME}"
  fi
fi

if [ -z "$TS_URL" ]; then
  echo "  ✗ Could not determine Tailscale URL automatically."
  echo "    Run: tailscale funnel status"
  echo "    Then update config/config.yaml → tunnel_url manually."
  exit 1
fi

WEBHOOK_URL="${TS_URL}/api/webhook/signal"
echo "  ✓ Tunnel URL: $TS_URL"

# ── Update config.yaml with permanent URL ───────────────────
echo ""
echo "▶ Saving permanent URL to config/config.yaml..."
python3 - <<PYEOF
import re
path = "${CONFIG}"
with open(path, encoding="utf-8") as f:
    content = f.read()
updated = re.sub(r'tunnel_url:.*', f'tunnel_url: "${TS_URL}"', content)
with open(path, "w", encoding="utf-8") as f:
    f.write(updated)
print("  ✓ config.yaml updated")
PYEOF

# ── Save URL to a local state file for scripts to read ───────
echo "$TS_URL" > "$BOT_DIR/.tailscale_url"
echo "  ✓ URL cached in .tailscale_url"

# ── Kill and clean up cloudflared if still present ───────────
pkill -f "cloudflared" 2>/dev/null && echo "  ✓ Stopped cloudflared" || true

# ── Summary ──────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════════════"
echo "  ✅  TAILSCALE FUNNEL IS ACTIVE"
echo "══════════════════════════════════════════════════════"
echo ""
echo "  Your PERMANENT webhook URL:"
echo ""
echo "  ┌──────────────────────────────────────────────────────────┐"
echo "  │  ${WEBHOOK_URL}"
echo "  └──────────────────────────────────────────────────────────┘"
echo ""
echo "  ★ This URL NEVER CHANGES."
echo "    Paste it into all 4 TradingView alerts once — done forever."
echo ""
echo "  The funnel survives reboots. start_trading_day.sh no longer"
echo "  needs to manage the tunnel at all."
echo ""
echo "  To disable the funnel:  tailscale funnel off"
echo "  To re-enable:           tailscale funnel 8000"
echo "  To check status:        tailscale funnel status"
echo "══════════════════════════════════════════════════════"
