"""
AlgoBot -- Paper Trading Loop
==============================
Script:  scripts/run_paper_trading.py
Phase:   6 -- Paper Trading
Purpose: Run ORB + FHB signals live using IBKR paper account.
         Generates signals with the same logic as backtests and submits
         bracket orders to TWS paper trading account.

Prerequisites:
    1. TWS (paper) is open and API is enabled (port 7497)
       TWS -> Edit -> Global Config -> API -> Settings
       - Enable ActiveX and Socket Clients = ON
       - Read-Only API = OFF
       - Port = 7497
       - Trusted IP = 127.0.0.1

    2. IBKR market data subscription for US Futures (free with paper account)
       TWS -> Account -> Settings -> Market Data Subscriptions
       - Add "US Futures (CME, CBOT, NYMEX, COMEX)"

    3. Run this script after 09:00 ET on a US trading day:
       conda run -n algobot_env python scripts/run_paper_trading.py

Schedule (all times US Eastern):
    09:00  Bot starts, connects to IBKR, loads HTF data
    09:45  ORB check (ES + NQ)
    10:30  FHB check (ES + NQ)
    16:00  EOD: cancel remaining orders, print daily summary, exit

Note on Sierra Chart:
    Sierra Chart is NOT used by the bot for data or execution.
    It runs alongside as a visual monitoring tool.
    The bot gets all data from IBKR via ib_insync.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, date
from pathlib import Path

import pytz
import yaml
import pandas as pd

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import asyncio
import subprocess

from src.execution.ibkr_bridge import IBKRBridge
from src.execution.live_signal_engine import LiveSignalEngine
from src.utils.econ_calendar import EconCalendar
from src.utils.vix_filter import VIXFilter
from src.utils.trade_readiness import GreenLightScore
from src.utils.trade_db import TradeDB
from src.utils.logger import get_logger
from run_fhb_backtest import FHB_GLS_HALF_SCORE, FHB_GLS_MIN_SCORE
from dashboard.bot_state import (
    update_daily_pnl, update_positions,
    set_bot_running, reset_daily as reset_daily_state,
)

log = get_logger(__name__)
ET  = pytz.timezone("America/New_York")


# ============================================================
# CONFIGURATION
# ============================================================

MARKETS       = ["ES", "NQ"]    # Equity index markets
GC_MARKETS    = ["GC"]          # Gold mean-reversion sub-bot
ORB_CHECK_H   = 9               # ORB check hour (ET)
ORB_CHECK_M   = 45              # ORB check minute (ET)
FHB_CHECK_H   = 10              # FHB + GC check hour (ET)
FHB_CHECK_M   = 30              # FHB + GC check minute (ET)
EOD_H         = 16              # End-of-day hour (ET)
LOOP_SLEEP_S  = 20              # Main loop sleep interval (seconds)
START_H       = 9               # Do not start signal checks before this hour
POSITION_SYNC_EVERY = 15        # Sync open positions to dashboard every N loops
RECONNECT_ATTEMPTS  = 3         # Number of reconnect tries on disconnect


# ============================================================
# HELPERS
# ============================================================

def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def now_et() -> datetime:
    return datetime.now(ET)


def is_trading_day() -> bool:
    """Return True if today is Monday-Friday (basic check)."""
    return now_et().weekday() < 5


def time_passed(h: int, m: int, now: datetime) -> bool:
    """Return True if current time is at or past h:m ET."""
    return (now.hour, now.minute) >= (h, m)


def time_window(h: int, m: int, window_min: int, now: datetime) -> bool:
    """Return True if current time is within [h:m, h:m + window_min)."""
    t = now.hour * 60 + now.minute
    target = h * 60 + m
    return target <= t < target + window_min


# ============================================================
# DAILY P&L TRACKER (hard stop enforcement)
# ============================================================

class DailyPnLTracker:
    """
    Tracks real-time daily P&L from fill callbacks and enforces the
    Topstep-safe circuit breakers defined in config.yaml:

        daily_loss_alert_usd:    1500  -> prints a warning
        daily_loss_hard_stop_usd: 2500 -> cancels all orders, halts trading

    Usage:
        tracker = DailyPnLTracker(alert_usd=1500, hard_stop_usd=2500)
        bridge.register_fill_callback(tracker.on_fill)

        # later, in main loop before sending signals:
        if tracker.is_halted:
            continue
    """

    def __init__(self, alert_usd: float = 1_500.0, hard_stop_usd: float = 2_500.0):
        self.alert_usd     = alert_usd
        self.hard_stop_usd = hard_stop_usd
        self.pnl_today:    float      = 0.0
        self.trades_today: list[dict] = []
        self._halted       = False
        self._alerted      = False
        self._halt_cb      = None          # called with () when hard stop fires

    def register_halt_callback(self, cb) -> None:
        """Register a zero-arg function to call when the daily hard stop fires."""
        self._halt_cb = cb

    def on_fill(self, fill_info: dict) -> None:
        """
        Update running daily P&L from an exit fill.
        Called by IBKRBridge via register_fill_callback().
        Triggers alert / hard-stop logic as thresholds are crossed.
        Also writes live P&L to bot_state.json so the dashboard updates.
        """
        pnl = float(fill_info.get("pnl_net", 0.0))
        self.pnl_today += pnl
        self.trades_today.append(fill_info)

        wins   = sum(1 for t in self.trades_today if float(t.get("pnl_net", 0)) > 0)
        losses = sum(1 for t in self.trades_today if float(t.get("pnl_net", 0)) < 0)
        try:
            update_daily_pnl(self.pnl_today, len(self.trades_today), wins, losses)
        except Exception:
            pass

        print(f"\n  [FILL] P&L today: ${self.pnl_today:+,.0f} "
              f"({wins}W/{losses}L after {len(self.trades_today)} trades)")

        # Alert threshold (warning only)
        if not self._alerted and self.pnl_today <= -self.alert_usd:
            self._alerted = True
            print(f"\n  *** DAILY LOSS ALERT ***")
            print(f"  Day P&L: ${self.pnl_today:,.0f}  |  Alert level: -${self.alert_usd:,.0f}")
            print(f"  Trading continues but be cautious.")

        # Hard stop threshold
        if not self._halted and self.pnl_today <= -self.hard_stop_usd:
            self._halted = True
            print(f"\n  *** DAILY HARD STOP TRIGGERED ***")
            print(f"  Day P&L: ${self.pnl_today:,.0f} <= -${self.hard_stop_usd:,.0f}")
            print(f"  Cancelling all orders. No more signals will be sent today.")
            if self._halt_cb is not None:
                try:
                    self._halt_cb()
                except Exception as e:
                    log.warning("halt_callback error: {e}", e=e)

    def reset(self) -> None:
        """Reset for a new trading day."""
        self.pnl_today     = 0.0
        self.trades_today.clear()
        self._halted       = False
        self._alerted      = False

    @property
    def is_halted(self) -> bool:
        return self._halted


# ============================================================
# DAILY STATE
# ============================================================

class DailyState:
    def __init__(self, tracker: DailyPnLTracker):
        self._tracker = tracker
        self.reset()

    def reset(self):
        self.date        = now_et().date()
        self.orb_done    = False    # ORB check fired for today
        self.fhb_done    = False    # FHB check fired for today
        self.gc_done     = False    # GC check fired for today
        self.signals_sent: list[dict] = []
        self.signal_ids:   list[str]  = []
        self._tracker.reset()
        try:
            reset_daily_state()
        except Exception:
            pass
        print(f"\n  [DAILY RESET] New trading day: {self.date}")

    def check_rollover(self):
        if now_et().date() != self.date:
            self.reset()


# ============================================================
# MAIN PAPER TRADING LOOP
# ============================================================

# Rotate through client IDs so a stale TWS session with ID=22 doesn't block startup.
# TWS reports "clientId already in use" when a previous session crashed without disconnecting.
_CLIENT_IDS = [22, 23, 24, 25]


def _connect_bridge(db_path: str) -> IBKRBridge:
    """Attempt to connect to TWS, rotating client IDs to avoid stale-session conflicts."""
    for attempt in range(1, RECONNECT_ATTEMPTS + 1):
        client_id = _CLIENT_IDS[(attempt - 1) % len(_CLIENT_IDS)]
        bridge = IBKRBridge(port=7497, paper=True, db_path=db_path, client_id=client_id)
        if bridge.connect(timeout=10):
            return bridge
        print(f"  Connection attempt {attempt}/{RECONNECT_ATTEMPTS} "
              f"(clientId={client_id}) failed. Retrying in 15s...")
        time.sleep(15)
    return None


def _refresh_dashboard():
    """Run generate_dashboard_data.py in background to update dashboard cache."""
    try:
        script = PROJECT_ROOT / "scripts" / "generate_dashboard_data.py"
        subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("  Dashboard data refresh started in background.")
    except Exception as e:
        log.warning("Dashboard refresh failed: {e}", e=e)


def main():
    print("\n" + "=" * 65)
    print("  AlgoBot -- Paper Trading Loop (Phase 6)")
    print(f"  Markets: {', '.join(MARKETS)} + {', '.join(GC_MARKETS)} (GC)")
    print("  Press Ctrl+C to stop")
    print("=" * 65)

    if not is_trading_day():
        print("\n  Today is not a trading day (weekend). Exiting.")
        return

    # Reset any stale bot_running=True left over from a previous crashed session.
    # This ensures the dashboard shows the correct state throughout startup.
    try:
        set_bot_running(False)
    except Exception:
        pass

    config = load_config()

    # ── Step 1: Init filters ─────────────────────────────────────────────────
    print("\n[1/4] Initialising filters...")
    econ_cal   = EconCalendar()
    vix_filter = VIXFilter.from_yahoo(
        start="2023-01-01",
        end=now_et().strftime("%Y-%m-%d"),
    )
    gls_engine = GreenLightScore(
        full_size_threshold=FHB_GLS_HALF_SCORE,
        half_size_threshold=FHB_GLS_MIN_SCORE,
    )
    db_path = str(PROJECT_ROOT / "data" / "trades.db")
    db      = TradeDB(db_path)
    print("  EconCalendar, VIXFilter, GreenLightScore, TradeDB ready")

    risk_cfg      = config.get("risk", {})
    alert_usd     = float(risk_cfg.get("daily_loss_alert_usd",    1_500.0))
    hard_stop_usd = float(risk_cfg.get("daily_loss_hard_stop_usd", 2_500.0))
    tracker       = DailyPnLTracker(alert_usd=alert_usd, hard_stop_usd=hard_stop_usd)
    print(f"  DailyPnLTracker: alert=${alert_usd:,.0f} | hard_stop=${hard_stop_usd:,.0f}")

    # ── Step 2: Connect to IBKR ──────────────────────────────────────────────
    print("\n[2/4] Connecting to IBKR TWS (port 7497)...")
    bridge = _connect_bridge(db_path)
    if bridge is None:
        print("\n  FATAL: Could not connect to TWS after all attempts.")
        print("  Make sure TWS is open with API enabled on port 7497.")
        return

    account_value = bridge.get_account_value()
    print(f"  Connected: account={bridge._ib.managedAccounts()} | balance=${account_value:,.0f}")

    tracker.register_halt_callback(bridge.cancel_all)
    bridge.register_fill_callback(tracker.on_fill)

    _eng_holder: list = []
    def _on_close(fill_info: dict) -> None:
        if _eng_holder:
            _eng_holder[0].record_trade_result(float(fill_info.get("pnl_net", 0.0)))
    bridge.register_fill_callback(_on_close)

    # ── Step 3: Load HTF data ─────────────────────────────────────────────────
    print("\n[3/4] Loading HTF bias data...")
    engine = LiveSignalEngine(
        ib         = bridge._ib,
        config     = config,
        econ_cal   = econ_cal,
        vix_filter = vix_filter,
        gls_engine = gls_engine,
    )
    _eng_holder.append(engine)
    all_markets = MARKETS + GC_MARKETS
    engine.load_htf(all_markets)
    for m in all_markets:
        bias = engine._htf_today(m)
        print(f"  {m}: HTF bias = {bias}")

    # ── Mark bot as running in dashboard ─────────────────────────────────────
    try:
        set_bot_running(True)
    except Exception:
        pass

    # ── Step 4: Main loop ─────────────────────────────────────────────────────
    print("\n[4/4] Starting signal loop...")
    print(f"  ORB  check at {ORB_CHECK_H:02d}:{ORB_CHECK_M:02d} ET  (ES, NQ)")
    print(f"  FHB  check at {FHB_CHECK_H:02d}:{FHB_CHECK_M:02d} ET  (ES, NQ)")
    print(f"  GC   check at {FHB_CHECK_H:02d}:{FHB_CHECK_M:02d} ET  (Gold mean-reversion)")
    print(f"  EOD  at {EOD_H:02d}:00 ET")

    state     = DailyState(tracker)
    loop_tick = 0
    now       = now_et()

    if time_passed(ORB_CHECK_H, ORB_CHECK_M + 15, now):
        state.orb_done = True
        print("  NOTE: Started after ORB window -- ORB skipped for today")
    if time_passed(FHB_CHECK_H, FHB_CHECK_M + 15, now):
        state.fhb_done = True
        state.gc_done  = True
        print("  NOTE: Started after FHB/GC window -- FHB + GC skipped for today")

    try:
        while True:
            state.check_rollover()
            now       = now_et()
            loop_tick += 1

            # ── Reconnection watchdog ────────────────────────────────────────
            if not bridge.is_connected():
                print(f"\n  [WARN] Lost IBKR connection. Reconnecting...")
                try:
                    bridge.disconnect()
                except Exception:
                    pass
                bridge = _connect_bridge(db_path)
                if bridge is None:
                    print("  Could not reconnect. Waiting 60s before retry...")
                    time.sleep(60)
                    continue
                engine.ib = bridge._ib
                tracker.register_halt_callback(bridge.cancel_all)
                bridge.register_fill_callback(tracker.on_fill)
                bridge.register_fill_callback(_on_close)
                print("  Reconnected successfully.")

            # ── EOD ─────────────────────────────────────────────────────────
            if now.hour >= EOD_H:
                print(f"\n[{now.strftime('%H:%M')}] EOD -- cancelling open orders")
                bridge.cancel_all()
                _print_daily_summary(state, bridge, db)
                print("\n  Refreshing dashboard with today's data...")
                _refresh_dashboard()
                print("  Session complete. Exiting.")
                break

            # ── ORB check at 09:45 ──────────────────────────────────────────
            if (not state.orb_done and
                    time_window(ORB_CHECK_H, ORB_CHECK_M, 10, now)):
                state.orb_done = True
                if tracker.is_halted:
                    print(f"\n[{now.strftime('%H:%M')}] ORB skipped -- daily hard stop active")
                else:
                    print(f"\n[{now.strftime('%H:%M')}] ORB check (ES + NQ)...")
                    for market in MARKETS:
                        signal = engine.check_orb(market)
                        if signal:
                            _handle_signal(signal, bridge, state, account_value)
                        else:
                            print(f"  {market}: No ORB signal")

            # ── FHB + GC check at 10:30 ─────────────────────────────────────
            if (not state.fhb_done and
                    time_window(FHB_CHECK_H, FHB_CHECK_M, 10, now)):
                state.fhb_done = True
                if tracker.is_halted:
                    print(f"\n[{now.strftime('%H:%M')}] FHB skipped -- daily hard stop active")
                else:
                    print(f"\n[{now.strftime('%H:%M')}] FHB check (ES + NQ)...")
                    for market in MARKETS:
                        signal = engine.check_fhb(market)
                        if signal:
                            _handle_signal(signal, bridge, state, account_value)
                        else:
                            print(f"  {market}: No FHB signal")

            if (not state.gc_done and
                    time_window(FHB_CHECK_H, FHB_CHECK_M, 10, now)):
                state.gc_done = True
                if not tracker.is_halted:
                    print(f"\n[{now.strftime('%H:%M')}] GC mean-reversion check...")
                    signal = engine.check_gc()
                    if signal:
                        _handle_signal(signal, bridge, state, account_value)
                    else:
                        print("  GC: No signal today")

            # ── Position sync to dashboard (every N loops) ──────────────────
            if loop_tick % POSITION_SYNC_EVERY == 0:
                try:
                    positions = bridge.get_open_positions()
                    update_positions(positions)
                    if now.minute % 30 == 0:
                        print(f"  [{now.strftime('%H:%M')}] Positions: {len(positions)} | "
                              f"Signals: {len(state.signals_sent)} | "
                              f"Day P&L: ${tracker.pnl_today:+,.0f}")
                except Exception:
                    pass

            try:
                bridge._ib.sleep(LOOP_SLEEP_S)
            except (ConnectionError, Exception):
                pass  # reconnect watchdog at top of loop handles it

    except KeyboardInterrupt:
        print("\n\n  [CTRL+C] Stopping bot...")
        bridge.cancel_all()
        _print_daily_summary(state, bridge, db)

    finally:
        try:
            set_bot_running(False)
        except Exception:
            pass
        db.close()
        bridge.disconnect()
        print("  IBKR disconnected. Goodbye.\n")


# ============================================================
# SIGNAL HANDLING
# ============================================================

def _handle_signal(
    signal:        dict,
    bridge:        IBKRBridge,
    state:         DailyState,
    account_value: float,
) -> None:
    m  = signal["market"]
    d  = signal["direction"]
    st = signal["strategy"]
    e  = signal["entry"]
    s  = signal["stop"]
    t  = signal["target"]
    sz = signal.get("size_mult", 1.0)

    risk_pts   = abs(e - s)
    point_val  = {"ES": 50.0, "NQ": 20.0, "GC": 100.0}.get(m, 50.0)
    risk_usd   = risk_pts * point_val
    risk_pct   = risk_usd / account_value * 100 if account_value > 0 else 0

    print(f"\n  *** {st} SIGNAL: {m} {d} ***")
    print(f"  Entry:   {e}")
    print(f"  Stop:    {s}")
    print(f"  Target:  {t}")
    print(f"  Size:    {sz:.0%} | Risk: ${risk_usd:,.0f} ({risk_pct:.1f}%)")
    print(f"  GLS:     {signal.get('gls_score', 'N/A')} | "
          f"OF: {signal.get('of_score', 'N/A')}")

    signal_id = bridge.submit_signal(signal)
    if signal_id:
        state.signals_sent.append(signal)
        state.signal_ids.append(signal_id)
        print(f"  ORDER SUBMITTED: id={signal_id}")
    else:
        print(f"  WARNING: Order submission failed")


# ============================================================
# EOD SUMMARY
# ============================================================

def _print_daily_summary(
    state:  DailyState,
    bridge: IBKRBridge,
    db:     TradeDB,
) -> None:
    print("\n" + "=" * 65)
    print(f"  DAILY SUMMARY -- {state.date}")
    print("=" * 65)
    print(f"  Signals sent today: {len(state.signals_sent)}")
    for sig in state.signals_sent:
        print(f"    {sig['strategy']} {sig['market']} {sig['direction']} "
              f"E={sig['entry']} S={sig['stop']} T={sig['target']}")

    # Pull today's closed trades from TradeDB
    try:
        summary = db.get_daily_summary(last_n_days=1)
        if summary:
            row = summary[0]
            if str(row.get("trade_date", "")) == str(state.date):
                print(f"\n  Closed trades: {row.get('wins', 0) + row.get('losses', 0)}")
                print(f"  Wins / Losses: {row.get('wins', 0)} / {row.get('losses', 0)}")
                print(f"  Net P&L:       ${row.get('pnl_net', 0):,.2f}")
    except Exception:
        pass

    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
