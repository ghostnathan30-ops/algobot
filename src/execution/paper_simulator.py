"""
AlgoBot — Paper Trade Simulator
=================================
Module:  src/execution/paper_simulator.py
Purpose: Drop-in replacement for IBKRBridge when running in tv_paper mode.
         Signals arrive via TradingView webhooks (not IBKR). Fills are
         simulated immediately at the signal's entry price. Open positions
         are monitored every 60 seconds via yfinance and settled when
         stop or target is hit.

Public interface is identical to IBKRBridge so run_tv_paper_trading.py
can call bridge.submit_signal() / bridge.cancel_all() without changes.

P&L calculation matches ibkr_bridge._on_exit_fill() exactly:
    pnl_gross = pnl_pts × contracts × POINT_VALUE[market]
    commission = contracts × 2.05   (round-trip, same as ibkr_bridge.py)
    pnl_net    = pnl_gross - commission
    pnl_r      = pnl_pts / abs(entry_price - stop_price)

Partial exit:
    ORB / FHB → exit 50% at 0.7R, trail stop to breakeven
    GC  / CL  → exit 50% at 0.5R, trail stop to breakeven
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, List, Optional

import pytz

from src.execution.tv_data_feed import TVDataFeed
from src.utils.logger import get_logger
from src.utils.trade_db import TradeDB
from src.utils.telegram_notifier import (
    TelegramNotifier,
    fmt_limit_queued, fmt_limit_expired,
    fmt_entry, fmt_exit,
)

log = get_logger(__name__)
ET  = pytz.timezone("America/New_York")

# ── Constants (mirror ibkr_bridge.py) ─────────────────────────────────────────

POINT_VALUE: dict[str, float] = {
    "ES":  50.0,
    "NQ":  20.0,
    "GC":  100.0,
    "CL":  1_000.0,
    "ZB":  1_000.0,
    "6E":  125_000.0,
    "RTY": 50.0,
    "YM":  5.0,
    # Micros
    "MES": 5.0,
    "MNQ": 2.0,
    "MGC": 10.0,
    "MCL": 100.0,
}

COMMISSION_PER_RT = 2.05   # $ per round-trip contract (matches ibkr_bridge.py)

# Partial exit R-threshold by strategy
_PARTIAL_R: dict[str, float] = {
    "ORB":    0.7,
    "FHB":    0.7,
    "GC_REV": 0.5,
    "CL_FHB": 0.5,
}


# ── Position record ────────────────────────────────────────────────────────────

@dataclass
class PositionRecord:
    signal_id:         str
    market:            str
    strategy:          str
    direction:         str          # "LONG" | "SHORT"
    entry_price:       float
    stop_price:        float
    target_price:      float
    contracts:         int
    size_mult:         float
    gls_score:         int
    htf_bias:          str
    entry_time:        datetime
    partial_threshold_r: float = 0.7
    partial_done:      bool = False
    partial_contracts: int  = 0     # contracts already exited via partial
    last_price:        float = 0.0  # last known price (updated by monitor)


# ── Pending limit order record ────────────────────────────────────────────────

@dataclass
class PendingLimit:
    signal_id:   str
    signal_dict: dict          # original signal payload (entry field = limit price)
    market:      str
    direction:   str           # "LONG" | "SHORT"
    limit_price: float
    expiry_time: datetime      # cancel if not filled by this time (ET)


# ── Simulator ─────────────────────────────────────────────────────────────────

class PaperSimulator:
    """
    Virtual broker for tv_paper mode.

    Usage:
        sim = PaperSimulator(db_path="data/trades.db", account_balance=50_000)
        sim.register_fill_callback(tracker.on_fill)
        loop = asyncio.get_event_loop()
        loop.create_task(sim.start_monitor())

        signal_id = sim.submit_signal(signal_dict)
        positions = sim.get_open_positions()
        sim.cancel_all()
    """

    def __init__(
        self,
        db_path: str = "data/trades.db",
        account_balance: float = 50_000.0,
        monitor_interval_s: int = 60,
    ) -> None:
        self._db               = TradeDB(db_path)
        self._account          = account_balance
        self._monitor_interval = monitor_interval_s
        self._feed             = TVDataFeed()
        self._positions: dict[str, PositionRecord] = {}
        self._pending_limits: dict[str, PendingLimit] = {}
        self._fill_callbacks: List[Callable] = []
        self._lock             = threading.Lock()
        self._halted           = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._notifier: Optional[TelegramNotifier] = None
        # Recover any positions that were open when the process last died
        self._restore_open_positions()

    # ── Crash-recovery helpers ────────────────────────────────────────────────

    def _restore_open_positions(self) -> None:
        """
        Re-hydrate in-memory positions from trades with no exit_time in the DB.
        Called once from __init__() so a crashed/restarted bot recovers mid-day positions.
        """
        try:
            cur = self._db._conn.execute(
                """SELECT t.signal_id, t.entry_time, t.entry_price, t.stop_price,
                          t.target_price, t.contracts, t.size_mult,
                          s.market, s.strategy, s.direction,
                          s.gls_score, s.htf_bias
                   FROM trades t
                   JOIN signals s ON s.signal_id = t.signal_id
                   WHERE t.exit_time IS NULL
                     AND s.gls_action != 'PARTIAL'"""
            )
            rows = cur.fetchall()
        except Exception as e:
            log.warning("PaperSimulator: restore query failed: {e}", e=e)
            return

        if not rows:
            return

        restored = 0
        for row in rows:
            try:
                entry_time_raw = row["entry_time"]
                if entry_time_raw:
                    dt = datetime.fromisoformat(entry_time_raw)
                    entry_time = dt if dt.tzinfo else ET.localize(dt)
                else:
                    entry_time = datetime.now(ET)

                partial_r = _PARTIAL_R.get(row["strategy"], 0.7)
                pos = PositionRecord(
                    signal_id           = row["signal_id"],
                    market              = row["market"],
                    strategy            = row["strategy"],
                    direction           = row["direction"],
                    entry_price         = row["entry_price"],
                    stop_price          = row["stop_price"],
                    target_price        = row["target_price"],
                    contracts           = row["contracts"],
                    size_mult           = row["size_mult"] or 1.0,
                    gls_score           = row["gls_score"] or 0,
                    htf_bias            = row["htf_bias"] or "NEUTRAL",
                    entry_time          = entry_time,
                    partial_threshold_r = partial_r,
                    last_price          = row["entry_price"],
                )
                self._positions[row["signal_id"]] = pos
                restored += 1
            except Exception as e:
                log.warning(
                    "PaperSimulator: could not restore position {sid}: {e}",
                    sid=row["signal_id"] if "signal_id" in row.keys() else "?",
                    e=e,
                )

        if restored:
            log.info("PaperSimulator: restored {n} open position(s) from DB", n=restored)
            print(f"\n  [RESTORE] Recovered {restored} open position(s) from previous session")

    # ── IBKRBridge-compatible interface ───────────────────────────────────────

    def connect(self, timeout: int = 10) -> bool:
        """No-op — simulator is always available."""
        return True

    def disconnect(self) -> None:
        """No-op."""
        pass

    def is_connected(self) -> bool:
        """Always True — no network connection required."""
        return True

    def register_fill_callback(self, callback: Callable) -> None:
        """Register a callback invoked on every exit fill (same signature as IBKRBridge)."""
        self._fill_callbacks.append(callback)

    def set_notifier(self, notifier: TelegramNotifier) -> None:
        """Attach a TelegramNotifier to receive trade alerts."""
        self._notifier = notifier

    def get_last_price(self, market: str) -> Optional[float]:
        """Return latest price for market via yfinance (TTL-cached)."""
        return self._feed.get_last_price(market)

    def get_open_positions(self) -> list[dict]:
        """
        Return open positions in the same dict format as IBKRBridge.get_open_positions().
        Used by the dashboard's /api/live/positions endpoint.
        """
        with self._lock:
            snapshot = dict(self._positions)
        result = []
        for sid, pos in snapshot.items():
            price = self._feed.get_last_price(pos.market) or pos.entry_price
            pv    = POINT_VALUE.get(pos.market, 50.0)
            if pos.direction == "LONG":
                unrealized = (price - pos.entry_price) * pos.contracts * pv
            else:
                unrealized = (pos.entry_price - price) * pos.contracts * pv
            result.append({
                "symbol":        pos.market,
                "market":        pos.market,
                "strategy":      pos.strategy,
                "direction":     pos.direction,
                "position":      pos.contracts if pos.direction == "LONG" else -pos.contracts,
                "avg_cost":      pos.entry_price,
                "entry_price":   pos.entry_price,
                "stop_price":    pos.stop_price,
                "target_price":  pos.target_price,
                "unrealized_pnl": round(unrealized, 2),
                "signal_id":     sid,
                "source":        "tv_paper",
            })
        return result

    def cancel_all(self) -> None:
        """
        Settle all open positions at the current market price.
        Called by DailyPnLTracker when the daily hard-stop fires.
        Sets the halted flag so no new signals are accepted.
        """
        self._halted = True
        log.warning("PaperSimulator.cancel_all: halting and settling all positions")
        with self._lock:
            signal_ids = list(self._positions.keys())
        for sid in signal_ids:
            pos = self._positions.get(sid)
            if pos is None:
                continue
            price = self._feed.get_last_price(pos.market) or pos.entry_price
            self._settle_position(sid, price, "cancelled")
        log.info("PaperSimulator: all positions settled, bot halted for today")

    def submit_signal(self, signal: dict) -> Optional[str]:
        """
        Accept a signal dict (same format as ibkr_bridge.submit_signal).
        Immediately simulates an entry fill at signal["entry"].
        Returns signal_id on success, None on rejection.
        """
        if self._halted:
            log.warning("PaperSimulator: halted — signal rejected")
            return None

        # ── Field extraction ──────────────────────────────────────────────────
        market    = str(signal.get("market", "")).upper()
        strategy  = str(signal.get("strategy", "ORB")).upper()
        direction = str(signal.get("direction", "")).upper()
        entry     = float(signal.get("entry", 0.0))
        stop      = float(signal.get("stop",  0.0))
        target    = float(signal.get("target", 0.0))
        size_mult = float(signal.get("size_mult", 1.0))
        gls_score = int(signal.get("gls_score", 0))
        htf_bias  = str(signal.get("htf_bias", "NEUTRAL"))
        risk_mode = str(signal.get("risk_mode", "safe"))
        max_cts   = int(signal.get("max_contracts", 1))

        # ── Validation ────────────────────────────────────────────────────────
        if market not in POINT_VALUE:
            log.warning("PaperSimulator: unknown market {m}", m=market)
            return None
        if direction not in ("LONG", "SHORT"):
            log.warning("PaperSimulator: bad direction {d}", d=direction)
            return None
        if entry <= 0 or stop <= 0 or target <= 0:
            log.warning("PaperSimulator: invalid prices e={e} s={s} t={t}", e=entry, s=stop, t=target)
            return None

        # ── Limit order: queue and wait for fill ──────────────────────────────
        order_type = str(signal.get("order_type", "market")).lower()
        if order_type == "limit":
            now_et    = datetime.now(ET)
            signal_id = f"{strategy}_{market}_{now_et.strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}"
            # Expire at 13:30 ET same day (no new FHB entries after 1 PM);
            # fall back to 3 h from now if already past that time.
            expiry = now_et.replace(hour=13, minute=30, second=0, microsecond=0)
            if now_et >= expiry:
                expiry = now_et + timedelta(hours=3)
            plim = PendingLimit(
                signal_id   = signal_id,
                signal_dict = dict(signal),
                market      = market,
                direction   = direction,
                limit_price = entry,   # Pine sends fhb_high/fhb_low as "entry"
                expiry_time = expiry,
            )
            with self._lock:
                self._pending_limits[signal_id] = plim
            log.info(
                "PaperSimulator: LIMIT QUEUED  {s} {m} {d}  limit={lp}  expires={exp}",
                s=strategy, m=market, d=direction,
                lp=entry, exp=expiry.strftime("%H:%M ET"),
            )
            if self._notifier:
                self._notifier.send(fmt_limit_queued(
                    strategy  = strategy,
                    market    = market,
                    direction = direction,
                    limit     = entry,
                    stop      = stop,
                    target    = target,
                    expires   = expiry.strftime("%H:%M ET"),
                ))
            return signal_id

        # ── Position sizing ───────────────────────────────────────────────────
        contracts = max(1, min(round(1 * size_mult), max_cts))

        # Per-trade loss cap (safe=400, medium=2000, hardcore=3000)
        _LOSS_CAPS = {"safe": 400.0, "medium": 2_000.0, "hardcore": 3_000.0}
        loss_cap  = _LOSS_CAPS.get(risk_mode, 400.0)
        pv        = POINT_VALUE[market]
        risk_pts  = abs(entry - stop)
        risk_usd  = risk_pts * contracts * pv

        if risk_usd > loss_cap:
            # Tighten stop to fit within cap (match ibkr_bridge logic)
            allowed_pts = loss_cap / (contracts * pv)
            if direction == "LONG":
                stop   = round(entry - allowed_pts, 2)
                target = round(entry + allowed_pts * 2.0, 2)
            else:
                stop   = round(entry + allowed_pts, 2)
                target = round(entry - allowed_pts * 2.0, 2)
            risk_pts = abs(entry - stop)
            risk_usd = risk_pts * contracts * pv
            log.info("PaperSimulator: stop tightened to fit ${cap:.0f} cap", cap=loss_cap)

        # ── Signal ID ─────────────────────────────────────────────────────────
        now_et     = datetime.now(ET)
        trade_date = now_et.strftime("%Y-%m-%d")
        signal_id  = f"{strategy}_{market}_{now_et.strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}"

        # ── Log to TradeDB ────────────────────────────────────────────────────
        try:
            self._db.log_signal(
                signal_id    = signal_id,
                trade_date   = trade_date,
                market       = market,
                strategy     = strategy,
                direction    = direction,
                gls_score    = gls_score,
                gls_action   = "FULL_SIZE",
                filtered     = False,
                filter_reason= "",
                htf_bias     = htf_bias,
                extra        = {"source": "tv_paper", "size_mult": size_mult, "risk_mode": risk_mode},
            )
            self._db.log_trade_entry(
                signal_id    = signal_id,
                entry_time   = now_et.isoformat(),
                entry_price  = entry,
                stop_price   = stop,
                target_price = target,
                contracts    = contracts,
                risk_usd     = round(risk_usd, 2),
                size_mult    = size_mult,
            )
        except Exception as e:
            log.warning("PaperSimulator: TradeDB log error: {e}", e=e)

        # ── Create position record ────────────────────────────────────────────
        partial_r = _PARTIAL_R.get(strategy, 0.7)
        pos = PositionRecord(
            signal_id          = signal_id,
            market             = market,
            strategy           = strategy,
            direction          = direction,
            entry_price        = entry,
            stop_price         = stop,
            target_price       = target,
            contracts          = contracts,
            size_mult          = size_mult,
            gls_score          = gls_score,
            htf_bias           = htf_bias,
            entry_time         = now_et,
            partial_threshold_r= partial_r,
            last_price         = entry,
        )

        with self._lock:
            self._positions[signal_id] = pos

        # Sync to dashboard
        try:
            from dashboard.bot_state import update_positions
            update_positions(self.get_open_positions())
        except Exception:
            pass

        log.info(
            "PaperSimulator: ENTRY FILL  {s} {m} {d} @ {e}  stop={st}  target={t}  qty={q}",
            s=strategy, m=market, d=direction, e=entry, st=stop, t=target, q=contracts,
        )
        if self._notifier:
            self._notifier.send(fmt_entry(
                strategy  = strategy,
                market    = market,
                direction = direction,
                entry     = entry,
                stop      = stop,
                target    = target,
                contracts = contracts,
                risk_usd  = round(risk_usd, 0),
            ))
        return signal_id

    # ── Position monitor ──────────────────────────────────────────────────────

    async def start_monitor(self) -> None:
        """
        Async background task that polls prices every monitor_interval_s seconds.
        Settles positions when stop or target is hit.
        Triggers EOD settlement at 16:05 ET.

        Schedule with: asyncio.create_task(sim.start_monitor())
        """
        log.info("PaperSimulator: position monitor started (interval={i}s)", i=self._monitor_interval)
        while True:
            try:
                await asyncio.sleep(self._monitor_interval)
                now_et = datetime.now(ET)

                # EOD: 16:05 ET — settle all remaining positions, cancel pending limits
                if now_et.hour > 16 or (now_et.hour == 16 and now_et.minute >= 5):
                    log.info("PaperSimulator: EOD ({t}) — settling all open positions", t=now_et.strftime("%H:%M"))
                    with self._lock:
                        sids       = list(self._positions.keys())
                        n_pending  = len(self._pending_limits)
                        self._pending_limits.clear()
                    if n_pending:
                        log.info("PaperSimulator: {n} pending limit orders cancelled (EOD)", n=n_pending)
                    for sid in sids:
                        if sid not in self._positions:
                            continue
                        pos   = self._positions[sid]
                        price = self._feed.get_last_price(pos.market) or pos.entry_price
                        self._settle_position(sid, price, "eod")
                    break   # stop the monitor after EOD

                # Check each open position
                with self._lock:
                    snapshot = dict(self._positions)

                for sid, pos in snapshot.items():
                    if sid not in self._positions:
                        continue
                    price = self._feed.get_last_price(pos.market)
                    if price is None:
                        continue
                    pos.last_price = price

                    reason = self._check_stop_target(pos, price)
                    if reason:
                        self._settle_position(sid, price, reason)

                # ── Check pending limit orders ─────────────────────────────
                with self._lock:
                    plim_snapshot = dict(self._pending_limits)

                for sid, plim in plim_snapshot.items():
                    if sid not in self._pending_limits:
                        continue
                    # Expire check
                    if now_et >= plim.expiry_time:
                        with self._lock:
                            self._pending_limits.pop(sid, None)
                        log.info(
                            "PaperSimulator: limit expired  {m} {d} @ {lp}",
                            m=plim.market, d=plim.direction, lp=plim.limit_price,
                        )
                        if self._notifier:
                            self._notifier.send(fmt_limit_expired(
                                market    = plim.market,
                                direction = plim.direction,
                                limit     = plim.limit_price,
                            ))
                        continue
                    # Price fill check
                    price = self._feed.get_last_price(plim.market)
                    if price is None:
                        continue
                    touched = (
                        (plim.direction == "LONG"  and price <= plim.limit_price)
                        or (plim.direction == "SHORT" and price >= plim.limit_price)
                    )
                    if touched:
                        with self._lock:
                            self._pending_limits.pop(sid, None)
                        # Fill: submit as market order at limit price
                        fill_signal = {**plim.signal_dict, "order_type": "market"}
                        self.submit_signal(fill_signal)
                        log.info(
                            "PaperSimulator: LIMIT FILLED   {m} {d} @ {lp}",
                            m=plim.market, d=plim.direction, lp=plim.limit_price,
                        )

            except asyncio.CancelledError:
                raise   # propagate — allow clean task cancellation
            except Exception as exc:
                log.error(
                    "PaperSimulator: monitor error (retrying in 5 s): {e}", e=exc
                )
                await asyncio.sleep(5)   # brief back-off before next iteration

        log.info("PaperSimulator: position monitor stopped")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _check_stop_target(self, pos: PositionRecord, price: float) -> Optional[str]:
        """
        Check whether price has hit stop or target.
        Also handles partial exit at the configured R threshold.
        Returns exit reason string or None.
        """
        risk_pts  = abs(pos.entry_price - pos.stop_price)
        if risk_pts <= 0:
            return None

        if pos.direction == "LONG":
            pnl_pts = price - pos.entry_price
        else:
            pnl_pts = pos.entry_price - price
        current_r = pnl_pts / risk_pts

        # ── Partial exit ──────────────────────────────────────────────────────
        if not pos.partial_done and current_r >= pos.partial_threshold_r and pos.contracts > 1:
            partial_cts = max(1, pos.contracts // 2)
            self._fire_partial_exit(pos, price, partial_cts)
            # Trail stop to breakeven
            pos.stop_price   = pos.entry_price
            pos.partial_done = True
            pos.partial_contracts = partial_cts
            pos.contracts   -= partial_cts
            log.info(
                "PaperSimulator: PARTIAL EXIT {sid} {r:.1f}R @ {p}  stop → BE",
                sid=pos.signal_id, r=current_r, p=price,
            )

        # ── Full stop ─────────────────────────────────────────────────────────
        if pos.direction == "LONG"  and price <= pos.stop_price:
            return "stop"
        if pos.direction == "SHORT" and price >= pos.stop_price:
            return "stop"

        # ── Full target ───────────────────────────────────────────────────────
        if pos.direction == "LONG"  and price >= pos.target_price:
            return "target"
        if pos.direction == "SHORT" and price <= pos.target_price:
            return "target"

        return None

    def _fire_partial_exit(
        self,
        pos: PositionRecord,
        exit_price: float,
        partial_cts: int,
    ) -> None:
        """Record a partial fill in the DB and fire callbacks (informational)."""
        now_et    = datetime.now(ET)
        pv        = POINT_VALUE.get(pos.market, 50.0)
        if pos.direction == "LONG":
            pnl_pts = exit_price - pos.entry_price
        else:
            pnl_pts = pos.entry_price - exit_price
        pnl_gross  = pnl_pts * partial_cts * pv
        commission = partial_cts * COMMISSION_PER_RT
        pnl_net    = pnl_gross - commission
        risk_pts   = abs(pos.entry_price - pos.stop_price)
        pnl_r      = pnl_pts / risk_pts if risk_pts > 0 else 0.0

        partial_id = f"{pos.signal_id}_P"
        try:
            self._db.log_signal(
                signal_id    = partial_id,
                trade_date   = now_et.strftime("%Y-%m-%d"),
                market       = pos.market,
                strategy     = pos.strategy,
                direction    = pos.direction,
                gls_score    = pos.gls_score,
                gls_action   = "PARTIAL",
                filtered     = False,
                filter_reason= "",
                htf_bias     = pos.htf_bias,
                extra        = {"partial": True, "parent_signal_id": pos.signal_id},
            )
            self._db.log_trade_entry(
                signal_id    = partial_id,
                entry_time   = pos.entry_time.isoformat(),
                entry_price  = pos.entry_price,
                stop_price   = pos.stop_price,
                target_price = pos.target_price,
                contracts    = partial_cts,
                risk_usd     = abs(pos.entry_price - pos.stop_price) * partial_cts * pv,
                size_mult    = pos.size_mult,
            )
            self._db.log_trade_exit(
                signal_id    = partial_id,
                exit_time    = now_et.isoformat(),
                exit_price   = exit_price,
                exit_reason  = "partial",
                pnl_gross    = pnl_gross,
                pnl_net      = pnl_net,
                pnl_r        = pnl_r,
                commission   = commission,
            )
        except Exception as e:
            log.warning("PaperSimulator: partial DB log error: {e}", e=e)

        fill_info = {
            "signal_id":   partial_id,
            "market":      pos.market,
            "strategy":    pos.strategy,
            "direction":   pos.direction,
            "exit_price":  exit_price,
            "exit_reason": "partial",
            "pnl_net":     pnl_net,
            "pnl_r":       pnl_r,
            "contracts":   partial_cts,
        }
        for cb in self._fill_callbacks:
            try:
                cb(fill_info)
            except Exception as e:
                log.warning("fill_callback error: {e}", e=e)

    def _settle_position(
        self,
        signal_id: str,
        exit_price: float,
        reason: str,
    ) -> None:
        """
        Calculate P&L, log to TradeDB, fire fill callbacks, and remove position.
        Mirrors ibkr_bridge._on_exit_fill() exactly.
        """
        with self._lock:
            pos = self._positions.pop(signal_id, None)
        if pos is None:
            return

        now_et     = datetime.now(ET)
        pv         = POINT_VALUE.get(pos.market, 50.0)

        if pos.direction == "LONG":
            pnl_pts = exit_price - pos.entry_price
        else:
            pnl_pts = pos.entry_price - exit_price

        pnl_gross  = pnl_pts * pos.contracts * pv
        commission = pos.contracts * COMMISSION_PER_RT
        pnl_net    = pnl_gross - commission
        risk_pts   = abs(pos.entry_price - pos.stop_price)
        pnl_r      = pnl_pts / risk_pts if risk_pts > 0 else 0.0
        bars_held  = max(0, int((now_et - pos.entry_time).total_seconds() / 300))  # approx 5-min bars

        # ── Log to TradeDB ────────────────────────────────────────────────────
        try:
            self._db.log_trade_exit(
                signal_id    = signal_id,
                exit_time    = now_et.isoformat(),
                exit_price   = exit_price,
                exit_reason  = reason,
                pnl_gross    = round(pnl_gross, 2),
                pnl_net      = round(pnl_net, 2),
                pnl_r        = round(pnl_r, 3),
                commission   = round(commission, 2),
                bars_held    = bars_held,
            )
        except Exception as e:
            log.warning("PaperSimulator: TradeDB exit log error: {e}", e=e)

        # ── Console banner (mirrors ibkr_bridge output) ───────────────────────
        result_icon = "✓" if pnl_net >= 0 else "✗"
        log.info(
            "PaperSimulator: EXIT FILL  {r}  {s} {m} {d}  {reason} @ {p}"
            "  P&L ${pnl:+,.2f}  ({pr:.2f}R)",
            r=result_icon, s=pos.strategy, m=pos.market, d=pos.direction,
            reason=reason, p=exit_price, pnl=pnl_net, pr=pnl_r,
        )
        print(
            f"\n  [PAPER EXIT] {pos.strategy} {pos.market} {pos.direction}  "
            f"{reason.upper()} @ {exit_price}  |  "
            f"P&L ${pnl_net:+,.2f}  ({pnl_r:.2f}R)  "
            f"[{pos.contracts} ct × ${pv:.0f}/pt]"
        )
        if self._notifier:
            self._notifier.send(fmt_exit(
                strategy   = pos.strategy,
                market     = pos.market,
                direction  = pos.direction,
                exit_price = exit_price,
                pnl_net    = pnl_net,
                pnl_r      = pnl_r,
                reason     = reason,
            ))

        # ── Fire fill callbacks (DailyPnLTracker.on_fill, etc.) ───────────────
        fill_info = {
            "signal_id":   signal_id,
            "market":      pos.market,
            "strategy":    pos.strategy,
            "direction":   pos.direction,
            "entry_price": pos.entry_price,
            "exit_price":  exit_price,
            "exit_reason": reason,
            "pnl_gross":   round(pnl_gross, 2),
            "pnl_net":     round(pnl_net, 2),
            "pnl_r":       round(pnl_r, 3),
            "commission":  round(commission, 2),
            "contracts":   pos.contracts,
        }
        for cb in self._fill_callbacks:
            try:
                cb(fill_info)
            except Exception as e:
                log.warning("fill_callback error: {e}", e=e)

        # Sync positions to dashboard
        try:
            from dashboard.bot_state import update_positions
            update_positions(self.get_open_positions())
        except Exception:
            pass
