"""
AlgoBot -- TradingView Paper Trading Loop
==========================================
Script:  scripts/run_tv_paper_trading.py
Purpose: Run the paper trading bot WITHOUT IBKR TWS.

Signal flow:
  TradingView Pine Script alert
      → POST /api/webhook/signal  (ngrok → FastAPI)
          → bot_state.json["pending_tv_signals"]   (file IPC queue)
              → this script drains queue every 5 s
                  → PaperSimulator.submit_signal()
                      → positions tracked in-memory + bot_state.json
                      → yfinance polls stop/target every 60 s
                      → TradeDB.log_trade_exit() on fill
                      → DailyPnLTracker updates dashboard P&L

Prerequisites:
  1. ngrok running: ngrok http 8000
  2. Dashboard server running: uvicorn dashboard.server:app ...
  3. Trading mode set to tv_paper in dashboard (POST /api/bot/set_mode)
  4. TradingView alerts configured with ngrok URL + secret

Start:
    python scripts/run_tv_paper_trading.py

Schedule (all times US Eastern):
    Any time  Bot polls for pending signals from TradingView every 5 s
    09:45     ORB window opens (Pine fires on 9:45 bar close)
    10:30     FHB / GC window opens (Pine fires on 10:30 bar close)
    10:31     CL window opens
    16:05     EOD: settle all open positions at last yfinance price, exit
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, date
from pathlib import Path

import pytz
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

WEBHOOK_LOG = PROJECT_ROOT / "logs" / "webhook_signals.jsonl"

from src.execution.paper_simulator import PaperSimulator
from src.utils.account_state import AccountState
from src.utils.trade_db import TradeDB
from src.utils.logger import get_logger
from src.utils.telegram_notifier import (
    TelegramNotifier,
    fmt_startup, fmt_loss_alert, fmt_hard_stop, fmt_eod,
    fmt_dd_warning, fmt_dd_halt,
)
from dashboard.bot_state import (
    update_daily_pnl, update_positions,
    set_bot_running, reset_daily as reset_daily_state,
    STATE_FILE, _lock as _bs_lock,
)

log = get_logger(__name__)
ET  = pytz.timezone("America/New_York")


# ============================================================
# CONFIGURATION
# ============================================================

LOOP_SLEEP_S = 5       # Poll pending_tv_signals every 5 seconds
EOD_H        = 16      # End-of-day hour ET
EOD_M        = 5       # End-of-day minute ET (match PaperSimulator default)

# ── NYSE Holidays 2026–2028 ───────────────────────────────────────────────────
_NYSE_HOLIDAYS: set = {
    # 2026
    date(2026, 1,  1),   # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4,  3),   # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7,  3),   # Independence Day (observed)
    date(2026, 9,  7),   # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1,  1),   # New Year's Day
    date(2027, 1, 18),   # MLK Day
    date(2027, 2, 15),   # Presidents' Day
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed)
    date(2027, 7,  5),   # Independence Day (observed)
    date(2027, 9,  6),   # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (observed)
    # 2028
    date(2028, 1, 17),   # MLK Day  (Jan 1 falls on Sat → no extra holiday)
    date(2028, 2, 21),   # Presidents' Day
    date(2028, 4, 14),   # Good Friday
    date(2028, 5, 29),   # Memorial Day
    date(2028, 6, 19),   # Juneteenth
    date(2028, 7,  4),   # Independence Day
    date(2028, 9,  4),   # Labor Day
    date(2028, 11, 23),  # Thanksgiving
    date(2028, 12, 25),  # Christmas
}


# ============================================================
# HELPERS
# ============================================================

def _log_signal_receipt(signal: dict) -> None:
    """Append a received signal to logs/webhook_signals.jsonl for audit trail."""
    try:
        WEBHOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "drained_at": datetime.now(ET).isoformat(timespec="seconds"),
            **signal,
        }
        with WEBHOOK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning("_log_signal_receipt: {e}", e=e)


def load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "config.yaml") as f:
        return yaml.safe_load(f)


def now_et() -> datetime:
    return datetime.now(ET)


def is_trading_day() -> bool:
    today = now_et().date()
    if today.weekday() >= 5:
        return False
    return today not in _NYSE_HOLIDAYS


def _drain_signal_queue() -> list[dict]:
    """
    Atomically read and clear pending_tv_signals from bot_state.json.
    Uses the same bot_state lock held by the webhook handler so the
    read→clear is never interleaved with an incoming webhook write.
    Returns the list of queued signals (may be empty).
    """
    with _bs_lock:
        try:
            raw = STATE_FILE.read_text(encoding="utf-8")
            s   = json.loads(raw)
            signals = list(s.get("pending_tv_signals", []))
            if signals:
                s["pending_tv_signals"] = []
                STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
                for sig in signals:
                    _log_signal_receipt(sig)
            return signals
        except Exception as e:
            log.warning("_drain_signal_queue: {e}", e=e)
            return []


# ============================================================
# DAILY P&L TRACKER  (identical to run_paper_trading.py)
# ============================================================

class DailyPnLTracker:
    def __init__(self, alert_usd: float = 1_500.0, hard_stop_usd: float = 2_500.0):
        self.alert_usd     = alert_usd
        self.hard_stop_usd = hard_stop_usd
        self.pnl_today:    float      = 0.0
        self.trades_today: list[dict] = []
        self._halted       = False
        self._alerted      = False
        self._halt_cb      = None
        self._notifier: "TelegramNotifier | None" = None

    def set_notifier(self, notifier: "TelegramNotifier") -> None:
        self._notifier = notifier

    def register_halt_callback(self, cb) -> None:
        self._halt_cb = cb

    def on_fill(self, fill_info: dict) -> None:
        pnl = float(fill_info.get("pnl_net", 0.0))
        self.pnl_today += pnl
        self.trades_today.append(fill_info)

        wins   = sum(1 for t in self.trades_today if float(t.get("pnl_net", 0)) > 0)
        losses = sum(1 for t in self.trades_today if float(t.get("pnl_net", 0)) < 0)
        try:
            update_daily_pnl(self.pnl_today, len(self.trades_today), wins, losses)
        except Exception as e:
            log.warning("DailyPnLTracker: dashboard sync failed: {e}", e=e)

        print(f"\n  [FILL] P&L today: ${self.pnl_today:+,.0f} "
              f"({wins}W/{losses}L after {len(self.trades_today)} trades)")

        if not self._alerted and self.pnl_today <= -self.alert_usd:
            self._alerted = True
            print(f"\n\a  {'!'*55}")
            print(f"  !! DAILY LOSS ALERT  —  ${self.pnl_today:+,.0f}")
            print(f"  Alert level: -${self.alert_usd:,.0f}  |  "
                  f"Remaining before stop: ${abs(self.hard_stop_usd - abs(self.pnl_today)):,.0f}")
            print(f"  Trading continues. Tighten risk.")
            print(f"  {'!'*55}")
            if self._notifier:
                self._notifier.send(fmt_loss_alert(self.pnl_today, self.hard_stop_usd))

        if not self._halted and self.pnl_today <= -self.hard_stop_usd:
            self._halted = True
            print(f"\n\a  {'#'*55}")
            print(f"  ## DAILY HARD STOP TRIGGERED  —  ${self.pnl_today:+,.0f}")
            print(f"  CANCELLING ALL ORDERS. No new signals today.")
            print(f"  {'#'*55}")
            if self._notifier:
                self._notifier.send(fmt_hard_stop(self.pnl_today))
            if self._halt_cb is not None:
                try:
                    self._halt_cb()
                except Exception as e:
                    log.warning("halt_callback error: {e}", e=e)

    def reset(self) -> None:
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
        self.orb_done    = False
        self.fhb_done    = False
        self.gc_done     = False
        self.cl_done     = False
        self.signal_ids: list[str] = []
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
# SIGNAL FILTER
# ============================================================

def _is_signal_enabled(signal: dict, cfg: dict) -> tuple[bool, str]:
    """
    Check config.tv_paper.enabled_signals to decide whether to process a signal.
    Checks specific key "STRATEGY_MARKET" first, then general "STRATEGY".
    Returns (enabled, reason_if_disabled).
    """
    enabled  = cfg.get("tv_paper", {}).get("enabled_signals", {})
    strategy = signal.get("strategy", "").upper()
    market   = signal.get("market",   "").upper()

    key_specific = f"{strategy}_{market}"
    key_general  = strategy

    if key_specific in enabled:
        if not enabled[key_specific]:
            return False, f"{key_specific} disabled in config (TV backtest: net loser)"
    elif key_general in enabled:
        if not enabled[key_general]:
            return False, f"{key_general} disabled in config (TV backtest: net loser)"

    return True, ""


# ============================================================
# SIGNAL HANDLER
# ============================================================

def _handle_signal(
    signal: dict,
    sim: PaperSimulator,
    state: DailyState,
    tracker: DailyPnLTracker,
    account_value: float,
    cfg: dict | None = None,
    account_state: "AccountState | None" = None,
) -> None:
    """
    Process one queued TradingView signal.
    Enforces once-per-day dedup per strategy and halted state.
    Applies consecutive-loss size reduction from AccountState.
    """
    strategy  = signal.get("strategy", "").upper()
    market    = signal.get("market",   "").upper()
    direction = signal.get("direction","").upper()

    # Strategy/market filter based on TV backtest calibration
    if cfg is not None:
        ok, reason = _is_signal_enabled(signal, cfg)
        if not ok:
            print(f"  ⊘  [{strategy}/{market}] {reason}")
            return

    # FHB SHORT direction safety filter — short side is a net loser (PF=0.918)
    # Pine v4 sends LONG only but this guard catches stale v3 alerts still firing.
    if strategy == "FHB" and direction == "SHORT":
        print(f"  ⊘  [FHB/SHORT] Rejected — short side disabled (TV backtest PF=0.918)")
        return

    if tracker.is_halted:
        print(f"  [SKIP] {strategy}/{market} — daily hard stop active")
        return

    # Once-per-day dedup per strategy
    if strategy == "ORB":
        if state.orb_done:
            print(f"  [SKIP] ORB already fired today")
            return
        state.orb_done = True
    elif strategy == "FHB":
        if state.fhb_done:
            print(f"  [SKIP] FHB already fired today")
            return
        state.fhb_done = True
    elif strategy == "GC":
        if state.gc_done:
            print(f"  [SKIP] GC already fired today")
            return
        state.gc_done = True
    elif strategy == "CL":
        if state.cl_done:
            print(f"  [SKIP] CL already fired today")
            return
        state.cl_done = True

    # Apply consecutive-loss size reduction
    if account_state is not None:
        mult = account_state.get_size_mult()
        if mult < 1.0:
            signal = dict(signal)   # don't mutate the original dict
            signal["size_mult"] = mult
            print(f"  [RISK] Size reduced to {mult:.2f}x "
                  f"({account_state.consecutive_losses} consecutive losses)")

    print(f"\n  [{strategy}] {market} {direction}  "
          f"E={signal.get('entry')}  S={signal.get('stop')}  T={signal.get('target')}")

    sid = sim.submit_signal(signal)
    if sid:
        state.signal_ids.append(sid)
        print(f"  [ENTRY] signal_id={sid}")
    else:
        print(f"  [SKIP] PaperSimulator rejected signal (risk gate or duplicate)")


# ============================================================
# MAIN ASYNC LOOP
# ============================================================

async def _main_async():
    print("\n" + "=" * 65)
    print("  AlgoBot -- TradingView Paper Trading Mode")
    print("  No IBKR TWS required. Signals via webhook.")
    print("  Press Ctrl+C to stop")
    print("=" * 65)

    if not is_trading_day():
        print("\n  Today is not a trading day (weekend / holiday). Exiting.")
        return

    try:
        set_bot_running(False)
    except Exception:
        pass

    config   = load_config()
    tv_cfg   = config.get("tv_paper", {})
    acct_bal = float(tv_cfg.get("account_balance", 50_000.0))

    risk_cfg          = config.get("risk", {})
    alert_usd         = float(risk_cfg.get("daily_loss_alert_usd",    1_500.0))
    hard_stop_usd     = float(risk_cfg.get("daily_loss_hard_stop_usd", 2_500.0))
    trailing_dd_alert = float(risk_cfg.get("trailing_dd_alert_usd",   1_500.0))
    trailing_dd_halt  = float(risk_cfg.get("trailing_dd_pause_usd",   1_800.0))

    db_path = str(PROJECT_ROOT / "data" / "trades.db")

    # ── Telegram notifier ─────────────────────────────────────────────────────
    notifier = TelegramNotifier.from_config(config)

    # ── Persistent account state (trailing DD + consecutive loss sizing) ──────
    account_state = AccountState(
        starting_balance=acct_bal,
        state_path=PROJECT_ROOT / "data" / "account_state.json",
    )
    print(f"\n  Account state loaded:")
    print(f"    Peak balance    : ${account_state.peak_balance:,.0f}")
    print(f"    Current balance : ${account_state.current_balance:,.0f}")
    print(f"    Trailing DD used: ${account_state.trailing_dd_used:,.0f} / ${trailing_dd_halt:,.0f}")
    print(f"    Consec. losses  : {account_state.consecutive_losses}  "
          f"(size mult: {account_state.get_size_mult():.2f}x)")

    # Check trailing DD at startup — do not trade if limit already breached
    dd_alerted = [False]
    dd_halted  = [False]
    _dd_alert_at_start, _dd_halt_at_start = account_state.check_trailing_dd(
        trailing_dd_alert, trailing_dd_halt
    )
    if _dd_halt_at_start:
        print(f"\n  [DD HALT] Trailing DD limit already hit "
              f"(${account_state.trailing_dd_used:,.0f} used). "
              f"Bot will not trade today.")
        dd_halted[0] = True
    elif _dd_alert_at_start:
        print(f"\n  [DD WARNING] Trailing DD at ${account_state.trailing_dd_used:,.0f} "
              f"— close to limit ${trailing_dd_halt:,.0f}")
        dd_alerted[0] = True

    tracker = DailyPnLTracker(alert_usd=alert_usd, hard_stop_usd=hard_stop_usd)
    tracker.set_notifier(notifier)

    # Crash recovery: restore today's P&L into the daily tracker so circuit
    # breakers account for any trades already done earlier today.
    today_pnl = account_state.get_today_pnl(now_et().date())
    if today_pnl != 0.0:
        tracker.pnl_today = today_pnl
        print(f"\n  [RECOVERY] Restored today's P&L from account state: ${today_pnl:+,.0f}")
        try:
            update_daily_pnl(today_pnl, 0, 0, 0)
        except Exception:
            pass

    sim     = PaperSimulator(db_path=db_path, account_balance=acct_bal)
    sim.set_notifier(notifier)

    tracker.register_halt_callback(sim.cancel_all)
    sim.register_fill_callback(tracker.on_fill)

    # ── Trailing DD fill callback ─────────────────────────────────────────────
    def _on_fill_trailing_dd(fill_info: dict) -> None:
        pnl = float(fill_info.get("pnl_net", 0.0))
        account_state.record_trade(pnl, now_et().date())

        _alert, _halt = account_state.check_trailing_dd(trailing_dd_alert, trailing_dd_halt)

        if _halt and not dd_halted[0]:
            dd_halted[0]  = True
            dd_alerted[0] = True
            dd = account_state.trailing_dd_used
            print(f"\n\a  {'#'*55}")
            print(f"  ## TRAILING DD LIMIT  —  ${dd:,.0f} consumed")
            print(f"  CANCELLING ALL ORDERS. Bot paused until manual restart.")
            print(f"  {'#'*55}")
            notifier.send(fmt_dd_halt(dd, trailing_dd_halt))
            sim.cancel_all()

        elif _alert and not dd_alerted[0]:
            dd_alerted[0] = True
            dd = account_state.trailing_dd_used
            print(f"\n  [DD WARNING] Trailing DD: ${dd:,.0f} / ${trailing_dd_halt:,.0f}")
            notifier.send(fmt_dd_warning(dd, trailing_dd_alert))

    sim.register_fill_callback(_on_fill_trailing_dd)

    # Position sync callback — update dashboard open positions on every fill
    def _on_fill_update_positions(_fill: dict) -> None:
        try:
            update_positions(sim.get_open_positions())
        except Exception:
            pass
    sim.register_fill_callback(_on_fill_update_positions)

    print(f"\n[1/2] PaperSimulator ready")
    print(f"  Account balance   : ${acct_bal:,.0f}")
    print(f"  Daily alert       : -${alert_usd:,.0f}")
    print(f"  Daily hard stop   : -${hard_stop_usd:,.0f}")
    print(f"  Trailing DD alert : -${trailing_dd_alert:,.0f}")
    print(f"  Trailing DD halt  : -${trailing_dd_halt:,.0f}")
    print(f"  DB path           : {db_path}")

    # Start background position monitor (polls yfinance every 60s, EOD settle at 16:05)
    monitor_task = asyncio.create_task(sim.start_monitor())

    async def _watchdog() -> None:
        """Restart the position monitor if it crashes (network errors, etc.)."""
        nonlocal monitor_task
        while True:
            await asyncio.sleep(30)
            if monitor_task.done() and not monitor_task.cancelled():
                exc = monitor_task.exception()
                if exc is not None:
                    log.error("Watchdog: monitor task died ({e}) — restarting", e=exc)
                    print(f"\n  [WATCHDOG] Monitor task crashed — restarting...")
                    monitor_task = asyncio.create_task(sim.start_monitor())
                else:
                    # Task finished normally (EOD break) — watchdog can exit too
                    break

    watchdog_task = asyncio.create_task(_watchdog())

    try:
        set_bot_running(True)
    except Exception:
        pass

    # Send startup notification
    enabled_strats = [
        k for k, v in config.get("tv_paper", {}).get("enabled_signals", {}).items()
        if v
    ]
    notifier.send(fmt_startup(
        mode       = "Paper",
        strategies = enabled_strats,
        time_str   = now_et().strftime("%H:%M ET — %a %d %b %Y"),
    ))

    print(f"\n[2/2] Waiting for TradingView webhook signals...")
    print(f"  Polling queue every {LOOP_SLEEP_S}s")
    print(f"  EOD settle at {EOD_H:02d}:{EOD_M:02d} ET")
    print(f"  Webhook endpoint: POST /api/webhook/signal\n")

    state = DailyState(tracker)

    try:
        while True:
            state.check_rollover()
            now = now_et()

            # EOD: exit loop (monitor task handles the actual settle)
            if now.hour > EOD_H or (now.hour == EOD_H and now.minute >= EOD_M):
                print(f"\n  [{now.strftime('%H:%M')} ET] EOD reached — wrapping up...")
                break

            # Drain and process any queued signals
            signals = _drain_signal_queue()
            for sig in signals:
                _handle_signal(sig, sim, state, tracker, acct_bal,
                               cfg=config, account_state=account_state)

            # Sync open positions to dashboard every loop
            try:
                update_positions(sim.get_open_positions())
            except Exception:
                pass

            await asyncio.sleep(LOOP_SLEEP_S)

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n  Ctrl+C received — shutting down...")

    finally:
        # Cancel all open positions at last known price
        open_pos = sim.get_open_positions()
        if open_pos:
            print(f"\n  Settling {len(open_pos)} open position(s) at EOD...")
            sim.cancel_all()
            await asyncio.sleep(2)   # allow settle callbacks to fire

        # Print daily summary
        wins   = sum(1 for t in tracker.trades_today if float(t.get("pnl_net", 0)) > 0)
        losses = sum(1 for t in tracker.trades_today if float(t.get("pnl_net", 0)) < 0)
        print("\n" + "=" * 65)
        print("  EOD SUMMARY")
        print(f"  Signals sent   : {len(state.signal_ids)}")
        print(f"  Trades today   : {len(tracker.trades_today)}")
        print(f"  P&L today      : ${tracker.pnl_today:+,.2f}")
        if tracker.trades_today:
            print(f"  Win / Loss     : {wins}W / {losses}L")
        print(f"  Trailing DD    : ${account_state.trailing_dd_used:,.0f} used "
              f"/ ${trailing_dd_halt:,.0f} limit")
        print(f"  Consec. losses : {account_state.consecutive_losses} "
              f"(next size mult: {account_state.get_size_mult():.2f}x)")
        print("=" * 65)

        # Send EOD Telegram summary
        best  = max(tracker.trades_today, key=lambda t: float(t.get("pnl_net", 0)), default=None)
        worst = min(tracker.trades_today, key=lambda t: float(t.get("pnl_net", 0)), default=None)
        notifier.send(fmt_eod(
            date_str  = now_et().strftime("%a %d %b %Y"),
            trades    = len(tracker.trades_today),
            wins      = wins,
            pnl_today = tracker.pnl_today,
            best      = best,
            worst     = worst,
        ))

        watchdog_task.cancel()
        monitor_task.cancel()
        for t in (watchdog_task, monitor_task):
            try:
                await t
            except asyncio.CancelledError:
                pass

        try:
            set_bot_running(False)
            update_positions([])
        except Exception:
            pass

        print("\n  Bot stopped.")


def main():
    asyncio.run(_main_async())


if __name__ == "__main__":
    main()
