"""
AlgoBot — Trade Dataclass
==========================
Module:  src/backtest/trade.py
Phase:   3 — Backtesting Engine
Purpose: Represents one complete trade from entry to exit.
         Every trade the engine executes is stored as a Trade object.
         Used by metrics.py, walk_forward.py, and monte_carlo.py.

Fields are grouped into four sections:
  1. Identity      — market, direction, strategy, signal source
  2. Entry         — date, price (raw + slippage-adjusted), size, stop, risk
  3. Exit          — date, price (raw + slippage-adjusted), reason
  4. Results       — P&L (gross, net), R-multiple, MAE, MFE, bars held

Exit reasons:
  "stop_loss"       — Initial stop price hit (bar Low < stop for longs)
  "trailing_stop"   — Trailing stop price hit (after activation at +1R)
  "profit_target"   — Profit target price hit (Phase 6+, e.g. 2.5R)
  "dcs_exit"        — DCS 20-bar channel exit fired
  "tma_flip"        — TMA signal flipped to opposite direction
  "vmr_exit"        — VMR RSI returned to neutral zone
  "vmr_timeout"     — VMR max_hold_bars exceeded (no RSI recovery)
  "daily_hard_stop" — Bot's daily loss limit hit, all positions closed
  "end_of_data"     — Backtest ended with position still open (force close)
  "signal_reversal" — Opposite direction signal fired, position reversed
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Optional


# ── Open position (in-flight, managed by the engine) ─────────────────────────

@dataclass
class OpenPosition:
    """
    Represents a trade that is currently open.
    Managed by BacktestEngine. Converted to a Trade when closed.
    """
    trade_id:       int
    market:         str
    direction:      str    # "LONG" or "SHORT"
    strategy:       str    # "TREND" or "VMR"
    signal_source:  str    # "AGREE_LONG", "AGREE_SHORT", "VMR_LONG", "VMR_SHORT"

    # Entry details
    entry_date:     datetime.date
    entry_bar_idx:  int            # Integer index in the DataFrame
    entry_price:    float          # Close of signal bar (raw)
    entry_price_adj: float         # After slippage (actual fill price)
    position_size:  float          # Units (fractional ETF proxy in backtest)
    stop_price:     float          # Initial hard stop
    point_value:    float          # $1 for ETF proxy, $50 for live ES
    initial_risk_dollars: float    # position_size * stop_distance * point_value

    # Regime context at entry
    regime_at_entry:   str   = ""
    size_multiplier:   float = 1.0
    atr_at_entry:      float = 0.0

    # Running state (updated each bar)
    bars_held:          int   = 0
    highest_close:      float = 0.0   # For trailing stop (long positions)
    lowest_close:       float = 0.0   # For trailing stop (short positions)
    trailing_active:    bool  = False
    trailing_stop_price: Optional[float] = None

    # Profit target and breakeven management (Phase 6+)
    # stop_dist is stored so the engine can compute target prices from entry
    stop_dist:              float = 0.0    # Initial stop distance in price points
    profit_target_r:        float = 2.5    # Close 100% at this R (0 = disabled)
    breakeven_move_r:       float = 1.5    # Move stop to entry_price_adj at this R
    breakeven_activated:    bool  = False  # True once stop has been moved to BE

    # Max adverse / favorable excursion tracking
    max_adverse_excursion:   float = 0.0  # Worst unrealised loss (in price points)
    max_favorable_excursion: float = 0.0  # Best unrealised gain (in price points)

    def __post_init__(self):
        self.highest_close = self.entry_price
        self.lowest_close  = self.entry_price

    def unrealised_pnl(self, current_price: float) -> float:
        """Current unrealised P&L in dollars."""
        direction_mult = 1.0 if self.direction == "LONG" else -1.0
        return (current_price - self.entry_price_adj) * direction_mult * self.position_size * self.point_value

    def current_r(self, current_price: float) -> float:
        """Current R-multiple (positive = winning)."""
        if self.initial_risk_dollars <= 0:
            return 0.0
        return self.unrealised_pnl(current_price) / self.initial_risk_dollars

    def __str__(self) -> str:
        return (
            f"[Open] {self.market} {self.direction} {self.strategy} | "
            f"Entry={self.entry_price_adj:.4f} Stop={self.stop_price:.4f} "
            f"Size={self.position_size:.3f} Risk=${self.initial_risk_dollars:.0f} | "
            f"Bars={self.bars_held}"
        )


# ── Closed trade (historical record) ─────────────────────────────────────────

@dataclass
class Trade:
    """
    A fully closed trade. Immutable record of the complete lifecycle.
    This is what metrics.py, walk_forward.py, and monte_carlo.py consume.
    """
    trade_id:       int
    market:         str
    direction:      str    # "LONG" or "SHORT"
    strategy:       str    # "TREND" or "VMR"
    signal_source:  str    # "AGREE_LONG", "AGREE_SHORT", "VMR_LONG", "VMR_SHORT"

    # Entry
    entry_date:       datetime.date
    entry_bar_idx:    int
    entry_price:      float   # Raw (close of signal bar)
    entry_price_adj:  float   # After entry slippage
    position_size:    float
    stop_price:       float
    point_value:      float
    initial_risk_dollars: float

    # Exit
    exit_date:        datetime.date
    exit_bar_idx:     int
    exit_price:       float   # Raw exit price
    exit_price_adj:   float   # After exit slippage
    exit_reason:      str

    # P&L
    pnl_gross:        float   # (exit_adj - entry_adj) × size × point_value × direction
    commission:       float   # Round-trip commission
    pnl_net:          float   # pnl_gross - commission
    pnl_r:            float   # pnl_net / initial_risk_dollars

    # Context
    regime_at_entry:      str   = ""
    size_multiplier:      float = 1.0
    atr_at_entry:         float = 0.0
    bars_held:            int   = 0
    max_adverse_excursion:   float = 0.0   # Points
    max_favorable_excursion: float = 0.0   # Points

    # ── Derived properties ─────────────────────────────────────────────────────

    @property
    def is_winner(self) -> bool:
        return self.pnl_net > 0

    @property
    def is_loser(self) -> bool:
        return self.pnl_net < 0

    @property
    def is_trend(self) -> bool:
        return self.strategy == "TREND"

    @property
    def is_vmr(self) -> bool:
        return self.strategy == "VMR"

    @property
    def is_long(self) -> bool:
        return self.direction == "LONG"

    @property
    def is_short(self) -> bool:
        return self.direction == "SHORT"

    def __str__(self) -> str:
        result = "WIN" if self.is_winner else "LOSS"
        return (
            f"[{result}] Trade#{self.trade_id} {self.market} {self.direction} "
            f"{self.strategy} | "
            f"Entry={self.entry_date} @{self.entry_price_adj:.4f} | "
            f"Exit={self.exit_date} @{self.exit_price_adj:.4f} ({self.exit_reason}) | "
            f"PnL=${self.pnl_net:+.2f} ({self.pnl_r:+.2f}R) | "
            f"{self.bars_held} bars"
        )


# ── Builder: OpenPosition -> Trade ────────────────────────────────────────────

def close_position(
    pos: OpenPosition,
    exit_date: datetime.date,
    exit_bar_idx: int,
    exit_price_raw: float,
    exit_price_adj: float,
    exit_reason: str,
    commission_per_rt: float = 10.0,
) -> Trade:
    """
    Convert an OpenPosition to a closed Trade.

    Called by the BacktestEngine when any exit condition is met.

    Args:
        pos:               The open position being closed
        exit_date:         Date of the exit bar
        exit_bar_idx:      Integer index of the exit bar
        exit_price_raw:    Raw close price of exit bar
        exit_price_adj:    After exit slippage (worse than raw for the trader)
        exit_reason:       String code for why the trade was closed
        commission_per_rt: Round-trip commission in dollars ($10 default)

    Returns:
        Trade object with all P&L and metrics computed.
    """
    direction_mult = 1.0 if pos.direction == "LONG" else -1.0

    pnl_gross = (
        (exit_price_adj - pos.entry_price_adj)
        * direction_mult
        * pos.position_size
        * pos.point_value
    )
    pnl_net = pnl_gross - commission_per_rt

    # R-multiple: how many initial risks did we gain/lose?
    if pos.initial_risk_dollars > 0:
        pnl_r = pnl_net / pos.initial_risk_dollars
    else:
        pnl_r = 0.0

    return Trade(
        trade_id=pos.trade_id,
        market=pos.market,
        direction=pos.direction,
        strategy=pos.strategy,
        signal_source=pos.signal_source,

        entry_date=pos.entry_date,
        entry_bar_idx=pos.entry_bar_idx,
        entry_price=pos.entry_price,
        entry_price_adj=pos.entry_price_adj,
        position_size=pos.position_size,
        stop_price=pos.stop_price,
        point_value=pos.point_value,
        initial_risk_dollars=pos.initial_risk_dollars,

        exit_date=exit_date,
        exit_bar_idx=exit_bar_idx,
        exit_price=exit_price_raw,
        exit_price_adj=exit_price_adj,
        exit_reason=exit_reason,

        pnl_gross=round(pnl_gross, 2),
        commission=round(commission_per_rt, 2),
        pnl_net=round(pnl_net, 2),
        pnl_r=round(pnl_r, 4),

        regime_at_entry=pos.regime_at_entry,
        size_multiplier=pos.size_multiplier,
        atr_at_entry=pos.atr_at_entry,
        bars_held=pos.bars_held,
        max_adverse_excursion=round(pos.max_adverse_excursion, 4),
        max_favorable_excursion=round(pos.max_favorable_excursion, 4),
    )


# ── Backtest result container ──────────────────────────────────────────────────

@dataclass
class BacktestResult:
    """
    Full output of a BacktestEngine.run() call.
    Contains all trades, equity curve, and computed metrics.
    """
    start_date:      str
    end_date:        str
    markets:         list
    initial_capital: float
    trades:          list          # list[Trade]
    equity_curve:    "pd.Series"   # DatetimeIndex -> equity float
    daily_pnl:       "pd.Series"   # DatetimeIndex -> daily P&L float
    metrics:         dict = field(default_factory=dict)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def final_equity(self) -> float:
        return float(self.equity_curve.iloc[-1]) if len(self.equity_curve) > 0 else self.initial_capital

    @property
    def total_return_pct(self) -> float:
        return (self.final_equity / self.initial_capital - 1.0) * 100.0

    def __str__(self) -> str:
        pf   = self.metrics.get("profit_factor",  "N/A")
        sr   = self.metrics.get("sharpe_ratio",   "N/A")
        dd   = self.metrics.get("max_drawdown_pct", "N/A")
        wr   = self.metrics.get("win_rate_pct",   "N/A")
        ret  = self.metrics.get("total_return_pct", "N/A")

        pf_str  = f"{pf:.2f}" if isinstance(pf, float) else str(pf)
        sr_str  = f"{sr:.2f}" if isinstance(sr, float) else str(sr)
        dd_str  = f"{dd:.1f}%" if isinstance(dd, float) else str(dd)
        wr_str  = f"{wr:.1f}%" if isinstance(wr, float) else str(wr)
        ret_str = f"{ret:.1f}%" if isinstance(ret, float) else str(ret)

        return (
            f"BacktestResult: {self.start_date} to {self.end_date} | "
            f"{len(self.markets)} markets | "
            f"{self.total_trades} trades | "
            f"Return={ret_str} | PF={pf_str} | Sharpe={sr_str} | "
            f"MaxDD={dd_str} | WinRate={wr_str}"
        )
