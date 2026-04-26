# LAB_001 — Development Environment Setup

**Date:** 2026-02-27
**Phase:** 0 — Foundation Setup
**Status:** IN PROGRESS — Files created, awaiting installation
**Engineer:** Claude (claude-sonnet-4-6)
**Reviewed By:** Ghost

## System State at Phase 0 Start

| Tool | Status | Notes |
|---|---|---|
| Git | **Installed** | v2.53.0.windows.1 |
| Python | **Not installed** | Install via Miniconda below |
| Conda | **Not installed** | Install via Miniconda below |
| pip | **Not installed** | Comes with Miniconda |
| VS Code | Unknown | Install if not present |

## Phase 0 Files Created

| File | Status | Purpose |
|---|---|---|
| `README.md` | **DONE** | Master project bible |
| `requirements.txt` | **DONE** | All Python dependencies |
| `.gitignore` | **DONE** | Protects secrets from git |
| `.env.example` | **DONE** | API key template |
| `config/config.yaml` | **DONE** | All strategy parameters |
| `verify_setup.py` | **DONE** | Environment verification script |
| `docs/SECURITY.md` | **DONE** | Full security guide |
| `docs/LAB_001_Environment_Setup.md` | **DONE (this file)** | Phase 0 lab report |

---

## Objective

Establish a complete, reproducible Python development environment for the AlgoBot project.
This lab report documents every installation step, every decision made, and every tool configured
so that the environment can be recreated from scratch at any time on any machine.

## Background

AlgoBot requires a scientific Python environment with financial data, backtesting, and
live trading libraries. The environment must be isolated (using Conda virtual environments)
to prevent dependency conflicts between AlgoBot and any other Python projects on the machine.

**Platform:** Windows 10/11 with MINGW64 bash shell
**Shell:** bash (Unix syntax)
**Python Target Version:** 3.11.x (stable, best library compatibility as of 2026)

---

## Method & Steps

## The author is Nathan-Blaise MIHINDU MI NZAMBE

Follow these steps **in exact order**. Each step has a verification check.

---

### STEP 1 — Install Miniconda (Python Environment Manager)

Miniconda is a lightweight version of Anaconda. It manages Python versions and
creates isolated environments so our bot's libraries never conflict with other software.

**Download:**
Go to: https://docs.conda.io/en/latest/miniconda.html
Download: `Miniconda3-latest-Windows-x86_64.exe`

**Install:**
- Run the installer
- Select: Install for "Just Me" (recommended)
- Default install path: `C:\Users\ghost\miniconda3`
- Check: "Add Miniconda to PATH" (required for bash access)
- Check: "Register Miniconda as default Python"

**Verify installation — open a new bash terminal and run:**
```bash
conda --version
# Expected output: conda 24.x.x or higher

python --version
# Expected output: Python 3.11.x
```

**Status:** [ ] Complete

---

### STEP 2 — Create the AlgoBot Virtual Environment

Virtual environments isolate our project's Python packages from the system Python.
This prevents version conflicts and makes the project reproducible.

```bash
# Create the environment with Python 3.11
conda create -n algobot_env python=3.11 -y

# Activate the environment
conda activate algobot_env

# Verify you are in the correct environment
python --version
# Expected: Python 3.11.x

which python
# Expected: path containing 'algobot_env'
```

**IMPORTANT:** Every time you work on this project, activate the environment first:
```bash
conda activate algobot_env
```

**Status:** [ ] Complete

---

### STEP 3 — Install Core Python Libraries

With the algobot_env environment active, install all required libraries.
Run each block and verify no errors before proceeding.

#### Block A — Core Scientific Stack
```bash
conda install -n algobot_env numpy pandas scipy matplotlib -y
```

**Verify:**
```bash
python -c "import numpy, pandas, scipy, matplotlib; print('Block A: OK')"
```

#### Block B — Financial Analysis Libraries
```bash
pip install vectorbt pyfolio-reloaded empyrical-reloaded statsmodels
```

**Verify:**
```bash
python -c "import vectorbt, empyrical, statsmodels; print('Block B: OK')"
```

#### Block C — Technical Indicators
```bash
pip install pandas-ta ta
```

**Verify:**
```bash
python -c "import pandas_ta; print('Block C: OK')"
```

#### Block D — Data Download Libraries
```bash
pip install yfinance fredapi requests pandas-datareader
```

**Verify:**
```bash
python -c "import yfinance, fredapi, pandas_datareader; print('Block D: OK')"
```

#### Block E — Visualization and Reporting
```bash
pip install plotly kaleido jinja2 weasyprint nbformat
```

**Verify:**
```bash
python -c "import plotly, jinja2; print('Block E: OK')"
```

#### Block F — Jupyter Notebooks
```bash
conda install -n algobot_env jupyter notebook ipykernel -y
python -m ipykernel install --user --name=algobot_env --display-name "AlgoBot (Python 3.11)"
```

**Verify:**
```bash
jupyter notebook --version
# Expected: 6.x or 7.x
```

#### Block G — Utilities
```bash
pip install pyyaml python-dotenv loguru schedule tqdm colorama
```

**Verify:**
```bash
python -c "import yaml, loguru, schedule; print('Block G: OK')"
```

**Status:** [ ] All blocks installed and verified

---

### STEP 4 — Install VS Code

VS Code is the code editor. It has excellent Python support and is completely free.

**Download:**
https://code.visualstudio.com/

**Install required extensions (open VS Code, press Ctrl+Shift+X, search each):**

| Extension | Publisher | Purpose |
|---|---|---|
| Python | Microsoft | Python language support |
| Pylance | Microsoft | Type checking and autocomplete |
| Jupyter | Microsoft | Run notebooks in VS Code |
| GitLens | GitKraken | Enhanced Git visualization |
| Better Comments | Aaron Bond | Color-coded comments |
| Indent Rainbow | oderwat | Visual indentation guides |
| Error Lens | Alexander | Inline error display |

**Configure Python interpreter:**
1. Open VS Code
2. Press `Ctrl+Shift+P`
3. Type "Python: Select Interpreter"
4. Choose the interpreter that contains "algobot_env"

**Status:** [ ] Complete

---

### STEP 5 — Install Git

Git tracks every change to the code so nothing is ever permanently lost.

**Download:**
https://git-scm.com/download/win

**Install with default settings.**

**Configure Git with your identity:**
```bash
git config --global user.name "Ghost"
git config --global user.email "your-email@example.com"
```

**Verify:**
```bash
git --version
# Expected: git version 2.x.x
```

**Status:** [ ] Complete

---

### STEP 6 — Set Up GitHub Repository

GitHub stores a cloud backup of all project code. Free for private repositories.

**Steps:**
1. Go to https://github.com and create a free account (if needed)
2. Click "New Repository"
3. Name: `algobot`
4. Set to Private
5. Do not initialize with README (we have our own)
6. Click "Create Repository"

**Connect local project to GitHub:**
```bash
# Navigate to the project folder
cd "C:/Users/ghost/Documents/Claude Workflow/Trading/AlgoBot"

# Initialize git repository
git init

# Add all files
git add .

# First commit
git commit -m "LAB_001: Initial project setup and structure"

# Connect to GitHub (replace YOUR_USERNAME with your GitHub username)
git remote add origin https://github.com/YOUR_USERNAME/algobot.git

# Push to GitHub
git push -u origin main
```

**Status:** [ ] Complete

---

### STEP 7 — Get Free API Keys

These API keys are free and required for data access.

#### FRED API Key (Federal Reserve Economic Data)
1. Go to: https://fred.stlouisfed.org/docs/api/api_key.html
2. Create a free account
3. Request an API key (instant)
4. Save key to `.env` file (see Step 8)

#### Alpha Vantage API Key (Supplemental data)
1. Go to: https://www.alphavantage.co/support/#api-key
2. Request free API key (instant)
3. Save key to `.env` file

#### QuantConnect Account
1. Go to: https://www.quantconnect.com
2. Create a free account
3. Note your Organization ID (in account settings)
4. This gives access to their free historical futures data

**Status:** [ ] All API keys obtained

---

### STEP 8 — Create .env File (Secure API Key Storage)

Never store API keys directly in code. Use a `.env` file which is excluded from Git.

**Create the file:**
```bash
# From the AlgoBot directory
touch .env
```

**Edit .env with VS Code and add:**
```
FRED_API_KEY=your_fred_api_key_here
ALPHA_VANTAGE_KEY=your_alpha_vantage_key_here
QUANTCONNECT_USER_ID=your_qc_user_id
QUANTCONNECT_TOKEN=your_qc_token
TELEGRAM_BOT_TOKEN=not_set_yet
TELEGRAM_CHAT_ID=not_set_yet
```

**Create .gitignore to protect secrets:**
```bash
# Create .gitignore file
cat > .gitignore << 'EOF'
# Environment secrets
.env

# Python
__pycache__/
*.py[cod]
*.pyo
.pytest_cache/
*.egg-info/

# Data (too large for git)
data/raw/
data/processed/

# Logs
logs/*.log

# Conda environments (not committed)
algobot_env/

# OS files
.DS_Store
Thumbs.db

# Jupyter checkpoints
.ipynb_checkpoints/
EOF
```

**Status:** [ ] Complete

---

### STEP 9 — Create Initial Configuration File

All strategy parameters live in one central config file. This makes changing parameters
easy and ensures consistency between backtest and live modes.

**File:** `config/config.yaml`

```yaml
# AlgoBot Configuration — Version 1.0
# All strategy parameters are defined here.
# Never hardcode parameters in source files.

project:
  name: "AlgoBot"
  version: "1.0.0"
  mode: "backtest"         # Options: "backtest", "paper", "live"
  log_level: "INFO"

# ============================================================
# STRATEGY PARAMETERS
# ============================================================
strategy:

  # --- Trend Signal: Dual EMA ---
  dema:
    fast_period: 21          # Fast EMA lookback (bars)
    slow_period: 89          # Slow EMA lookback (bars)

  # --- Breakout Signal: Donchian Channel ---
  donchian:
    breakout_period: 20      # Bars for channel calculation

  # --- Regime Filter: ADX ---
  adx:
    period: 14               # ADX lookback (bars)
    threshold_trending: 25   # ADX above this = trending, trade allowed
    threshold_ranging: 20    # ADX below this = ranging, no new trades

  # --- Position Sizing: ATR-Based ---
  position_sizing:
    atr_period: 14           # ATR lookback (bars)
    atr_stop_multiplier: 2.5 # Stop distance in ATR multiples
    risk_per_trade_pct: 1.0  # % of account to risk per trade

  # --- Timeframe ---
  timeframe: "daily"         # Options: "daily", "4hour", "1hour"

# ============================================================
# MARKETS TRADED
# ============================================================
markets:
  - symbol: "ES"
    name: "E-mini S&P 500"
    point_value: 50.0        # $ per full point
    tick_size: 0.25          # Minimum price increment
    tick_value: 12.50        # $ per tick
    commission: 5.00         # $ per side (round turn = 2x)
    slippage_ticks: 1        # Assumed slippage in ticks

  - symbol: "NQ"
    name: "E-mini Nasdaq-100"
    point_value: 20.0
    tick_size: 0.25
    tick_value: 5.00
    commission: 5.00
    slippage_ticks: 1

  - symbol: "GC"
    name: "Gold Futures"
    point_value: 100.0
    tick_size: 0.10
    tick_value: 10.00
    commission: 5.00
    slippage_ticks: 1

  - symbol: "CL"
    name: "Crude Oil WTI"
    point_value: 1000.0
    tick_size: 0.01
    tick_value: 10.00
    commission: 5.00
    slippage_ticks: 1

  - symbol: "ZB"
    name: "30-Year Treasury Bond"
    point_value: 1000.0
    tick_size: 0.03125
    tick_value: 31.25
    commission: 5.00
    slippage_ticks: 1

# ============================================================
# RISK MANAGEMENT
# ============================================================
risk:
  initial_capital: 150000.0          # Starting account size ($)
  max_daily_loss_hard_stop: 2500.0   # Bot stops ALL trading at this daily loss
  max_daily_loss_alert: 1500.0       # Bot sends alert at this daily loss
  max_trailing_drawdown: 3000.0      # Bot pauses at this trailing drawdown
  max_correlated_positions: 2        # Max long or short positions in correlated markets
  topstep_daily_limit: 4500.0        # Topstep's actual daily limit (NEVER exceed)
  topstep_trailing_drawdown: 4500.0  # Topstep's trailing drawdown limit

# ============================================================
# BACKTESTING PARAMETERS
# ============================================================
backtest:
  start_date: "2000-01-01"
  end_date: "2024-12-31"
  in_sample_end: "2018-12-31"        # Walk-forward training cutoff
  benchmark_symbol: "SPY"            # Comparison benchmark

# ============================================================
# LIVE TRADING
# ============================================================
live:
  broker: "ninjatrader"              # Options: "ninjatrader", "ibkr"
  paper_mode: true                   # ALWAYS start in paper mode
  data_feed: "rithmic"               # Options: "rithmic", "ibkr"
  check_interval_seconds: 5          # How often to check for signals

# ============================================================
# ALERTS
# ============================================================
alerts:
  telegram_enabled: false            # Enable after setting up bot token
  email_enabled: false
  alert_on_trade: true
  alert_on_daily_loss_threshold: true
  alert_on_error: true
```

**Status:** [ ] Complete

---

### STEP 10 — Verify Complete Environment

Run this verification script to confirm everything is correctly installed:

**Create and run:** `python verify_setup.py`

```python
"""
AlgoBot Environment Verification Script
Run this to confirm all dependencies are correctly installed.
"""

import sys
import importlib

def check_import(module_name, display_name=None):
    """Try to import a module and report success or failure."""
    name = display_name or module_name
    try:
        importlib.import_module(module_name)
        print(f"  [OK]  {name}")
        return True
    except ImportError as e:
        print(f"  [FAIL] {name} — {e}")
        return False

def main():
    print("\n" + "="*50)
    print("  ALGOBOT ENVIRONMENT VERIFICATION")
    print("="*50)

    print(f"\nPython version: {sys.version}")
    print(f"Expected: 3.11.x\n")

    modules = [
        ("numpy", "NumPy"),
        ("pandas", "Pandas"),
        ("scipy", "SciPy"),
        ("matplotlib", "Matplotlib"),
        ("plotly", "Plotly"),
        ("vectorbt", "VectorBT"),
        ("empyrical", "Empyrical"),
        ("statsmodels", "StatsModels"),
        ("pandas_ta", "Pandas-TA"),
        ("yfinance", "yFinance"),
        ("fredapi", "FRED API"),
        ("yaml", "PyYAML"),
        ("loguru", "Loguru"),
        ("jinja2", "Jinja2"),
        ("sklearn", "Scikit-Learn"),
        ("requests", "Requests"),
    ]

    results = []
    print("Checking required libraries:")
    for mod, name in modules:
        results.append(check_import(mod, name))

    passed = sum(results)
    total = len(results)

    print(f"\n{'='*50}")
    print(f"  RESULT: {passed}/{total} libraries verified")
    if passed == total:
        print("  STATUS: ALL CLEAR — Environment is ready")
    else:
        print("  STATUS: ISSUES FOUND — Install missing libraries before proceeding")
    print("="*50 + "\n")

if __name__ == "__main__":
    main()
```

**Expected output when setup is complete:**
```
==================================================
  ALGOBOT ENVIRONMENT VERIFICATION
==================================================

Python version: 3.11.x

Checking required libraries:
  [OK]  NumPy
  [OK]  Pandas
  [OK]  SciPy
  [OK]  Matplotlib
  [OK]  Plotly
  [OK]  VectorBT
  [OK]  Empyrical
  [OK]  StatsModels
  [OK]  Pandas-TA
  [OK]  yFinance
  [OK]  FRED API
  [OK]  PyYAML
  [OK]  Loguru
  [OK]  Jinja2
  [OK]  Scikit-Learn
  [OK]  Requests

==================================================
  RESULT: 16/16 libraries verified
  STATUS: ALL CLEAR — Environment is ready
==================================================
```

**Status:** [ ] Verification script passes with 16/16

---

## Results

Document your results here after completing each step:

| Step | Status | Notes |
|---|---|---|
| 1 — Miniconda | | |
| 2 — Virtual Environment | | |
| 3 — Python Libraries | | |
| 4 — VS Code | | |
| 5 — Git | | |
| 6 — GitHub | | |
| 7 — API Keys | | |
| 8 — .env File | | |
| 9 — Config File | | |
| 10 — Verification | | |

---

## Issues Encountered

*Document any errors or problems here as you work through setup.*

---

## Conclusions

Environment setup is complete when:
- [ ] All 10 steps above show Status: Complete
- [ ] Verification script passes 16/16
- [ ] GitHub repository shows all project files
- [ ] VS Code opens project and recognizes algobot_env interpreter

---

## Next Steps

On completion of LAB_001, proceed to:

**→ LAB_002 — Data Infrastructure**

In that lab we will:
1. Connect to Yahoo Finance and download proxy data for all 5 markets
2. Connect to FRED and download macro data
3. Build the data cleaning pipeline
4. Verify data quality and generate first data report

---

*LAB_001 — AlgoBot Environment Setup*
*Phase 0 — Foundation*
