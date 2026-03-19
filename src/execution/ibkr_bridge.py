"""
AlgoBot -- Interactive Brokers Paper Trading Bridge
=====================================================
Module:  src/execution/ibkr_bridge.py
Phase:   6 -- Paper Trading
Purpose: Connect AlgoBot signal engine to Interactive Brokers TWS (paper account)
         via the ib_insync library. Translates FHB/ORB signal dicts into real
         bracket orders (entry + stop + target) and logs everything to TradeDB.

Architecture:
    AlgoBot signals -> IBKRBridge.submit_signal() -> TWS (paper) -> filled/stopped
                                                  -> TradeDB (SQLite log)

Connection setup (run ONCE before starting the bot):
    1. Download TWS from interactivebrokers.com
    2. Log into Paper Trading account (separate credentials from live)
    3. TWS -> Edit -> Global Configuration -> API -> Settings
       - Check:   "Enable ActiveX and Socket Clients"
       - Uncheck: "Read-Only API"
       - Socket port: 7497 (paper TWS) or 4002 (paper IB Gateway)
       - Trusted IP: 127.0.0.1
    4. Click OK, restart TWS if prompted
    5. Run: conda run -n algobot_env python -m src.execution.ibkr_bridge

Contract expiry codes (update quarterly):
    ES / NQ: Mar(H), Jun(M), Sep(U), Dec(Z)  -> e.g. "202506" for June 2025
    GC:      nearest active month             -> e.g. "202506"
    Update CONTRACT_EXPIRY below each quarter.

Usage:
    from src.execution.ibkr_bridge import IBKRBridge

    bridge = IBKRBridge(paper=True)
    bridge.connect()

    # Submit an FHB or ORB signal dict
    order_id = bridge.submit_signal({
        "market":     "ES",
        "direction":  "LONG",
        "entry":      5800.0,
        "stop":       5781.0,    # stop price
        "target":     5838.0,    # 2R target
        "size_mult":  1.0,       # 1.0 = full size, 0.5 = half
        "strategy":   "FHB",
        "gls_score":  85,
    })

    bridge.disconnect()
"""

from __future__ import annotations

import functools
import time
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

# ib_insync is installed: pip install ib_insync
try:
    from ib_insync import IB, Future, Order, LimitOrder, StopOrder, MarketOrder
    from ib_insync import util as ib_util
    IB_AVAILABLE = True
except ImportError:
    IB_AVAILABLE = False

from src.utils.logger import get_logger
from src.utils.trade_db import TradeDB

log = get_logger(__name__)


def _load_risk_mode_config() -> dict:
    """
    Read the active risk mode from dashboard/cache/bot_state.json and return
    its parameter dict.  Falls back to 'medium' defaults if file is missing.
    """
    try:
        state_file = Path(__file__).parent.parent.parent / "dashboard" / "cache" / "bot_state.json"
        if state_file.exists():
            import json as _json
            state = _json.loads(state_file.read_text(encoding="utf-8"))
            mode  = state.get("risk_mode", "medium")
            # Inline fallback table (mirrors bot_state.RISK_MODES)
            modes = {
                "safe":     {"max_contracts": 1, "daily_loss_cap_usd": 1500.0, "max_loss_per_trade_usd": 1000.0},
                "medium":   {"max_contracts": 3, "daily_loss_cap_usd": 2500.0, "max_loss_per_trade_usd": 2000.0},
                "hardcore": {"max_contracts": 5, "daily_loss_cap_usd": 3800.0, "max_loss_per_trade_usd": 3000.0},
            }
            cfg = modes.get(mode, modes["medium"])
            cfg["risk_mode"] = mode
            return cfg
    except Exception:
        pass
    return {"risk_mode": "medium", "max_contracts": 3, "daily_loss_cap_usd": 2500.0, "max_loss_per_trade_usd": 2000.0}

# ============================================================
# CONTRACT CONFIGURATION
# Update CONTRACT_EXPIRY each quarter (Mar/Jun/Sep/Dec for equity index,
# or nearest active month for Gold/Oil).
# ============================================================

CONTRACT_EXPIRY: dict[str, str] = {
    "ES":  "202606",   # E-mini S&P 500   -- Jun 2026 (rolled from Mar on ~Mar 13 2026)
    "NQ":  "202606",   # E-mini Nasdaq-100 -- Jun 2026
    "GC":  "202606",   # Gold futures      -- Jun 2026 (Apr contract expired; next active is Jun)
    "RTY": "202606",   # E-mini Russell 2000 -- Jun 2026
    "YM":  "202606",   # E-mini Dow Jones  -- Jun 2026
    "CL":  "202605",   # Crude Oil         -- May 2026 (monthly roll, ~Apr 20)
    "ZB":  "202606",   # 30-yr Bond        -- Jun 2026 (quarterly)
    "6E":  "202606",   # Euro FX           -- Jun 2026
}

CONTRACT_EXCHANGE: dict[str, str] = {
    "ES":  "CME",
    "NQ":  "CME",
    "GC":  "COMEX",
    "RTY": "CME",
    "YM":  "CBOT",
    "CL":  "NYMEX",
    "ZB":  "CBOT",
    "6E":  "CME",
}

CONTRACT_SYMBOL: dict[str, str] = {
    "ES":  "ES",
    "NQ":  "NQ",
    "GC":  "GC",
    "RTY": "RTY",
    "YM":  "YM",
    "CL":  "CL",
    "ZB":  "ZB",
    "6E":  "6E",
}

# Base contract size (1 lot) -- Topstep limits apply on top of this
CONTRACT_SIZE: dict[str, int] = {
    "ES":  1,
    "NQ":  1,
    "GC":  1,
    "RTY": 1,
    "YM":  1,
    "CL":  1,
    "ZB":  1,
    "6E":  1,
}

# ── Priority 1A: Hard per-trade loss cap ──────────────────────────────────────
# If the signal's ATR-based stop would risk more than this, the stop is
# tightened at submission time.  Target: never let a single trade exceed
# the Topstep $3,000/day limit, with a $1,000 safety buffer.
MAX_LOSS_PER_TRADE_USD: float = 2_000.0

POINT_VALUE: dict[str, float] = {
    "ES":  50.0,       # $50 per full index point
    "NQ":  20.0,       # $20 per full index point
    "GC":  100.0,      # $100 per troy-oz point
    "RTY": 50.0,       # $50 per full index point
    "YM":  5.0,        # $5 per Dow point
    "CL":  1_000.0,    # $1,000 per dollar move
    "ZB":  1_000.0,    # $1,000 per full point
    "6E":  125_000.0,  # $125,000 per pip (contract face value)
}


# ============================================================
# IBKR BRIDGE
# ============================================================

class IBKRBridge:
    """
    Interactive Brokers paper trading bridge for AlgoBot.

    Args:
        host:       TWS host (default 127.0.0.1 for local)
        port:       7497 for paper TWS, 4002 for paper IB Gateway, 7496 for live TWS
        client_id:  Unique client ID (use different IDs if running multiple bots)
        paper:      Safety switch -- if True, adds paper-trading validation checks
        db_path:    Path to SQLite trade database (logs all orders)
        max_contracts: Max contracts per order (hard position limit)
    """

    def __init__(
        self,
        host:          str   = "127.0.0.1",
        port:          int   = 7497,
        client_id:     int   = 1,
        paper:         bool  = True,
        db_path:       str   = "",
        max_contracts: int   = 0,       # 0 = read from active risk mode in bot_state.json
    ):
        if not IB_AVAILABLE:
            raise ImportError(
                "ib_insync not installed. Run: "
                "conda run -n algobot_env pip install ib_insync"
            )

        # Load risk mode config from dashboard state (set via web control panel)
        self._risk_cfg = _load_risk_mode_config()

        self.host          = host
        self.port          = port
        self.client_id     = client_id
        self.paper         = paper
        # If caller passes 0 (default), use the mode's max_contracts
        self.max_contracts = max_contracts if max_contracts > 0 else self._risk_cfg["max_contracts"]
        self._ib           = IB()
        self._connected    = False
        self._open_orders: dict[str, object] = {}          # signal_id -> Trade info dict
        self._entries: dict[str, float] = {}               # signal_id -> actual entry fill price
        self._fill_callbacks: list[Callable] = []          # all callbacks fired on every exit fill

        if not db_path:
            db_path = str(Path(__file__).parent.parent.parent / "data" / "trades.db")
        self._db = TradeDB(db_path)

        log.info(
            "IBKRBridge init: host={h} port={p} client_id={c} paper={paper} "
            "risk_mode={mode} max_contracts={mc} daily_cap=${cap}",
            h=host, p=port, c=client_id, paper=paper,
            mode=self._risk_cfg["risk_mode"],
            mc=self.max_contracts,
            cap=self._risk_cfg["daily_loss_cap_usd"],
        )

    # ── Connection ─────────────────────────────────────────────────────────────

    def connect(self, timeout: int = 10) -> bool:
        """
        Connect to TWS. Returns True on success.

        TWS must be running and API access must be enabled.
        Paper trading port is 7497 (TWS) or 4002 (IB Gateway).
        """
        if self._connected:
            return True
        try:
            self._ib.connect(self.host, self.port, clientId=self.client_id,
                             timeout=timeout, readonly=False)
            self._connected = True
            acct = self._ib.managedAccounts()
            log.info("IBKRBridge connected: accounts={acct}", acct=acct)
            print(f"  IBKR connected | Account(s): {acct}")
            print(f"  Mode: {'PAPER TRADING' if self.paper else 'LIVE TRADING'}")
            return True
        except Exception as exc:
            log.error("IBKRBridge connection failed: {err}", err=exc)
            print(f"\n  ERROR: Could not connect to TWS at {self.host}:{self.port}")
            print("  Make sure TWS is running and API access is enabled.")
            print("  See module docstring for setup steps.")
            return False

    def disconnect(self) -> None:
        """Cleanly disconnect from TWS."""
        if self._connected:
            self._ib.disconnect()
            self._connected = False
            log.info("IBKRBridge disconnected")
        self._db.close()

    def is_connected(self) -> bool:
        return self._connected and self._ib.isConnected()

    # ── Account info ───────────────────────────────────────────────────────────

    def get_account_value(self) -> float:
        """Return current net liquidation value of the account."""
        self._require_connection()
        vals = self._ib.accountValues()
        for v in vals:
            if v.tag == "NetLiquidation" and v.currency == "USD":
                return float(v.value)
        return 0.0

    def get_open_positions(self) -> list[dict]:
        """Return list of open futures positions."""
        self._require_connection()
        positions = []
        for pos in self._ib.positions():
            if pos.contract.secType == "FUT":
                positions.append({
                    "symbol":    pos.contract.localSymbol,
                    "market":    pos.contract.symbol,
                    "position":  pos.position,
                    "avg_cost":  pos.avgCost,
                })
        return positions

    def get_total_position_count(self) -> int:
        """Count total open futures contracts (absolute value)."""
        return sum(abs(p["position"]) for p in self.get_open_positions())

    # ── Order submission ───────────────────────────────────────────────────────

    def submit_signal(self, signal: dict) -> Optional[str]:
        """
        Convert an AlgoBot signal dict into a TWS bracket order.

        Signal dict keys:
            market      (str)   "ES", "NQ", etc.
            direction   (str)   "LONG" or "SHORT"
            entry       (float) entry price (market order if 0.0)
            stop        (float) hard stop price
            target      (float) profit target price
            size_mult   (float) 1.0 = full, 0.5 = half
            strategy    (str)   "FHB" or "ORB"
            gls_score   (int)   0-100 quality score
            signal_id   (str)   optional unique ID (auto-generated if missing)

        Returns:
            signal_id string if order placed, None if rejected.
        """
        self._require_connection()

        market    = str(signal.get("market", "ES")).upper()
        direction = str(signal.get("direction", "LONG")).upper()
        entry     = float(signal.get("entry",    0.0))
        stop_px   = float(signal.get("stop",     0.0))
        target    = float(signal.get("target",   0.0))
        size_mult = float(signal.get("size_mult", 1.0))
        strategy  = str(signal.get("strategy",  "FHB"))
        gls_score = int(signal.get("gls_score",  0))
        signal_id = str(signal.get("signal_id",
                                    f"{strategy}_{market}_{datetime.now().strftime('%H%M%S')}"))

        # ── Validation ────────────────────────────────────────────────────────
        if market not in CONTRACT_EXPIRY:
            log.error("Unknown market {m} -- order rejected", m=market)
            return None

        if stop_px <= 0 or target <= 0:
            log.error("Invalid stop={s} or target={t} -- rejected", s=stop_px, t=target)
            return None

        qty = max(1, round(CONTRACT_SIZE.get(market, 1) * size_mult))
        qty = min(qty, self.max_contracts)

        if self.paper:
            acct_val = self.get_account_value()
            if acct_val > 0 and qty * 50 * abs(entry - stop_px) > acct_val * 0.03:
                log.warning(
                    "Risk per trade exceeds 3%% of account -- capping to 1 contract",
                )
                qty = 1

        # ── Priority 1A: Hard per-trade dollar loss cap ───────────────────────
        # Cap comes from the active risk mode (set in web control panel).
        # Safe=$1k, Medium=$2k, Hardcore=$3k — all below Topstep daily limit.
        pv           = POINT_VALUE.get(market, 50.0)
        risk_pts_raw = abs(entry - stop_px)
        mode_cap     = self._risk_cfg.get("max_loss_per_trade_usd", MAX_LOSS_PER_TRADE_USD)
        max_risk_pts = mode_cap / pv
        if risk_pts_raw > max_risk_pts:
            log.warning(
                "Hard loss cap [{mode}]: {m} risk=${r:.0f} -> capped at ${cap:.0f} "
                "({pts:.1f} pts max)",
                mode=self._risk_cfg["risk_mode"], m=market, r=risk_pts_raw * pv,
                cap=mode_cap, pts=max_risk_pts,
            )
            if direction == "LONG":
                stop_px = round(entry - max_risk_pts, 2)
                target  = round(entry + 2.0 * max_risk_pts, 2)
            else:
                stop_px = round(entry + max_risk_pts, 2)
                target  = round(entry - 2.0 * max_risk_pts, 2)
            print(
                f"  RISK CAP [{self._risk_cfg['risk_mode'].upper()}] applied: "
                f"original risk ${risk_pts_raw * pv:,.0f} "
                f"-> capped at ${mode_cap:,.0f} | stop={stop_px}, target={target}"
            )

        # ── Build contract ────────────────────────────────────────────────────
        contract = Future(
            symbol   = CONTRACT_SYMBOL.get(market, market),
            lastTradeDateOrContractMonth = CONTRACT_EXPIRY[market],
            exchange = CONTRACT_EXCHANGE.get(market, "CME"),
            currency = "USD",
        )

        # ── Qualify contract (get full IBKR contract details) ─────────────────
        try:
            self._ib.qualifyContracts(contract)
        except Exception as exc:
            log.warning("Could not qualify contract {m}: {err}", m=market, err=exc)

        # ── Build bracket order ───────────────────────────────────────────────
        action = "BUY" if direction == "LONG" else "SELL"
        close_action = "SELL" if direction == "LONG" else "BUY"

        # Parent: market entry or limit entry
        if entry > 0:
            parent_order = LimitOrder(action, qty, entry)
        else:
            parent_order = MarketOrder(action, qty)

        parent_order.orderId  = self._ib.client.getReqId()
        parent_order.transmit = False   # hold until all legs are ready

        # Stop loss (child 1)
        stop_order             = StopOrder(close_action, qty, stop_px)
        stop_order.orderId     = self._ib.client.getReqId()
        stop_order.parentId    = parent_order.orderId
        stop_order.transmit    = False

        # Profit target (child 2)
        target_order           = LimitOrder(close_action, qty, target)
        target_order.orderId   = self._ib.client.getReqId()
        target_order.parentId  = parent_order.orderId
        target_order.transmit  = True   # transmit all 3 together

        # Set OCA group so stop and target cancel each other
        oca_group = f"OCA_{signal_id}"
        stop_order.ocaGroup    = oca_group
        stop_order.ocaType     = 1   # cancel other orders with block remaining
        target_order.ocaGroup  = oca_group
        target_order.ocaType   = 1

        # ── Place orders ──────────────────────────────────────────────────────
        try:
            parent_trade = self._ib.placeOrder(contract, parent_order)
            stop_trade   = self._ib.placeOrder(contract, stop_order)
            target_trade = self._ib.placeOrder(contract, target_order)
            self._ib.sleep(0.5)   # give TWS a moment to acknowledge

            self._open_orders[signal_id] = {
                "parent":   parent_trade,
                "stop":     stop_trade,
                "target":   target_trade,
                "contract": contract,
                "signal":   signal,
            }

            log.info(
                "Order placed: {strat} {mkt} {dir} x{qty} | "
                "entry={e} stop={s} target={t} | signal_id={sid}",
                strat=strategy, mkt=market, dir=direction, qty=qty,
                e=entry, s=stop_px, t=target, sid=signal_id,
            )
            print(f"\n  ORDER PLACED: {strategy} {direction} {market} x{qty}")
            print(f"    Entry:  {entry}")
            print(f"    Stop:   {stop_px}")
            print(f"    Target: {target}")
            print(f"    GLS:    {gls_score}/100")

            # ── Log signal to database (entry logged on actual fill, not here) ──
            # This prevents dangling trade records when limit entries never trigger.
            self._db.log_signal(
                signal_id=signal_id,
                trade_date=str(date.today()),
                market=market,
                strategy=strategy,
                direction=direction,
                gls_score=gls_score,
                gls_action="FULL_SIZE" if size_mult >= 1.0 else "HALF_SIZE",
                filtered=False,
            )

            # ── Attach fill event handlers ─────────────────────────────────────
            # Builds a frozen copy of signal for use in callbacks
            _sig = dict(signal, market=market, direction=direction,
                        strategy=strategy, stop=stop_px, target=target,
                        entry=entry, size_mult=size_mult)
            parent_trade.fillEvent += functools.partial(
                self._on_entry_fill, signal_id=signal_id, signal=_sig,
            )
            stop_trade.fillEvent += functools.partial(
                self._on_exit_fill, signal_id=signal_id, signal=_sig,
                reason_hint="stop",
            )
            target_trade.fillEvent += functools.partial(
                self._on_exit_fill, signal_id=signal_id, signal=_sig,
                reason_hint="target",
            )

            return signal_id

        except Exception as exc:
            log.error("Order failed for {m}: {err}", m=market, err=exc)
            print(f"  ERROR placing order: {exc}")
            return None

    def cancel_signal(self, signal_id: str) -> bool:
        """Cancel all orders associated with a signal_id."""
        self._require_connection()
        info = self._open_orders.get(signal_id)
        if not info:
            log.warning("No open orders for signal_id={sid}", sid=signal_id)
            return False
        try:
            self._ib.cancelOrder(info["parent"].order)
            self._ib.cancelOrder(info["stop"].order)
            self._ib.cancelOrder(info["target"].order)
            del self._open_orders[signal_id]
            log.info("Cancelled orders for {sid}", sid=signal_id)
            return True
        except Exception as exc:
            log.error("Cancel failed: {err}", err=exc)
            return False

    def cancel_all(self) -> None:
        """Cancel all open orders. Call this on bot shutdown."""
        self._require_connection()
        self._ib.reqGlobalCancel()
        self._open_orders.clear()
        log.info("All orders cancelled (reqGlobalCancel)")

    # ── Market data (real-time quote) ─────────────────────────────────────────

    def get_last_price(self, market: str) -> Optional[float]:
        """Get last traded price for a market. Requires market data subscription."""
        self._require_connection()
        if market not in CONTRACT_EXPIRY:
            return None
        contract = Future(
            symbol   = CONTRACT_SYMBOL.get(market, market),
            lastTradeDateOrContractMonth = CONTRACT_EXPIRY[market],
            exchange = CONTRACT_EXCHANGE.get(market, "CME"),
            currency = "USD",
        )
        try:
            self._ib.qualifyContracts(contract)
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._ib.sleep(2)   # wait for snapshot
            price = ticker.last or ticker.close
            self._ib.cancelMktData(contract)
            return float(price) if price and price > 0 else None
        except Exception as exc:
            log.warning("get_last_price({m}) failed: {err}", m=market, err=exc)
            return None

    # ── Fill monitoring ────────────────────────────────────────────────────────

    def register_fill_callback(self, callback: Callable) -> None:
        """
        Register a function to be called whenever a trade exits (stop or target).
        Multiple callbacks can be registered; all are called in registration order.

        callback signature:
            callback(fill_info: dict) -> None

        fill_info keys:
            signal_id, market, strategy, direction,
            exit_price, exit_reason, pnl_net, pnl_r
        """
        self._fill_callbacks.append(callback)

    def _on_entry_fill(self, trade, fill, *, signal_id: str, signal: dict) -> None:
        """
        Called by ib_insync when the parent (entry) order fills.
        Logs the actual fill price to TradeDB; overwrites the placeholder
        row written at order submission time.
        """
        filled_px  = float(fill.execution.avgPrice)
        qty        = int(fill.execution.shares)
        market     = signal.get("market", "ES")
        pv         = POINT_VALUE.get(market, 50.0)
        stop_px    = float(signal.get("stop",   0.0))
        target_px  = float(signal.get("target", 0.0))

        self._entries[signal_id] = filled_px   # store for P&L calculation at exit

        self._db.log_trade_entry(
            signal_id    = signal_id,
            entry_time   = str(datetime.now()),
            entry_price  = filled_px,
            stop_price   = stop_px,
            target_price = target_px,
            risk_usd     = qty * abs(filled_px - stop_px) * pv,
            size_mult    = float(signal.get("size_mult", 1.0)),
        )
        log.info("{sid}: ENTRY filled @ {px}", sid=signal_id, px=filled_px)
        print(f"\n  [ENTRY FILL] {signal.get('strategy','')} {market} "
              f"{signal.get('direction','')} @ {filled_px}")

    def _on_exit_fill(
        self, trade, fill, *,
        signal_id:   str,
        signal:      dict,
        reason_hint: str,   # "target" or "stop"
    ) -> None:
        """
        Called by ib_insync when the stop or target order fills.
        Computes net P&L, logs the exit to TradeDB, and calls the fill callback.
        """
        filled_px  = float(fill.execution.avgPrice)
        qty        = int(fill.execution.shares)
        market     = signal.get("market", "ES")
        direction  = signal.get("direction", "LONG")
        pv         = POINT_VALUE.get(market, 50.0)

        entry_px   = self._entries.get(signal_id, float(signal.get("entry", 0.0)))
        stop_px    = float(signal.get("stop", 0.0))
        risk_pts   = abs(entry_px - stop_px)

        pnl_pts    = (filled_px - entry_px) if direction == "LONG" else (entry_px - filled_px)
        pnl_gross  = pnl_pts * qty * pv
        commission = qty * 2.05     # ~$2.05 round-trip per contract (IBKR typical)
        pnl_net    = pnl_gross - commission
        pnl_r      = pnl_pts / risk_pts if risk_pts > 0 else 0.0

        self._db.log_trade_exit(
            signal_id  = signal_id,
            exit_time  = str(datetime.now()),
            exit_price = filled_px,
            exit_reason= reason_hint,
            pnl_gross  = pnl_gross,
            pnl_net    = pnl_net,
            pnl_r      = pnl_r,
            commission = commission,
        )

        log.info(
            "{sid}: EXIT ({r}) @ {px} | PnL ${p:,.0f} ({rr:+.2f}R)",
            sid=signal_id, r=reason_hint, px=filled_px, p=pnl_net, rr=pnl_r,
        )
        print(f"\n  [EXIT FILL] {signal.get('strategy','')} {market} "
              f"{reason_hint.upper()} @ {filled_px} | "
              f"P&L ${pnl_net:+,.0f} ({pnl_r:+.2f}R)")

        # Notify all registered callbacks (daily P&L tracker, consecutive loss counter, etc.)
        fill_info = {
            "signal_id":   signal_id,
            "market":      market,
            "strategy":    signal.get("strategy", ""),
            "direction":   direction,
            "exit_price":  filled_px,
            "exit_reason": reason_hint,
            "pnl_net":     pnl_net,
            "pnl_r":       pnl_r,
        }
        for cb in self._fill_callbacks:
            try:
                cb(fill_info)
            except Exception as cb_err:
                log.warning("fill_callback error: {e}", e=cb_err)

        # Clean up tracking dicts
        self._open_orders.pop(signal_id, None)
        self._entries.pop(signal_id, None)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _require_connection(self) -> None:
        if not self.is_connected():
            raise ConnectionError(
                "Not connected to TWS. Call bridge.connect() first."
            )

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()


# ============================================================
# QUICK CONNECTION TEST
# Run: conda run -n algobot_env python -m src.execution.ibkr_bridge
# ============================================================

def _test_connection():
    """Test that the IBKR bridge can connect and read account info."""
    print("\n" + "=" * 60)
    print("  AlgoBot IBKR Bridge -- Connection Test")
    print("=" * 60)
    print("  Connecting to TWS paper account (port 7497)...")
    print("  Make sure TWS is running with API enabled.\n")

    bridge = IBKRBridge(paper=True, port=7497)
    if not bridge.connect(timeout=5):
        print("\n  SETUP GUIDE:")
        print("  1. Open TWS and log into your PAPER TRADING account")
        print("  2. Go to: Edit -> Global Configuration -> API -> Settings")
        print("  3. Check:   'Enable ActiveX and Socket Clients'")
        print("  4. Uncheck: 'Read-Only API'")
        print("  5. Socket port: 7497  (paper TWS)")
        print("  6. Add 127.0.0.1 to Trusted IP Addresses")
        print("  7. Click OK, then rerun this test")
        return

    print(f"\n  Account value  : ${bridge.get_account_value():>12,.2f}")
    positions = bridge.get_open_positions()
    if positions:
        print(f"  Open positions :")
        for p in positions:
            print(f"    {p['market']:6} {p['position']:+} @ {p['avg_cost']:.2f}")
    else:
        print(f"  Open positions : None")

    print(f"\n  Connection test PASSED.")
    print(f"  The bot is ready to place paper trades through TWS.")
    print(f"  Run the live signal loop from scripts/run_live_paper.py")
    print("=" * 60 + "\n")

    bridge.disconnect()


if __name__ == "__main__":
    _test_connection()
