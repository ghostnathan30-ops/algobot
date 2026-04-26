#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  AlgoBot — Named Cloudflare Tunnel Setup
#
#  Run ONCE to create a persistent tunnel with a fixed URL.
#  After setup, your webhook URL never changes between sessions.
#
#  Usage:
#    bash scripts/setup_named_tunnel.sh
#
#  Prerequisites:
#    brew install cloudflare/cloudflare/cloudflared
#    cloudflared login    (opens browser — authenticate with Cloudflare)
#
#  What this script does:
#    1. Creates a named tunnel called "algobot"
#    2. Routes it to localhost:8000
#    3. Writes a cloudflared config to ~/.cloudflared/algobot.yml
#    4. Updates config/config.yaml with the fixed tunnel URL
#    5. Updates start_trading_day.sh to use the named tunnel
#
#  After setup:
#    Your webhook URL will be something like:
#      https://algobot.<your-domain>.com/api/webhook/signal
#    This URL is permanent — paste it into TradingView alerts ONCE.
# ─────────────────────────────────────────────────────────────

set -e

TUNNEL_NAME="algobot"
CONDA_ENV="algobot_env"
BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CF_CONFIG="$HOME/.cloudflared/${TUNNEL_NAME}.yml"

echo ""
echo "══════════════════════════════════════════════"
echo "  ALGOBOT — Named Cloudflare Tunnel Setup"
echo "══════════════════════════════════════════════"

# ── Check cloudflared is installed ───────────────────────────
if ! command -v cloudflared &>/dev/null; then
  echo ""
  echo "  ✗ cloudflared not found. Install it first:"
  echo "    brew install cloudflare/cloudflare/cloudflared"
  exit 1
fi

# ── Check authentication ──────────────────────────────────────
if [ ! -f "$HOME/.cloudflared/cert.pem" ]; then
  echo ""
  echo "  You are not logged in to Cloudflare."
  echo "  Running: cloudflared login"
  echo "  (A browser window will open — authenticate with your Cloudflare account)"
  echo ""
  cloudflared login
fi

# ── Check if tunnel already exists ───────────────────────────
EXISTING_ID=$(cloudflared tunnel list 2>/dev/null | awk "/$TUNNEL_NAME/{print \$1}" | head -1)

if [ -n "$EXISTING_ID" ]; then
  echo ""
  echo "  ✓ Tunnel '${TUNNEL_NAME}' already exists (ID: ${EXISTING_ID})"
  TUNNEL_ID="$EXISTING_ID"
else
  # Create the named tunnel
  echo ""
  echo "▶ Creating tunnel '${TUNNEL_NAME}'..."
  OUTPUT=$(cloudflared tunnel create "$TUNNEL_NAME" 2>&1)
  echo "$OUTPUT"
  TUNNEL_ID=$(echo "$OUTPUT" | grep -o '[0-9a-f\-]\{36\}' | head -1)
  if [ -z "$TUNNEL_ID" ]; then
    echo "  ✗ Failed to parse tunnel ID. Re-run after checking cloudflared output."
    exit 1
  fi
  echo "  ✓ Tunnel created: ${TUNNEL_ID}"
fi

# ── Write cloudflared config ──────────────────────────────────
echo ""
echo "▶ Writing cloudflared config to ${CF_CONFIG}..."

cat > "$CF_CONFIG" <<EOF
tunnel: ${TUNNEL_ID}
credentials-file: ${HOME}/.cloudflared/${TUNNEL_ID}.json

ingress:
  - service: http://localhost:8000

EOF

echo "  ✓ Config written"

# ── Check DNS route ───────────────────────────────────────────
echo ""
echo "▶ Setting up DNS route..."
echo "  NOTE: You must have a domain registered in your Cloudflare account."
echo "  If you only have the default trycloudflare.com domain (free tier),"
echo "  named tunnels require a custom domain."
echo ""

read -p "  Enter your Cloudflare domain (e.g. example.com), or press Enter to skip: " CF_DOMAIN

if [ -n "$CF_DOMAIN" ]; then
  SUBDOMAIN="${TUNNEL_NAME}.${CF_DOMAIN}"
  echo ""
  echo "▶ Creating DNS route: ${SUBDOMAIN} → tunnel ${TUNNEL_NAME}..."
  cloudflared tunnel route dns "$TUNNEL_NAME" "$SUBDOMAIN" && {
    echo "  ✓ DNS route created"
    FIXED_URL="https://${SUBDOMAIN}"
  } || {
    echo "  ✗ DNS route failed — tunnel is still usable but URL may change"
    FIXED_URL=""
  }
else
  echo "  Skipped DNS route — you'll need to configure this manually."
  FIXED_URL=""
fi

# ── Update algobot config.yaml ────────────────────────────────
if [ -n "$FIXED_URL" ]; then
  echo ""
  echo "▶ Updating config/config.yaml with fixed tunnel URL..."
  python3 -c "
import re
path = '${BOT_DIR}/config/config.yaml'
with open(path) as f: content = f.read()
updated = re.sub(r'tunnel_url:.*', 'tunnel_url: \"${FIXED_URL}\"', content)
with open(path, 'w') as f: f.write(updated)
print('  ✓ config.yaml updated')
"
fi

# ── Update start_trading_day.sh to use named tunnel ──────────
echo ""
echo "▶ Creating start_named_tunnel.sh helper..."
HELPER="$BOT_DIR/scripts/start_named_tunnel.sh"
cat > "$HELPER" <<HELPEOF
#!/bin/bash
# Start the named Cloudflare tunnel (fixed URL — no need to update TV alerts)
TUNNEL_NAME="${TUNNEL_NAME}"
CF_CONFIG="${CF_CONFIG}"

pkill -f "cloudflared tunnel run" 2>/dev/null
sleep 1
cloudflared tunnel --config "\$CF_CONFIG" run "\$TUNNEL_NAME" > /tmp/cloudflared.log 2>&1 &
echo "  ✓ Named tunnel '\$TUNNEL_NAME' started (PID \$!)"
HELPEOF
chmod +x "$HELPER"
echo "  ✓ Helper written to scripts/start_named_tunnel.sh"

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════════"
echo "  ✅  NAMED TUNNEL SETUP COMPLETE"
echo "══════════════════════════════════════════════"
echo ""
echo "  Tunnel name : ${TUNNEL_NAME}"
echo "  Tunnel ID   : ${TUNNEL_ID}"
if [ -n "$FIXED_URL" ]; then
  echo "  Fixed URL   : ${FIXED_URL}"
  echo "  Webhook URL : ${FIXED_URL}/api/webhook/signal"
  echo ""
  echo "  ★ Paste the webhook URL into your TradingView alerts ONCE."
  echo "    It will never change."
fi
echo ""
echo "  To start the tunnel every morning:"
echo "    bash scripts/start_named_tunnel.sh"
echo ""
echo "  Or use start_trading_day.sh — it will pick up the named tunnel"
echo "  automatically if scripts/start_named_tunnel.sh exists."
echo "══════════════════════════════════════════════"
