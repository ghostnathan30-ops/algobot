"""
AlgoBot -- Trade Database (SQLite)
=====================================
Module:  src/utils/trade_db.py
Phase:   5D -- Institutional Filters
Purpose: Persistent SQLite database for all signals, trades, daily P&L,
         and conditional performance statistics.

         Enables:
           - Live performance tracking during paper/live trading
           - Conditional expectancy lookup ("show me win rate when GLS >= 80")
           - Backtesting result storage for cross-session analysis
           - Quick performance queries without re-running full backtests

Schema:
  signals      -- Every signal fired (including filtered ones), with GLS score
  trades       -- Every executed trade (entry, exit, P&L)
  daily_pnl    -- Aggregated daily P&L across all strategies
  session_meta -- Backtest / paper run metadata

Usage:
    db = TradeDB("data/trades.db")

    # Log a signal
    db.log_signal(signal_id="FHB_ES_2024-03-20", date="2024-03-20",
                  market="ES", strategy="FHB", direction="LONG",
                  gls_score=82, gls_action="FULL_SIZE", filtered=False,
                  filter_reason="")

    # Log trade entry
    db.log_trade_entry(signal_id="FHB_ES_2024-03-20",
                       entry_price=5200.0, stop=5182.5, target=5235.0,
                       contracts=2, risk_usd=350.0)

    # Log trade exit
    db.log_trade_exit(signal_id="FHB_ES_2024-03-20",
                      exit_price=5225.0, exit_reason="target_partial",
                      pnl_usd=500.0, pnl_r=1.0)

    # Query conditional stats
    stats = db.conditional_stats(strategy="FHB", market="ES",
                                 min_gls=70, regime="TRENDING")
    print(stats["win_rate_pct"], stats["avg_pnl"])
"""

from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.logger import get_logger

log = get_logger(__name__)

# Current schema version -- increment when adding columns
_SCHEMA_VERSION = 1

_DDL = """
-- Signals table: every signal fired by any strategy
CREATE TABLE IF NOT EXISTS signals (
    signal_id       TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,         -- ISO datetime UTC
    trade_date      TEXT NOT NULL,         -- YYYY-MM-DD
    market          TEXT NOT NULL,         -- ES, NQ, GC ...
    strategy        TEXT NOT NULL,         -- FHB, ORB, SWING
    direction       TEXT NOT NULL,         -- LONG, SHORT
    regime          TEXT,
    htf_bias        TEXT,
    vix_regime      TEXT,
    vix_level       REAL,
    econ_impact     TEXT,
    gls_score       INTEGER,
    gls_action      TEXT,                  -- FULL_SIZE, HALF_SIZE, SKIP
    filtered        INTEGER NOT NULL DEFAULT 0,   -- 1 = filtered out (not traded)
    filter_reason   TEXT,
    extra_json      TEXT                   -- arbitrary extra data as JSON
);

-- Trades table: every executed trade
CREATE TABLE IF NOT EXISTS trades (
    signal_id       TEXT PRIMARY KEY REFERENCES signals(signal_id),
    entry_time      TEXT,
    entry_price     REAL,
    stop_price      REAL,
    target_price    REAL,
    contracts       INTEGER,
    risk_usd        REAL,
    size_mult       REAL,
    -- Exit fields (filled on close)
    exit_time       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,
    pnl_gross       REAL,
    commission      REAL,
    pnl_net         REAL,
    pnl_r           REAL,
    bars_held       INTEGER,
    is_overnight    INTEGER DEFAULT 0,
    is_multiday     INTEGER DEFAULT 0
);

-- Daily aggregated P&L
CREATE TABLE IF NOT EXISTS daily_pnl (
    trade_date      TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    market          TEXT NOT NULL,
    signals_fired   INTEGER DEFAULT 0,
    signals_traded  INTEGER DEFAULT 0,
    signals_filtered INTEGER DEFAULT 0,
    trades_open     INTEGER DEFAULT 0,
    trades_closed   INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    pnl_gross       REAL DEFAULT 0.0,
    pnl_net         REAL DEFAULT 0.0,
    PRIMARY KEY (trade_date, strategy, market)
);

-- Session metadata (one row per backtest / paper run)
CREATE TABLE IF NOT EXISTS session_meta (
    session_id      TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    mode            TEXT,          -- 'backtest', 'paper', 'live'
    strategy        TEXT,
    market          TEXT,
    start_date      TEXT,
    end_date        TEXT,
    initial_capital REAL,
    final_equity    REAL,
    total_trades    INTEGER,
    profit_factor   REAL,
    max_dd_pct      REAL,
    config_json     TEXT
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);
"""

_IDX = """
CREATE INDEX IF NOT EXISTS idx_signals_date_market   ON signals  (trade_date, market);
CREATE INDEX IF NOT EXISTS idx_signals_strategy_gls  ON signals  (strategy, gls_score);
CREATE INDEX IF NOT EXISTS idx_trades_exit_reason     ON trades   (exit_reason);
CREATE INDEX IF NOT EXISTS idx_daily_date_strategy    ON daily_pnl(trade_date, strategy);
"""


class TradeDB:
    """
    SQLite-backed trade database.

    Args:
        db_path: Path to the SQLite file. Created if it doesn't exist.
                 Use ':memory:' for in-memory testing.
    """

    def __init__(self, db_path: str | Path = "data/trades.db"):
        self._path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._open()

    # ── Connection management ──────────────────────────────────────────────────

    def _open(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._apply_schema()

    def _apply_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(_DDL)
        cur.executescript(_IDX)
        # Record schema version
        cur.execute(
            "INSERT OR IGNORE INTO schema_meta (version, applied_at) VALUES (?, ?)",
            (_SCHEMA_VERSION, _now()),
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    # ── Signal logging ─────────────────────────────────────────────────────────

    def log_signal(
        self,
        signal_id:    str,
        trade_date:   str,
        market:       str,
        strategy:     str,
        direction:    str,
        gls_score:    int   = 0,
        gls_action:   str   = "SKIP",
        filtered:     bool  = False,
        filter_reason: str  = "",
        regime:       str   = "",
        htf_bias:     str   = "",
        vix_regime:   str   = "",
        vix_level:    float = float("nan"),
        econ_impact:  str   = "NONE",
        extra:        Dict  = None,
    ) -> None:
        """Insert or replace a signal record."""
        self._conn.execute(
            """INSERT OR REPLACE INTO signals
               (signal_id, created_at, trade_date, market, strategy, direction,
                regime, htf_bias, vix_regime, vix_level, econ_impact,
                gls_score, gls_action, filtered, filter_reason, extra_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                signal_id, _now(), trade_date, market, strategy, direction,
                regime, htf_bias, vix_regime,
                None if _isnan(vix_level) else vix_level,
                econ_impact, gls_score, gls_action,
                int(filtered), filter_reason,
                json.dumps(extra) if extra else None,
            ),
        )
        self._conn.commit()

    # ── Trade logging ──────────────────────────────────────────────────────────

    def log_trade_entry(
        self,
        signal_id:   str,
        entry_time:  str,
        entry_price: float,
        stop_price:  float,
        target_price: float,
        contracts:   int   = 1,
        risk_usd:    float = 0.0,
        size_mult:   float = 1.0,
    ) -> None:
        """Insert trade entry record."""
        self._conn.execute(
            """INSERT OR REPLACE INTO trades
               (signal_id, entry_time, entry_price, stop_price, target_price,
                contracts, risk_usd, size_mult)
               VALUES (?,?,?,?,?,?,?,?)""",
            (signal_id, entry_time, entry_price, stop_price, target_price,
             contracts, risk_usd, size_mult),
        )
        self._conn.commit()

    def log_trade_exit(
        self,
        signal_id:   str,
        exit_time:   str,
        exit_price:  float,
        exit_reason: str,
        pnl_gross:   float,
        pnl_net:     float,
        pnl_r:       float = 0.0,
        commission:  float = 0.0,
        bars_held:   int   = 0,
        is_overnight: bool = False,
        is_multiday:  bool = False,
    ) -> None:
        """Update trade with exit data."""
        self._conn.execute(
            """UPDATE trades SET
               exit_time=?, exit_price=?, exit_reason=?,
               pnl_gross=?, commission=?, pnl_net=?, pnl_r=?,
               bars_held=?, is_overnight=?, is_multiday=?
               WHERE signal_id=?""",
            (exit_time, exit_price, exit_reason,
             pnl_gross, commission, pnl_net, pnl_r,
             bars_held, int(is_overnight), int(is_multiday),
             signal_id),
        )
        self._conn.commit()

    # ── Daily P&L ─────────────────────────────────────────────────────────────

    def update_daily_pnl(
        self,
        trade_date:       str,
        strategy:         str,
        market:           str,
        signals_fired:    int   = 0,
        signals_traded:   int   = 0,
        signals_filtered: int   = 0,
        trades_open:      int   = 0,
        trades_closed:    int   = 0,
        wins:             int   = 0,
        losses:           int   = 0,
        pnl_gross:        float = 0.0,
        pnl_net:          float = 0.0,
    ) -> None:
        """Upsert a daily P&L record."""
        self._conn.execute(
            """INSERT INTO daily_pnl
               (trade_date, strategy, market,
                signals_fired, signals_traded, signals_filtered,
                trades_open, trades_closed, wins, losses, pnl_gross, pnl_net)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trade_date, strategy, market) DO UPDATE SET
               signals_fired    = signals_fired    + excluded.signals_fired,
               signals_traded   = signals_traded   + excluded.signals_traded,
               signals_filtered = signals_filtered + excluded.signals_filtered,
               trades_open      = excluded.trades_open,
               trades_closed    = trades_closed    + excluded.trades_closed,
               wins             = wins             + excluded.wins,
               losses           = losses           + excluded.losses,
               pnl_gross        = pnl_gross        + excluded.pnl_gross,
               pnl_net          = pnl_net          + excluded.pnl_net""",
            (trade_date, strategy, market,
             signals_fired, signals_traded, signals_filtered,
             trades_open, trades_closed, wins, losses, pnl_gross, pnl_net),
        )
        self._conn.commit()

    # ── Conditional statistics ─────────────────────────────────────────────────

    def conditional_stats(
        self,
        strategy:  str = "",
        market:    str = "",
        min_gls:   int = 0,
        max_gls:   int = 100,
        regime:    str = "",
        direction: str = "",
    ) -> Dict[str, Any]:
        """
        Query win rate and average P&L for trades matching the given conditions.

        Returns a dict with keys:
            n_trades, win_rate_pct, avg_pnl_net, avg_pnl_r,
            total_pnl_net, max_dd_net, conditions (the query used)
        """
        where = ["t.exit_price IS NOT NULL"]  # closed trades only
        params: List[Any] = []

        if strategy:
            where.append("s.strategy = ?");  params.append(strategy.upper())
        if market:
            where.append("s.market = ?");    params.append(market.upper())
        if regime:
            where.append("s.regime = ?");    params.append(regime.upper())
        if direction:
            where.append("s.direction = ?"); params.append(direction.upper())
        where.append("s.gls_score >= ?");   params.append(min_gls)
        where.append("s.gls_score <= ?");   params.append(max_gls)

        sql = f"""
            SELECT
                COUNT(*)                        AS n_trades,
                SUM(CASE WHEN t.pnl_net > 0 THEN 1 ELSE 0 END) AS n_wins,
                AVG(t.pnl_net)                  AS avg_pnl_net,
                AVG(t.pnl_r)                    AS avg_pnl_r,
                SUM(t.pnl_net)                  AS total_pnl_net,
                MIN(t.pnl_net)                  AS worst_trade
            FROM trades t
            JOIN signals s ON s.signal_id = t.signal_id
            WHERE {" AND ".join(where)}
        """
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()

        if not row or row["n_trades"] == 0:
            return {
                "n_trades": 0, "win_rate_pct": 0.0, "avg_pnl_net": 0.0,
                "avg_pnl_r": 0.0, "total_pnl_net": 0.0, "max_dd_net": 0.0,
                "conditions": {"strategy": strategy, "market": market,
                               "regime": regime, "min_gls": min_gls, "max_gls": max_gls},
            }

        n   = row["n_trades"] or 0
        win = row["n_wins"]   or 0
        return {
            "n_trades":      n,
            "win_rate_pct":  round(100.0 * win / n, 1) if n else 0.0,
            "avg_pnl_net":   round(row["avg_pnl_net"] or 0.0, 2),
            "avg_pnl_r":     round(row["avg_pnl_r"]   or 0.0, 4),
            "total_pnl_net": round(row["total_pnl_net"] or 0.0, 2),
            "worst_trade":   round(row["worst_trade"]  or 0.0, 2),
            "conditions":    {
                "strategy": strategy, "market": market, "regime": regime,
                "direction": direction, "min_gls": min_gls, "max_gls": max_gls,
            },
        }

    # ── Convenience queries ────────────────────────────────────────────────────

    def get_recent_trades(self, n: int = 20) -> List[Dict]:
        """Return the N most recent closed trades."""
        cur = self._conn.execute(
            """SELECT s.trade_date, s.market, s.strategy, s.direction,
                      s.gls_score, t.entry_price, t.exit_price,
                      t.exit_reason, t.pnl_net, t.pnl_r
               FROM trades t JOIN signals s ON s.signal_id = t.signal_id
               WHERE t.exit_price IS NOT NULL
               ORDER BY t.exit_time DESC LIMIT ?""",
            (n,),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_daily_summary(self, last_n_days: int = 30) -> List[Dict]:
        """Return daily P&L summary for the last N trading days."""
        cur = self._conn.execute(
            """SELECT trade_date,
                      SUM(signals_traded)   AS signals,
                      SUM(wins)             AS wins,
                      SUM(losses)           AS losses,
                      SUM(pnl_net)          AS pnl_net
               FROM daily_pnl
               GROUP BY trade_date
               ORDER BY trade_date DESC
               LIMIT ?""",
            (last_n_days,),
        )
        return [dict(r) for r in cur.fetchall()]

    def log_session(
        self,
        session_id: str,
        mode: str,
        strategy: str,
        market: str,
        start_date: str,
        end_date: str,
        initial_capital: float,
        final_equity: float = 0.0,
        total_trades: int = 0,
        profit_factor: float = 0.0,
        max_dd_pct: float = 0.0,
        config: Dict = None,
    ) -> None:
        """Record a backtest or trading session."""
        self._conn.execute(
            """INSERT OR REPLACE INTO session_meta
               (session_id, started_at, mode, strategy, market,
                start_date, end_date, initial_capital, final_equity,
                total_trades, profit_factor, max_dd_pct, config_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, _now(), mode, strategy, market,
             start_date, end_date, initial_capital, final_equity,
             total_trades, profit_factor, max_dd_pct,
             json.dumps(config) if config else None),
        )
        self._conn.commit()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds")

def _isnan(v) -> bool:
    try:
        import math
        return math.isnan(v)
    except Exception:
        return False
