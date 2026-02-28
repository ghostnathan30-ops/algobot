# SECURITY — AlgoBot Security Baseline
### Protecting Your Credentials, Capital, and System

**Date:**     2026-02-27
**Phase:**    0 — Foundation
**Status:**   ACTIVE — applies to all phases

---

## Why Security Matters Here Specifically

AlgoBot handles three categories of sensitive assets:

1. **API keys** — If stolen, someone could drain your FRED/broker API limits or access your account data
2. **Broker credentials** — If stolen, someone could close your Topstep positions or submit orders
3. **Financial capital** — The funded account is real money. Security failures can mean financial loss

A single `.env` file committed to a public GitHub repo has caused real traders to lose funded accounts.
We prevent every known attack vector in this document.

---

## Layer 1 — Secrets Management (API Keys and Credentials)

### The Rule: Secrets NEVER touch code files

All credentials live exclusively in the `.env` file. The `.env` file:
- Is listed in `.gitignore` (never committed to git)
- Is listed in `.gitignore` in the data/ and logs/ folders too
- Is never logged, never printed, never included in reports
- Is loaded at runtime via `python-dotenv` — not hardcoded anywhere

### How Secrets Are Loaded in Code

Every module that needs a credential does this:

```python
# CORRECT — loads from .env at runtime
from dotenv import load_dotenv
import os

load_dotenv()  # reads .env file
api_key = os.environ.get("FRED_API_KEY")

if not api_key:
    raise ValueError("FRED_API_KEY not set in .env file")
```

```python
# WRONG — never do this
api_key = "abc123xyz"  # hardcoded — visible to anyone who reads the file
```

### Verifying No Secrets Are in Git History

Before any `git push`, run:
```bash
git log --all --full-history -- .env
# If this shows anything, .env was committed — see remediation below
```

If `.env` was accidentally committed:
```bash
# Remove from all git history (nuclear option — use carefully)
git filter-branch --force --index-filter \
  "git rm --cached --ignore-unmatch .env" \
  --prune-empty --tag-name-filter cat -- --all
```

---

## Layer 2 — Code Security (Input Validation and Safe Operations)

### Data Input Validation

All market data loaded from external sources is validated before use:

```python
# In src/utils/data_cleaner.py — enforced on every data load
def validate_ohlcv(df):
    # High must be >= Low on every bar
    assert (df['high'] >= df['low']).all(), "Data error: high < low found"

    # Close must be between low and high
    assert (df['close'] >= df['low']).all(), "Data error: close < low"
    assert (df['close'] <= df['high']).all(), "Data error: close > high"

    # No negative prices
    assert (df[['open','high','low','close']] > 0).all().all(), "Negative price"

    # No NaN in price columns (after cleaning)
    assert not df[['open','high','low','close']].isnull().any().any(), "NaN in prices"
```

### No Eval, No Shell Injection

The codebase never uses:
- `eval()` or `exec()` on external data
- `os.system()` with user-provided input
- `subprocess.call()` with string interpolation
- `pickle.load()` on untrusted files (use `yaml.safe_load()` instead)

```python
# WRONG — vulnerable to injection
os.system(f"git commit -m '{user_input}'")

# CORRECT — use subprocess with list arguments
subprocess.run(["git", "commit", "-m", commit_message], check=True)
```

### Configuration Loading — Safe YAML Only

```python
import yaml

# CORRECT — safe_load prevents code execution in YAML
with open("config/config.yaml", "r") as f:
    config = yaml.safe_load(f)

# WRONG — yaml.load() allows arbitrary code execution in crafted YAML
config = yaml.load(f)  # Never use this
```

---

## Layer 3 — Broker and Live Trading Security

### The "Paper Mode First" Rule

The `config.yaml` file has:
```yaml
mode: "backtest"  # or "paper"
live:
  paper_mode: true  # MUST remain true until Go/No-Go checklist complete
```

The live engine code checks this at startup:

```python
def startup_safety_check(config):
    if config['mode'] == 'live' and config['live']['paper_mode']:
        raise ValueError("Contradiction: mode='live' but paper_mode=True. Check config.")

    if config['mode'] == 'live':
        # Require explicit human confirmation at startup
        confirm = input("WARNING: LIVE mode active. Type 'CONFIRMED' to proceed: ")
        if confirm != "CONFIRMED":
            raise SystemExit("Live mode startup cancelled by user.")
```

### Order Safety Limits (Enforced in order_manager.py)

Before any order is submitted to the broker, a safety check fires:

```python
MAX_CONTRACTS_LIMIT = 10  # Absolute hard limit — no order above this ever sent
MAX_SINGLE_ORDER_VALUE = 500_000  # $500k notional — reject anything above this

def submit_order(market, contracts, direction):
    # Hard safety checks — cannot be disabled by config
    if contracts > MAX_CONTRACTS_LIMIT:
        raise ValueError(f"Order rejected: {contracts} contracts exceeds hard limit {MAX_CONTRACTS_LIMIT}")

    notional = contracts * market.point_value * current_price
    if notional > MAX_SINGLE_ORDER_VALUE:
        raise ValueError(f"Order rejected: ${notional:,.0f} notional exceeds limit")

    # Only then submit
    broker.submit(market, contracts, direction)
```

### Emergency Stop (Kill Switch)

A dedicated emergency stop is available at all times:

```python
# src/live/risk_monitor.py
def emergency_close_all():
    """
    NUCLEAR OPTION: Immediately close ALL open positions.
    Sends market orders for every open position regardless of P&L.
    Logs the action and sends Telegram alert.
    Use only when: bot is malfunctioning, Topstep limit approaching, emergency.
    """
    logger.critical("EMERGENCY STOP TRIGGERED — closing all positions")
    send_alert("🔴 EMERGENCY STOP TRIGGERED — closing all positions NOW", level="CRITICAL")
    for position in get_all_open_positions():
        close_position(position, reason="EMERGENCY_STOP")
    halt_all_new_entries()
    logger.critical("All positions closed. Bot halted.")
```

**How to trigger it manually:** Kill the bot process on the VPS (`Ctrl+C` or `kill PID`).
All open positions remain — you then manually close them in NinjaTrader or IBKR.

---

## Layer 4 — VPS Security (When Running Live, Phase 6+)

When we reach live trading, the bot runs on a remote server 24/7.
These steps are required when setting up the VPS.

### SSH Security (No Password Login)
```bash
# On your local machine — generate SSH key pair
ssh-keygen -t ed25519 -C "algobot-vps"

# Copy public key to VPS (run once)
ssh-copy-id -i ~/.ssh/algobot-vps.pub user@your-vps-ip

# Disable password authentication on VPS
# Edit /etc/ssh/sshd_config:
# PasswordAuthentication no
# PubkeyAuthentication yes
```

### Firewall Rules (UFW — Ubuntu)
```bash
# Allow only SSH (22) and NinjaTrader/IBKR ports
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp          # SSH
ufw allow 7497/tcp        # IBKR TWS (if using IBKR)
ufw enable
```

### Automatic Security Updates
```bash
# Ubuntu — auto-install security patches
apt install unattended-upgrades -y
dpkg-reconfigure --priority=low unattended-upgrades
```

### Bot Process — Least Privilege
```bash
# Run the bot as a dedicated non-root user
adduser algobot --disabled-password
# Copy bot code to /home/algobot/algobot/
# Run bot as this user only — never as root
su -s /bin/bash algobot -c "python main.py"
```

---

## Layer 5 — Logging Security (No Secrets in Logs)

Logs are essential for debugging but must never contain credentials.

```python
# In src/utils/logger.py
import re

REDACT_PATTERNS = [
    (r'api_key["\s]*[:=]["\s]*\S+', 'api_key=REDACTED'),
    (r'password["\s]*[:=]["\s]*\S+', 'password=REDACTED'),
    (r'token["\s]*[:=]["\s]*\S+', 'token=REDACTED'),
]

def sanitize_log_message(message: str) -> str:
    """Remove any accidentally included credentials from log messages."""
    for pattern, replacement in REDACT_PATTERNS:
        message = re.sub(pattern, replacement, message, flags=re.IGNORECASE)
    return message
```

Log files are also gitignored (in `.gitignore`) so they are never committed.

---

## Layer 6 — Dependency Security (Supply Chain)

We pin all library versions in `requirements.txt`. This prevents a malicious
update to a dependency from affecting the bot.

```
# requirements.txt uses pinned versions:
vectorbt>=0.26.0    # Not just "vectorbt" — ensures minimum safe version
pandas>=2.0.0       # Same pattern throughout
```

Periodically check for known vulnerabilities:
```bash
pip install pip-audit
pip-audit  # Scans installed packages against known CVE database
```

---

## Security Checklist — Verify Before Each Phase

```
PHASE 0 (Now):
  [ ] .env is in .gitignore and never committed
  [ ] config.yaml mode is "backtest" or "paper"
  [ ] verify_setup.py security check passes
  [ ] GitHub repository is set to PRIVATE

PHASE 6 (Live):
  [ ] VPS uses SSH key authentication (no passwords)
  [ ] VPS firewall is configured (ufw)
  [ ] Bot runs as non-root user
  [ ] Broker API credentials in .env only, not in code
  [ ] Emergency stop procedure tested
  [ ] Daily loss hard stop tested

PHASE 7 (Topstep):
  [ ] Topstep credentials in .env only
  [ ] Paper_mode: true until Go/No-Go checklist complete
  [ ] Order size hard limits enforced in code
  [ ] Startup confirmation prompt active
```

---

## What To Do If a Credential Is Compromised

If you believe an API key or broker credential was exposed:

```
Immediate actions (do in order):
  1. Revoke the compromised key IMMEDIATELY at the provider's website
  2. Generate a new key and update .env
  3. Check broker account for unauthorized activity
  4. If broker credential: log into NinjaTrader/IBKR and change password NOW
  5. Check git history: git log --all --full-history -- .env
  6. If committed to git: rotate all keys and scrub git history (see Layer 1)
  7. Enable 2FA on all broker and exchange accounts
```

---

*Security is not a phase — it is applied from Phase 0 onwards and throughout the entire project lifetime.*
