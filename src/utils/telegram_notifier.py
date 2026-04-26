"""
AlgoBot — Telegram Notifier
============================
Module:  src/utils/telegram_notifier.py
Purpose: Fire-and-forget Telegram alerts for every trade event.

Setup (one-time, ~2 minutes):
  1. Open Telegram → search for @BotFather → /newbot → follow prompts.
     Copy the bot token (looks like: 123456789:ABCDef...).

  2. Start a conversation with your new bot (click Start or send it any message).

  3. Get your chat_id — run this in terminal:
       curl "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
     Look for "chat" → "id" in the JSON response (a number like 987654321).

  4. Add to config/config.yaml:
       telegram:
         enabled:   true
         bot_token: "123456789:ABCDef..."
         chat_id:   "987654321"

All sends are non-blocking (fire-and-forget).
If Telegram is unreachable or misconfigured, alerts are silently dropped —
the trading loop is never blocked.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional

from src.utils.logger import get_logger

log = get_logger(__name__)

_SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    """
    Async-friendly, sync-safe Telegram message sender.

    Async context (inside asyncio loop):
        asyncio.create_task(notifier.send_async("Hello!"))

    Sync context (anywhere):
        notifier.send("Hello!")   # spawns a daemon thread, returns immediately

    If enabled=False or token/chat_id are empty, all calls are no-ops.
    """

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True) -> None:
        self._token   = bot_token.strip()
        self._chat_id = str(chat_id).strip()
        self._enabled = enabled and bool(self._token) and bool(self._chat_id)
        self._url     = _SEND_URL.format(token=self._token)
        if self._enabled:
            log.info("TelegramNotifier: enabled (chat_id={c})", c=self._chat_id)
        else:
            log.info("TelegramNotifier: disabled (no token/chat_id configured)")

    @classmethod
    def from_config(cls, config: dict) -> "TelegramNotifier":
        """Construct from the top-level config dict (reads config.telegram)."""
        tg = config.get("telegram", {})
        return cls(
            bot_token = str(tg.get("bot_token", "")),
            chat_id   = str(tg.get("chat_id",   "")),
            enabled   = bool(tg.get("enabled",  False)),
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Async send ────────────────────────────────────────────────────────────

    async def send_async(self, text: str) -> None:
        """Send a message asynchronously. Never raises."""
        if not self._enabled:
            return
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(self._url, json={
                    "chat_id":    self._chat_id,
                    "text":       text,
                    "parse_mode": "HTML",
                })
                if resp.status_code != 200:
                    log.warning("Telegram: non-200 response {s}", s=resp.status_code)
        except Exception as exc:
            log.warning("Telegram: send_async failed: {e}", e=exc)

    # ── Sync send (fire-and-forget via daemon thread) ─────────────────────────

    def send(self, text: str) -> None:
        """Send a message in a background thread — non-blocking, sync-safe."""
        if not self._enabled:
            return
        threading.Thread(target=self._send_sync, args=(text,), daemon=True).start()

    def _send_sync(self, text: str) -> None:
        try:
            import httpx
            httpx.post(self._url, json={
                "chat_id":    self._chat_id,
                "text":       text,
                "parse_mode": "HTML",
            }, timeout=5.0)
        except Exception as exc:
            log.warning("Telegram: send_sync failed: {e}", e=exc)


# ── Message formatters ─────────────────────────────────────────────────────────
# Keep message format consistent across all call sites.

def fmt_startup(mode: str, strategies: list[str], time_str: str) -> str:
    strats = " + ".join(strategies) if strategies else "none"
    return (
        f"🚀 <b>AlgoBot started</b>\n"
        f"Mode: {mode} | {strats}\n"
        f"{time_str}"
    )


def fmt_limit_queued(strategy: str, market: str, direction: str,
                     limit: float, stop: float, target: float,
                     expires: str) -> str:
    sign = "▲" if direction == "LONG" else "▼"
    return (
        f"⏳ <b>{strategy} {market} {sign}{direction}</b> — Limit queued\n"
        f"Limit: {limit:,.2f} | Stop: {stop:,.2f} | Target: {target:,.2f}\n"
        f"Expires: {expires}"
    )


def fmt_entry(strategy: str, market: str, direction: str,
              entry: float, stop: float, target: float,
              contracts: int, risk_usd: float) -> str:
    sign = "▲" if direction == "LONG" else "▼"
    return (
        f"✅ <b>{strategy} {market} {sign}{direction}</b> — Entry filled\n"
        f"@ {entry:,.2f} | {contracts} contract{'s' if contracts > 1 else ''}\n"
        f"Stop: {stop:,.2f} | Target: {target:,.2f}\n"
        f"Risk: ${risk_usd:,.0f}"
    )


def fmt_exit(strategy: str, market: str, direction: str,
             exit_price: float, pnl_net: float, pnl_r: float,
             reason: str) -> str:
    sign = "▲" if direction == "LONG" else "▼"
    pnl_str = f"${pnl_net:+,.0f}"

    if reason == "target" or "target" in reason:
        icon = "🟢"
        tag  = "TARGET HIT"
    elif reason == "stop" or "stop" in reason:
        icon = "🔴"
        tag  = "STOPPED OUT"
    elif reason in ("eod", "time", "overnight_time", "cancelled"):
        icon = "⏰"
        tag  = "Time exit"
    else:
        icon = "⚪"
        tag  = reason.replace("_", " ").title()

    return (
        f"{icon} <b>{strategy} {market} {sign}{direction}</b> — {tag}\n"
        f"Exit @ {exit_price:,.2f} | P&amp;L: <b>{pnl_str}</b> ({pnl_r:+.2f}R)"
    )


def fmt_loss_alert(pnl_today: float, hard_stop: float) -> str:
    remaining = hard_stop - abs(pnl_today)
    return (
        f"⚠️ <b>Daily Loss Alert</b>\n"
        f"P&amp;L today: ${pnl_today:+,.0f}\n"
        f"${remaining:,.0f} from hard stop. Tighten risk."
    )


def fmt_hard_stop(pnl_today: float) -> str:
    return (
        f"🛑 <b>HARD STOP TRIGGERED</b>\n"
        f"Daily P&amp;L: ${pnl_today:+,.0f}\n"
        f"All positions cancelled. No new signals today."
    )


def fmt_eod(date_str: str, trades: int, wins: int,
            pnl_today: float, best: Optional[dict] = None,
            worst: Optional[dict] = None) -> str:
    wr   = f"{wins/trades*100:.0f}%" if trades > 0 else "—"
    pnl  = f"${pnl_today:+,.0f}"
    icon = "📈" if pnl_today >= 0 else "📉"
    msg  = (
        f"{icon} <b>EOD Summary — {date_str}</b>\n"
        f"Trades: {trades} | Wins: {wins} | WR: {wr}\n"
        f"Net P&amp;L: <b>{pnl}</b>"
    )
    if best and best.get("pnl_net", 0) > 0:
        msg += f"\nBest: +${best['pnl_net']:,.0f} ({best.get('strategy','?')} {best.get('market','?')})"
    if worst and worst.get("pnl_net", 0) < 0:
        msg += f"\nWorst: ${worst['pnl_net']:,.0f} ({worst.get('strategy','?')} {worst.get('market','?')})"
    return msg


def fmt_limit_expired(market: str, direction: str, limit: float) -> str:
    sign = "▲" if direction == "LONG" else "▼"
    return (
        f"❌ <b>Limit expired</b> — {market} {sign}{direction}\n"
        f"Price never retraced to {limit:,.2f}"
    )


def fmt_dd_warning(dd_used: float, dd_limit: float) -> str:
    remaining = dd_limit - dd_used
    return (
        f"⚠️ <b>Trailing DD Warning</b>\n"
        f"DD used: ${dd_used:,.0f} / ${dd_limit:,.0f} limit\n"
        f"${remaining:,.0f} remaining. Tighten risk."
    )


def fmt_dd_halt(dd_used: float, dd_limit: float) -> str:
    return (
        f"🛑 <b>TRAILING DD LIMIT — BOT PAUSED</b>\n"
        f"DD used: ${dd_used:,.0f} (limit: ${dd_limit:,.0f})\n"
        f"All positions cancelled. Restart required to resume."
    )
