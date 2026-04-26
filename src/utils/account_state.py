"""
AlgoBot — Persistent Account State
=====================================
Module:  src/utils/account_state.py
Purpose: Track high-water mark, trailing drawdown, and consecutive losses
         across restarts. State is persisted to data/account_state.json.

TopStep $50k rules enforced here:
  - Trailing drawdown: $2,000 from peak balance (bot halts at $1,800)
  - Daily loss: $1,000 (handled separately in DailyPnLTracker)

Position-sizing reduction on consecutive losses:
  0–1 losses → 1.00x  (full size)
  2 losses   → 0.50x  (half size)
  3+ losses  → 0.25x  (quarter size)

State file layout (JSON):
{
    "starting_balance": 50000.0,       # initial account value (never changes)
    "peak_balance":     50000.0,       # highest equity ever achieved
    "cumulative_pnl":   0.0,           # total P&L since inception
    "daily_pnl": {                     # P&L per calendar date (ET), ISO keys
        "2026-03-27":  450.0,
        "2026-03-28": -200.0
    },
    "consecutive_losses": 0,           # current unbroken losing streak
    "last_updated":  "2026-03-28T16:05:00"
}
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from src.utils.logger import get_logger

log = get_logger(__name__)

_DEFAULT_PATH = Path(__file__).parent.parent.parent / "data" / "account_state.json"


class AccountState:
    """
    Persistent account-level risk state for TopStep compliance.

    Survives process restarts — all mutations are immediately flushed to
    ``data/account_state.json``.

    Usage::

        state = AccountState(starting_balance=50_000.0)

        # After each closed trade:
        state.record_trade(pnl_net=+450.0)

        # Before each new signal:
        signal["size_mult"] = state.get_size_mult()

        # After each trade, check trailing DD:
        alert, halt = state.check_trailing_dd(
            alert_usd=1_500.0, halt_usd=1_800.0
        )
    """

    def __init__(
        self,
        starting_balance: float = 50_000.0,
        state_path: Optional[Path] = None,
    ) -> None:
        self._path    = Path(state_path or _DEFAULT_PATH)
        # Defaults (overwritten by _load if file exists)
        self._starting = starting_balance
        self._peak     = starting_balance
        self._cum_pnl  = 0.0
        self._daily_pnl: dict[str, float] = {}
        self._consec_losses = 0
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Read state from disk; silently use defaults if file is absent/corrupt."""
        if not self._path.exists():
            self._save()
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._starting      = float(data.get("starting_balance",  self._starting))
            self._peak          = float(data.get("peak_balance",       self._starting))
            self._cum_pnl       = float(data.get("cumulative_pnl",     0.0))
            self._daily_pnl     = {k: float(v) for k, v in
                                   data.get("daily_pnl", {}).items()}
            self._consec_losses = int(data.get("consecutive_losses",   0))
            log.info(
                "AccountState: loaded — peak={peak:.0f} cum={cum:+.0f} streak={s}",
                peak=self._peak, cum=self._cum_pnl, s=self._consec_losses,
            )
        except Exception as e:
            log.warning("AccountState: load failed ({e}) — using defaults", e=e)

    def _save(self) -> None:
        """Write state to disk immediately after every mutation."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "starting_balance":  self._starting,
                "peak_balance":      round(self._peak,     2),
                "cumulative_pnl":    round(self._cum_pnl,  2),
                "daily_pnl":         {k: round(v, 2) for k, v in self._daily_pnl.items()},
                "consecutive_losses": self._consec_losses,
                "last_updated":      datetime.now().isoformat(timespec="seconds"),
            }
            self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            log.warning("AccountState: save failed ({e})", e=e)

    # ── Trade recording ───────────────────────────────────────────────────────

    def record_trade(self, pnl_net: float, trade_date: Optional[date] = None) -> None:
        """
        Update persistent state after a trade closes.

        Call exactly once per closed trade (both wins and losses).

        Args:
            pnl_net:    Net P&L of the trade in USD (after commission).
            trade_date: Calendar date of the exit (ET). Defaults to today.
        """
        if trade_date is None:
            trade_date = date.today()
        key = trade_date.isoformat()

        self._cum_pnl += pnl_net
        self._daily_pnl[key] = round(
            self._daily_pnl.get(key, 0.0) + pnl_net, 2
        )

        # High-water mark — ratchets up, never back down
        current = self._starting + self._cum_pnl
        if current > self._peak:
            self._peak = current

        # Consecutive loss streak
        if pnl_net < 0:
            self._consec_losses += 1
        else:
            self._consec_losses = 0

        self._save()
        log.info(
            "AccountState: trade pnl={pnl:+.0f} | cum={cum:+.0f} "
            "| peak={peak:.0f} | dd={dd:.0f} | streak={s}",
            pnl=pnl_net,
            cum=self._cum_pnl,
            peak=self._peak,
            dd=self.trailing_dd_used,
            s=self._consec_losses,
        )

    # ── Position sizing ───────────────────────────────────────────────────────

    def get_size_mult(self) -> float:
        """
        Return position-size multiplier based on current losing streak.

        Streak   Multiplier   Rationale
        0–1      1.00         Full size — edge is intact
        2        0.50         Half size — possible bad run, protect capital
        3+       0.25         Quarter size — preserve account, avoid ruin
        """
        if self._consec_losses >= 3:
            return 0.25
        if self._consec_losses == 2:
            return 0.50
        return 1.0

    # ── Trailing drawdown ─────────────────────────────────────────────────────

    def check_trailing_dd(
        self, alert_usd: float, halt_usd: float
    ) -> tuple[bool, bool]:
        """
        Compare trailing drawdown consumed against TopStep thresholds.

        Returns:
            (alert_triggered, halt_triggered)
            alert_triggered: DD used ≥ alert_usd — send warning, continue
            halt_triggered:  DD used ≥ halt_usd  — cancel positions, pause bot

        Trailing DD = peak_balance − current_balance.
        Even if today was profitable, a prior peak that was higher still
        counts (this is the TopStep rule that surprises many traders).
        """
        dd = self.trailing_dd_used
        return (dd >= alert_usd), (dd >= halt_usd)

    # ── Crash recovery ────────────────────────────────────────────────────────

    def get_today_pnl(self, trade_date: Optional[date] = None) -> float:
        """
        Return the P&L already recorded for a given date.

        Used at bot startup to restore DailyPnLTracker after a crash/restart
        so the daily circuit breaker threshold accounts for trades already done.
        """
        if trade_date is None:
            trade_date = date.today()
        return self._daily_pnl.get(trade_date.isoformat(), 0.0)

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def peak_balance(self) -> float:
        """Highest equity ever achieved (high-water mark)."""
        return self._peak

    @property
    def current_balance(self) -> float:
        """Estimated current account equity (starting_balance + cumulative P&L)."""
        return self._starting + self._cum_pnl

    @property
    def trailing_dd_used(self) -> float:
        """Drawdown consumed from high-water mark, in USD (always ≥ 0)."""
        return max(self._peak - self.current_balance, 0.0)

    @property
    def consecutive_losses(self) -> int:
        """Current unbroken losing streak (reset to 0 on any win)."""
        return self._consec_losses

    @property
    def cumulative_pnl(self) -> float:
        """Total net P&L since account inception."""
        return self._cum_pnl
