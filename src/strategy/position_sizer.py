"""
AlgoBot — Position Sizer
=========================
Module:  src/strategy/position_sizer.py
Phase:   2 — Strategy Signals
Purpose: Calculates the correct position size for each trade.
         The position sizer is the single most important risk control
         in the entire system.

Core principle: Risk 1% of account per trade.
  If the account is $150,000:
    1% risk = $1,500 per trade
    ATR(20) stop for trend  = 2.5 x ATR (stop is 2.5 ATR from entry)
    ATR(20) stop for VMR    = 1.5 x ATR (tighter — VMR is quicker to invalidate)

  Position size = Risk dollars / Dollar risk per contract
  Dollar risk per contract = Stop distance in points x Point value

Example (ES futures, TRENDING):
  Account:      $150,000
  Risk (1%):    $1,500
  ATR:          35.0 points (typical for ES)
  Stop:         2.5 x 35 = 87.5 points from entry
  ES point val: $50 per point
  Dollar risk per contract: 87.5 x $50 = $4,375
  Contracts: 1,500 / 4,375 = 0.34 -> rounds DOWN to 0 contracts

WAIT — that would mean no trade? Let me explain:

  In this case we can't trade with 1% risk AND use 2.5 ATR stops on
  a $150,000 account for ES. Our options:
    a) Increase risk to 2% (increases drawdown risk — not preferred)
    b) Use tighter stop (increases stop-out rate — not preferred)
    c) Cap minimum at 1 contract (means risk is higher than 1% this trade)

  For Topstep phase (starting), we use a minimum of 1 contract.
  This means risk on some trades will be 2-3% of the account.
  This is acceptable because:
    - Topstep provides $150k buying power but your actual capital at risk
      is the evaluation fee (~$165/month), not $150k of personal money
    - The risk system is protecting Topstep's drawdown limits, not
      protecting your personal net worth (that happens in Phase 7)

  For a PERSONAL $150k account (not Topstep), you would need ~$500,000
  to properly size ES futures at 1 contract = 1% risk with 2.5 ATR stops.
  This is why serious prop firms use multiple smaller contracts and
  micro futures ($5/point MES) for smaller accounts.

  In backtesting (Phases 2-5): We use ETF proxies where fractional
  sizing is possible. The position_sizer returns a fractional "unit"
  size (e.g., 0.34 units of SPY) for backtesting, and the actual
  contract count for live trading.

Regime size multiplier:
  TRENDING:      1.0x (full size)
  RANGING:       1.0x (full size, VMR only)
  HIGH_VOL:      0.5x (half size — ATR spike, protect capital)
  TRANSITIONING: 0.0x (no new entries)
  CRISIS:        0.0x (no new entries)
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd
import numpy as np

from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Position sizing result ────────────────────────────────────────────────────

@dataclass
class SizingResult:
    """
    Complete position sizing output for one trade.
    Contains the size AND all the inputs that produced it.
    """
    market:              str
    trade_type:          str    # "TREND" or "VMR"
    account_equity:      float  # USD
    risk_fraction:       float  # e.g., 0.01 for 1%
    risk_dollars:        float  # account_equity * risk_fraction
    atr:                 float  # Current ATR value
    stop_multiplier:     float  # 2.5 for trend, 1.5 for VMR
    stop_distance:       float  # stop_multiplier * atr (in price points)
    point_value:         float  # Contract value per point (e.g., $50 for ES)
    dollar_risk_per_unit: float # stop_distance * point_value
    raw_size:            float  # risk_dollars / dollar_risk_per_unit (before rounding)
    size:                float  # Final size (rounded down, minimum 1 for live)
    size_multiplier:     float  # From regime (1.0, 0.5, 0.0)
    final_size:          float  # size * size_multiplier (0 if regime blocks)
    stop_price:          Optional[float] = None  # Actual stop price (entry +/- stop_distance)

    def __str__(self) -> str:
        return (
            f"{self.market} {self.trade_type}: "
            f"{self.final_size:.2f} units | "
            f"risk=${self.risk_dollars:.0f} | "
            f"stop={self.stop_distance:.2f}pts ({self.stop_multiplier}x ATR) | "
            f"regime_mult={self.size_multiplier:.1f}x"
        )


# ── Market point values ───────────────────────────────────────────────────────
# These must match config.yaml. Defined here as fallback defaults.
# Live trading always reads from config.yaml via the config dict.

DEFAULT_POINT_VALUES = {
    "ES":  50.0,   # $50 per point
    "NQ":  20.0,   # $20 per point
    "GC":  100.0,  # $100 per troy oz (1 contract = 100 oz)
    "CL":  1000.0, # $1000 per dollar (1 contract = 1000 barrels)
    "ZB":  1000.0, # $1000 per point
    "6E":  125000.0, # EUR 125,000 per contract (value in USD varies)
}

# For ETF proxy backtesting, use $1 per unit (fractional sizing)
# The backtest will scale P&L separately using contract multipliers
ETF_POINT_VALUE = 1.0


# ── Core sizing function ──────────────────────────────────────────────────────

def calculate_position_size(
    market:          str,
    trade_type:      str,
    atr:             float,
    account_equity:  float,
    size_multiplier: float,
    config:          dict,
    entry_price:     Optional[float] = None,
    is_long:         bool            = True,
    use_etf_sizing:  bool            = True,
) -> SizingResult:
    """
    Calculate the position size for a single trade.

    Args:
        market:          Market code ("ES", "NQ", etc.)
        trade_type:      "TREND" or "VMR" (affects stop multiplier)
        atr:             Current ATR value (from calculate_indicators)
        account_equity:  Current account value in USD
        size_multiplier: From regime (1.0 full, 0.5 half, 0.0 no trade)
        config:          Strategy config dict. Required keys:
                           risk_per_trade_pct       (e.g., 1.0 for 1%)
                           stop_multiplier_trend    (e.g., 2.5)
                           stop_multiplier_mr       (e.g., 1.5)
        entry_price:     Entry price (used to calculate stop_price)
        is_long:         True for long (stop below entry), False for short
        use_etf_sizing:  True for backtesting with ETF proxies (fractional)
                         False for live trading (whole contracts)

    Returns:
        SizingResult with final_size and all inputs documented.
        If size_multiplier == 0.0, final_size will be 0.0 (no trade).

    Example:
        result = calculate_position_size(
            market="ES", trade_type="TREND",
            atr=35.0, account_equity=150000.0,
            size_multiplier=1.0, config=cfg,
            entry_price=5000.0, is_long=True,
        )
        print(result)
        # ES TREND: 0.86 units | risk=$1500 | stop=87.5pts (2.5x ATR) | regime_mult=1.0x
    """
    if size_multiplier <= 0.0:
        # Regime says no trade
        return SizingResult(
            market=market, trade_type=trade_type,
            account_equity=account_equity, risk_fraction=0.0,
            risk_dollars=0.0, atr=atr, stop_multiplier=0.0,
            stop_distance=0.0, point_value=0.0, dollar_risk_per_unit=0.0,
            raw_size=0.0, size=0.0, size_multiplier=0.0, final_size=0.0,
        )

    # ── Parameters from config ─────────────────────────────────────────────────
    risk_pct = float(config.get("risk_per_trade_pct", 1.0)) / 100.0

    if trade_type.upper() == "TREND":
        stop_mult = float(config.get("stop_multiplier_trend", 2.5))
    else:
        stop_mult = float(config.get("stop_multiplier_mr", 1.5))

    # ── Point value ────────────────────────────────────────────────────────────
    if use_etf_sizing:
        # Backtesting: ETF proxies use $1/unit for fractional sizing
        point_value = ETF_POINT_VALUE
    else:
        # Live trading: use actual contract point values
        market_config = config.get("markets", {}).get(market, {})
        point_value   = float(market_config.get("point_value",
                              DEFAULT_POINT_VALUES.get(market, 1.0)))

    # ── Core calculation ───────────────────────────────────────────────────────
    risk_dollars         = account_equity * risk_pct
    stop_distance        = stop_mult * atr
    dollar_risk_per_unit = stop_distance * point_value

    if dollar_risk_per_unit <= 0 or pd.isna(dollar_risk_per_unit):
        log.warning(
            "{market}: Cannot size position — invalid ATR ({atr}) or point_value ({pv})",
            market=market, atr=atr, pv=point_value,
        )
        raw_size = 0.0
    else:
        raw_size = risk_dollars / dollar_risk_per_unit

    # ── Rounding ───────────────────────────────────────────────────────────────
    if use_etf_sizing:
        # ETF backtesting: fractional size OK (2 decimal places)
        size = round(raw_size, 2)
    else:
        # Live trading: whole contracts only, round DOWN for safety
        size = max(1.0, float(int(raw_size)))  # Minimum 1 contract

    # ── Apply regime multiplier ────────────────────────────────────────────────
    final_size = round(size * size_multiplier, 2 if use_etf_sizing else 0)

    # ── Stop price (for order placement) ──────────────────────────────────────
    stop_price = None
    if entry_price is not None and stop_distance > 0:
        if is_long:
            stop_price = round(entry_price - stop_distance, 4)
        else:
            stop_price = round(entry_price + stop_distance, 4)

    result = SizingResult(
        market=market,
        trade_type=trade_type,
        account_equity=account_equity,
        risk_fraction=risk_pct,
        risk_dollars=risk_dollars,
        atr=atr,
        stop_multiplier=stop_mult,
        stop_distance=stop_distance,
        point_value=point_value,
        dollar_risk_per_unit=dollar_risk_per_unit,
        raw_size=raw_size,
        size=size,
        size_multiplier=size_multiplier,
        final_size=final_size,
        stop_price=stop_price,
    )

    log.debug(
        "{market} {type}: equity=${eq:.0f} risk=${risk:.0f} "
        "ATR={atr:.2f} stop={sd:.2f}pts size={sz:.2f} final={fs:.2f}",
        market=market, type=trade_type,
        eq=account_equity, risk=risk_dollars,
        atr=atr, sd=stop_distance,
        sz=size, fs=final_size,
    )

    return result


# ── Vectorized sizing for backtesting ─────────────────────────────────────────

def add_position_sizes(
    df: pd.DataFrame,
    market: str,
    config: dict,
    account_equity: float = 150000.0,
    use_etf_sizing: bool  = True,
) -> pd.DataFrame:
    """
    Add position size columns to the DataFrame for all bars.

    This is the backtesting version — calculates sizing for every bar
    so the backtest engine can reference it at entry time.

    Args:
        df:             DataFrame with combined_signal, atr, size_multiplier columns
        market:         Market code
        config:         Strategy config dict
        account_equity: Simulated account equity (default $150,000 = Topstep)
        use_etf_sizing: True for fractional ETF backtesting (default)

    Returns:
        DataFrame with added columns:
          pos_size_trend  - float: trend trade size (AGREE_LONG/SHORT)
          pos_size_vmr    - float: VMR trade size
          stop_dist_trend - float: stop distance in points for trend trades
          stop_dist_vmr   - float: stop distance in points for VMR trades

    Example:
        df = combine_signals(df, "ES")
        df = add_position_sizes(df, "ES", cfg, account_equity=150000)
        entries = df[df["combined_new_entry"]]
        print(entries[["Close", "combined_signal", "pos_size_trend", "atr"]].head(10))
    """
    df = df.copy()

    risk_pct  = float(config.get("risk_per_trade_pct", 1.0)) / 100.0
    stop_tr   = float(config.get("stop_multiplier_trend", 2.5))
    stop_vmr  = float(config.get("stop_multiplier_mr", 1.5))
    pv        = ETF_POINT_VALUE if use_etf_sizing else float(
        config.get("markets", {}).get(market, {}).get("point_value",
        DEFAULT_POINT_VALUES.get(market, 1.0))
    )

    risk_dollars = account_equity * risk_pct

    # Stop distances in price points
    df["stop_dist_trend"] = df["atr"] * stop_tr
    df["stop_dist_vmr"]   = df["atr"] * stop_vmr

    # Dollar risk per unit for each trade type
    dollar_risk_trend = df["stop_dist_trend"] * pv
    dollar_risk_vmr   = df["stop_dist_vmr"]   * pv

    # Raw sizes (before rounding and regime multiplier)
    raw_trend = risk_dollars / dollar_risk_trend.replace(0, np.nan)
    raw_vmr   = risk_dollars / dollar_risk_vmr.replace(0, np.nan)

    # Apply regime multiplier
    mult = df.get("size_multiplier", pd.Series(1.0, index=df.index))

    if use_etf_sizing:
        df["pos_size_trend"] = (raw_trend * mult).round(2).fillna(0.0)
        df["pos_size_vmr"]   = (raw_vmr   * mult).round(2).fillna(0.0)
    else:
        df["pos_size_trend"] = (raw_trend * mult).fillna(0).apply(
            lambda x: max(1.0, float(int(x))) if x > 0 else 0.0
        )
        df["pos_size_vmr"] = (raw_vmr * mult).fillna(0).apply(
            lambda x: max(1.0, float(int(x))) if x > 0 else 0.0
        )

    log.info(
        "{market}: Position sizes added. Trend avg={tr_avg:.3f}, VMR avg={vmr_avg:.3f} units "
        "(equity=${eq:.0f}, risk={r:.1%})",
        market=market,
        tr_avg=df["pos_size_trend"].replace(0, np.nan).mean(),
        vmr_avg=df["pos_size_vmr"].replace(0, np.nan).mean(),
        eq=account_equity,
        r=risk_pct,
    )

    return df
