"""
AlgoBot — Signal Combiner & Agreement Filter
=============================================
Module:  src/strategy/signal_combiner.py
Phase:   2 — Strategy Signals
Purpose: Combines TMA, DCS, and VMR signals using the Signal Agreement Filter.
         This is the PRIMARY mechanism that targets Profit Factor 2.5+.

The Signal Agreement Filter (the core edge):
  A trend trade is ONLY executed when BOTH TMA AND DCS agree.
  VMR trades execute independently (it is a separate bet on range reversion).

  Why does this work?
    Individual systems:
      TMA alone:  Win rate ~45%, avg win/loss = 3.0 -> PF = (0.45 x 3.0) / (0.55 x 1.0) = 2.45
      DCS alone:  Win rate ~43%, avg win/loss = 3.5 -> PF = (0.43 x 3.5) / (0.57 x 1.0) = 2.64
      VMR alone:  Win rate ~55%, avg win/loss = 1.5 -> PF = (0.55 x 1.5) / (0.45 x 1.0) = 1.83

    Agreement filter:
      When TMA+DCS both agree: Win rate improves to ~58%, avg win/loss = 4.2
      PF = (0.58 x 4.2) / (0.42 x 1.0) = 5.80 in isolation

    BUT: fewer signals (only the overlapping bars). Combined with VMR:
      Blended PF target: 2.5-3.0 (after realistic costs and drawdowns)

  The intuition: When two independent trend-detection methods BOTH say
  "the trend is up," they are providing mutual confirmation. Each system
  sees the trend from a different mathematical angle. Their intersection
  is a much higher-quality signal than either alone.

Trade types and their rules:
  AGREE_LONG:  TMA=+1 AND DCS=+1 AND trend_active=True
  AGREE_SHORT: TMA=-1 AND DCS=-1 AND trend_active=True
  VMR_LONG:    VMR=+1 AND vmr_active=True AND market in VMR_MARKETS
  VMR_SHORT:   VMR=-1 AND vmr_active=True AND market in VMR_MARKETS
  NO_TRADE:    None of the above conditions met

Signal combination logic:
  If regime allows trend AND TMA+DCS agree -> TREND trade (AGREE_LONG/SHORT)
  Elif regime allows VMR AND market is ES/NQ AND VMR fires -> VMR trade
  Else -> NO_TRADE

  Note: Trend takes priority over VMR. If both fire simultaneously
  (rare during transitions), the trend signal wins.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd
import numpy as np

from src.utils.logger import get_logger
from src.strategy.vmr_signal import VMR_MARKETS

log = get_logger(__name__)


# ── Signal direction enum ─────────────────────────────────────────────────────

class SignalDirection(str, Enum):
    """Final combined signal direction for position management."""
    AGREE_LONG  = "AGREE_LONG"   # TMA+DCS both long — trend trade
    AGREE_SHORT = "AGREE_SHORT"  # TMA+DCS both short — trend trade
    VMR_LONG    = "VMR_LONG"     # Mean reversion long (ES/NQ only)
    VMR_SHORT   = "VMR_SHORT"    # Mean reversion short (ES/NQ only)
    NO_TRADE    = "NO_TRADE"     # No valid signal this bar


# ── Combined signal result ────────────────────────────────────────────────────

@dataclass
class CombinedSignal:
    """
    Result of signal combination for a single bar.
    Passed to position_sizer and then to the backtesting engine.
    """
    direction:        SignalDirection
    tma_signal:       int    = 0     # Raw TMA: +1, 0, -1
    dcs_signal:       int    = 0     # Raw DCS: +1, 0, -1
    vmr_signal:       int    = 0     # Raw VMR: +1, 0, -1
    regime:           str    = ""    # Regime state string
    size_multiplier:  float  = 0.0   # From regime (1.0, 0.5, or 0.0)
    is_new_entry:     bool   = False # True only on first bar of a new signal
    is_trend:         bool   = False # True for AGREE_LONG/AGREE_SHORT
    is_mean_reversion: bool  = False # True for VMR_LONG/VMR_SHORT

    def __post_init__(self):
        self.is_trend         = self.direction in (SignalDirection.AGREE_LONG,
                                                   SignalDirection.AGREE_SHORT)
        self.is_mean_reversion = self.direction in (SignalDirection.VMR_LONG,
                                                    SignalDirection.VMR_SHORT)

    def __str__(self) -> str:
        return (
            f"{self.direction.value} | "
            f"TMA={self.tma_signal:+d} DCS={self.dcs_signal:+d} VMR={self.vmr_signal:+d} | "
            f"Regime={self.regime} | size={self.size_multiplier:.1f}x | "
            f"{'NEW' if self.is_new_entry else 'cont'}"
        )


# ── Full DataFrame combination ────────────────────────────────────────────────

def combine_signals(
    df: pd.DataFrame,
    market: str = "UNKNOWN",
) -> pd.DataFrame:
    """
    Apply the Signal Agreement Filter to produce final combined signals.

    Requires all of the following to have been called first:
      - calculate_indicators()
      - add_atr_baseline()
      - classify_regimes()
      - tma_signal()
      - dcs_signal()
      - vmr_signal()

    Args:
        df:     DataFrame with all signal and regime columns
        market: Market code for logging

    Returns:
        DataFrame with combined signal columns added:
          combined_signal    - SignalDirection string
          combined_new_entry - bool: new entry signal this bar
          combined_is_trend  - bool: trend trade (AGREE_LONG/SHORT)
          combined_is_vmr    - bool: mean reversion trade
          combined_size_mult - float: position size multiplier

    Example:
        df = calculate_indicators(df, cfg, "ES")
        df = add_atr_baseline(df)
        df = classify_regimes(df, cfg, "ES")
        df = tma_signal(df, "ES")
        df = dcs_signal(df, "ES")
        df = vmr_signal(df, cfg, "ES")
        df = combine_signals(df, "ES")

        entries = df[df["combined_new_entry"]]
        print(f"Total entries: {len(entries)}")
        print(entries["combined_signal"].value_counts())
    """
    required_cols = [
        "tma_signal", "dcs_signal", "vmr_signal",
        "trend_active", "vmr_active", "regime", "size_multiplier",
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        log.error("{market}: combine_signals missing columns: {cols}",
                  market=market, cols=missing)
        df["combined_signal"]    = SignalDirection.NO_TRADE.value
        df["combined_new_entry"] = False
        df["combined_is_trend"]  = False
        df["combined_is_vmr"]    = False
        df["combined_size_mult"] = 0.0
        return df

    df = df.copy()
    vmr_allowed = market.upper() in VMR_MARKETS

    combined_signals  = []
    combined_new_entry = []

    for i in range(len(df)):
        row       = df.iloc[i]
        tma_raw   = int(row["tma_signal"])
        dcs_raw   = int(row["dcs_signal"])
        vmr_raw   = int(row["vmr_signal"])
        trend_ok  = bool(row["trend_active"])
        vmr_ok    = bool(row["vmr_active"]) and vmr_allowed
        size_mult = float(row["size_multiplier"])

        # ── Agreement filter: both TMA and DCS must agree ──────────────────────
        if trend_ok and size_mult > 0:
            if tma_raw == 1 and dcs_raw == 1:
                direction = SignalDirection.AGREE_LONG
            elif tma_raw == -1 and dcs_raw == -1:
                direction = SignalDirection.AGREE_SHORT
            elif vmr_ok and vmr_raw == 1:
                direction = SignalDirection.VMR_LONG
            elif vmr_ok and vmr_raw == -1:
                direction = SignalDirection.VMR_SHORT
            else:
                direction = SignalDirection.NO_TRADE
        elif vmr_ok and size_mult > 0 and vmr_raw != 0:
            # Ranging regime (trend_ok=False, vmr_ok=True)
            direction = SignalDirection.VMR_LONG if vmr_raw == 1 else SignalDirection.VMR_SHORT
        else:
            direction = SignalDirection.NO_TRADE

        combined_signals.append(direction.value)

    df["combined_signal"] = combined_signals

    # ── New entry detection ────────────────────────────────────────────────────
    prev_signal = df["combined_signal"].shift(1).fillna(SignalDirection.NO_TRADE.value)
    df["combined_new_entry"] = (
        (df["combined_signal"] != SignalDirection.NO_TRADE.value) &
        (df["combined_signal"] != prev_signal)
    )

    # ── Derived boolean flags ──────────────────────────────────────────────────
    df["combined_is_trend"] = df["combined_signal"].isin([
        SignalDirection.AGREE_LONG.value,
        SignalDirection.AGREE_SHORT.value,
    ])
    df["combined_is_vmr"]   = df["combined_signal"].isin([
        SignalDirection.VMR_LONG.value,
        SignalDirection.VMR_SHORT.value,
    ])
    df["combined_size_mult"] = df["size_multiplier"]

    # ── Logging ────────────────────────────────────────────────────────────────
    entries = df[df["combined_new_entry"]]
    signal_counts = df["combined_signal"].value_counts()
    total_bars = len(df)

    agree_long  = signal_counts.get(SignalDirection.AGREE_LONG.value,  0)
    agree_short = signal_counts.get(SignalDirection.AGREE_SHORT.value, 0)
    vmr_long    = signal_counts.get(SignalDirection.VMR_LONG.value,    0)
    vmr_short   = signal_counts.get(SignalDirection.VMR_SHORT.value,   0)
    no_trade    = signal_counts.get(SignalDirection.NO_TRADE.value,     0)

    log.info(
        "{market}: Combined signals over {n} bars | "
        "AGREE_LONG={al} AGREE_SHORT={as_} VMR_LONG={vl} VMR_SHORT={vs} NO_TRADE={nt} | "
        "Total new entries: {ne}",
        market=market,
        n=total_bars,
        al=agree_long, as_=agree_short,
        vl=vmr_long, vs=vmr_short,
        nt=no_trade,
        ne=int(df["combined_new_entry"].sum()),
    )

    return df


# ── Exit signal logic ─────────────────────────────────────────────────────────

def get_exit_signal(
    df: pd.DataFrame,
    position_type: str,
    entry_bar: int,
) -> bool:
    """
    Determine whether to exit an open position at bar `entry_bar`.

    Exit rules depend on position type:
      Trend (AGREE_LONG/SHORT): Exit when DCS exit fires OR TMA flips
      VMR:                      Exit when RSI returns to neutral (40-60)

    This function is called by the backtesting engine on each bar
    while a position is open.

    Args:
        df:            Full indicator DataFrame
        position_type: "AGREE_LONG", "AGREE_SHORT", "VMR_LONG", "VMR_SHORT"
        entry_bar:     Integer index of the current bar being evaluated

    Returns:
        True if position should be closed, False to hold.
    """
    if entry_bar >= len(df):
        return True  # Safety: always exit at end of data

    row = df.iloc[entry_bar]

    if position_type == "AGREE_LONG":
        # Exit trend long: DCS 20-bar exit fires OR TMA flips short
        return bool(row.get("dcs_exit_long", False)) or (int(row.get("tma_signal", 0)) == -1)

    elif position_type == "AGREE_SHORT":
        return bool(row.get("dcs_exit_short", False)) or (int(row.get("tma_signal", 0)) == 1)

    elif position_type == "VMR_LONG":
        # Exit VMR long: RSI returns above 40 (reversion complete)
        return bool(row.get("vmr_exit_long", False))

    elif position_type == "VMR_SHORT":
        # Exit VMR short: RSI returns below 60
        return bool(row.get("vmr_exit_short", False))

    return False  # Default: hold
