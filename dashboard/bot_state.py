"""
AlgoBot -- Bot State Manager
==============================
Module:  dashboard/bot_state.py
Purpose: Persist and serve bot operating state across the dashboard and
         live trading engine. State is stored in dashboard/cache/bot_state.json
         so it survives server restarts.

State includes:
  - risk_mode       : "safe" | "medium" | "hardcore"
  - account_override: float | None  (None = use live IBKR balance)
  - bot_running     : bool
  - paper_mode      : bool
  - daily_pnl       : float  (updated by the live engine)
  - open_positions  : list
  - last_updated    : ISO timestamp
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

STATE_FILE = Path(__file__).parent / "cache" / "bot_state.json"

_lock = threading.Lock()

# Default state (first run)
_DEFAULTS: dict[str, Any] = {
    "risk_mode":        "safe",    # Default to safe for TopStep $50k account
    "trading_mode":     "tv_paper", # "ibkr" | "tv_paper"
    "account_override": None,      # None = pull from IBKR live (ibkr mode) / config (tv_paper mode)
    "bot_running":      False,
    "paper_mode":       True,
    "daily_pnl":        0.0,
    "daily_trades":     0,
    "daily_wins":       0,
    "daily_losses":     0,
    "open_positions":   [],
    "pending_tv_signals": [],      # IPC queue: webhook → run_tv_paper_trading.py
    "last_updated":     datetime.now().isoformat(timespec="seconds"),
    # Strategy improvement plugins (each can be toggled from the web panel)
    "strategy_flags": {
        "vix_filter":          True,   # Skip HIGH_VOL and QUIET VIX regimes
        "econ_filter":         True,   # Skip high-impact news days (FOMC, NFP)
        "gls_gate":            True,   # GreenLightScore quality gate (40/60)
        "htf_bias_gate":       True,   # Weekly/monthly HTF bias gate (core edge)
        "half_stop":           True,   # Priority 1: exit 50% at -0.3R
        "consecutive_loss_pause": False,  # Pause after 3 consecutive losses/day
        "overnight_carry":     True,   # Carry profitable FHB trades overnight
        "vwap_filter":         True,   # Require price on correct VWAP side
        "delta_filter":        True,   # Require delta confirmation
    },
}

# Risk mode definitions (mirrors config.yaml)
RISK_MODES: dict[str, dict] = {
    "safe": {
        "label":                   "Safe — TopStep $50k",
        "description":             "1 micro contract max. TopStep $50k compliant. $1k daily limit / $2k trailing DD.",
        "risk_per_trade_pct":      0.50,
        "max_contracts":           1,
        "daily_loss_cap_usd":      900.0,
        "trailing_dd_cap_usd":     1800.0,
        "max_loss_per_trade_usd":  400.0,
        "color":                   "#00c878",   # green
    },
    "medium": {
        "label":                   "Medium",
        "description":             "Standard config. FHB PF 2.87 | Win 63.5% | Max DD -$7.6k (v3 comprehensive)",
        "risk_per_trade_pct":      1.00,
        "max_contracts":           3,
        "daily_loss_cap_usd":      2500.0,
        "trailing_dd_cap_usd":     3000.0,
        "max_loss_per_trade_usd":  2000.0,
        "color":                   "#f59e0b",   # amber
    },
    "hardcore": {
        "label":                   "Hardcore",
        "description":             "Max sizing. 5 contracts. $3,800 daily cap — $700 Topstep buffer.",
        "risk_per_trade_pct":      2.00,
        "max_contracts":           5,
        "daily_loss_cap_usd":      3800.0,
        "trailing_dd_cap_usd":     4000.0,
        "max_loss_per_trade_usd":  3000.0,
        "color":                   "#ff3b5c",   # red
    },
}


def _read() -> dict:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        _write(_DEFAULTS.copy())
        return _DEFAULTS.copy()
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _DEFAULTS.copy()


def _write(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_state() -> dict:
    """Return full bot state dict."""
    with _lock:
        s = _read()
        # Always attach the risk modes catalogue so the frontend can use it
        s["risk_modes"] = RISK_MODES
        s["active_mode_config"] = RISK_MODES.get(s.get("risk_mode", "medium"), RISK_MODES["medium"])
        return s


def set_risk_mode(mode: str) -> dict:
    """Set active risk mode. Returns updated state."""
    if mode not in RISK_MODES:
        raise ValueError(f"Unknown risk mode: {mode!r}. Choose from {list(RISK_MODES)}")
    with _lock:
        s = _read()
        s["risk_mode"]    = mode
        s["last_updated"] = datetime.now().isoformat(timespec="seconds")
        _write(s)
    return get_state()


def set_account_override(balance: Optional[float]) -> dict:
    """
    Set manual account balance override.
    Pass None to revert to live IBKR balance.
    """
    with _lock:
        s = _read()
        s["account_override"] = balance
        s["last_updated"]     = datetime.now().isoformat(timespec="seconds")
        _write(s)
    return get_state()


def set_trading_mode(mode: str) -> dict:
    """Set trading mode: 'ibkr' for TWS, 'tv_paper' for TradingView webhook + paper simulator."""
    if mode not in ("ibkr", "tv_paper"):
        raise ValueError(f"Unknown trading mode: {mode!r}. Choose 'ibkr' or 'tv_paper'.")
    with _lock:
        s = _read()
        s["trading_mode"] = mode
        s["last_updated"] = datetime.now().isoformat(timespec="seconds")
        _write(s)
    return get_state()


def get_trading_mode() -> str:
    """Return current trading mode ('ibkr' or 'tv_paper')."""
    return _read().get("trading_mode", "ibkr")


def set_bot_running(running: bool) -> dict:
    with _lock:
        s = _read()
        s["bot_running"]  = running
        s["last_updated"] = datetime.now().isoformat(timespec="seconds")
        _write(s)
    return get_state()


def update_daily_pnl(pnl: float, trades: int = 0, wins: int = 0, losses: int = 0) -> None:
    """Called by the live engine to update today's running P&L."""
    with _lock:
        s = _read()
        s["daily_pnl"]    = round(pnl, 2)
        s["daily_trades"] = trades
        s["daily_wins"]   = wins
        s["daily_losses"] = losses
        s["last_updated"] = datetime.now().isoformat(timespec="seconds")
        _write(s)


def reset_daily() -> None:
    """Reset daily counters — called at market open or midnight."""
    with _lock:
        s = _read()
        s["daily_pnl"]       = 0.0
        s["daily_trades"]    = 0
        s["daily_wins"]      = 0
        s["daily_losses"]    = 0
        s["open_positions"]  = []
        s["last_updated"]    = datetime.now().isoformat(timespec="seconds")
        _write(s)


def set_strategy_flags(flags: dict) -> dict:
    """
    Update one or more strategy plugin flags.
    Only keys present in the flags dict are updated; others are preserved.
    Returns updated full state.
    """
    valid_keys = set(_DEFAULTS["strategy_flags"].keys())
    with _lock:
        s = _read()
        current_flags = s.get("strategy_flags", _DEFAULTS["strategy_flags"].copy())
        for key, val in flags.items():
            if key in valid_keys:
                current_flags[key] = bool(val)
        s["strategy_flags"] = current_flags
        s["last_updated"] = datetime.now().isoformat(timespec="seconds")
        _write(s)
    return get_state()


def get_strategy_flags() -> dict:
    """Return current strategy flag settings."""
    s = _read()
    return s.get("strategy_flags", _DEFAULTS["strategy_flags"].copy())


def update_positions(positions: list) -> None:
    with _lock:
        s = _read()
        s["open_positions"] = positions
        s["last_updated"]   = datetime.now().isoformat(timespec="seconds")
        _write(s)


def get_active_mode_config() -> dict:
    """Return the full risk parameters for the currently active mode."""
    s = _read()
    mode = s.get("risk_mode", "medium")
    return RISK_MODES.get(mode, RISK_MODES["medium"])


def compute_position_size(
    account_balance: float,
    market: str,
    atr_pts: float,
    point_value: float,
) -> dict:
    """
    Compute position sizes for all three modes given live market data.
    Returns a dict with 'safe', 'medium', 'hardcore' size recommendations.
    """
    results = {}
    for mode_name, mode in RISK_MODES.items():
        risk_usd   = account_balance * mode["risk_per_trade_pct"] / 100
        risk_usd   = min(risk_usd, mode["max_loss_per_trade_usd"])
        dollar_per_contract = atr_pts * 2.5 * point_value   # 2.5x ATR stop
        if dollar_per_contract > 0:
            raw_size = risk_usd / dollar_per_contract
        else:
            raw_size = 1
        size = max(1, min(int(raw_size), mode["max_contracts"]))
        results[mode_name] = {
            "contracts":       size,
            "risk_usd":        round(risk_usd, 0),
            "dollar_per_ct":   round(dollar_per_contract, 0),
            "max_loss_usd":    round(size * dollar_per_contract, 0),
        }
    return results
