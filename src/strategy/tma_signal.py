"""
AlgoBot — Triple Moving Average Signal (TMA)
==============================================
Module:  src/strategy/tma_signal.py
Phase:   2 — Strategy Signals
Purpose: Generates directional trend signals from the 8/21/89 EMA stack.

Strategy Logic:
  The TMA signal fires when all three EMAs are perfectly stacked:
    LONG:  ema_fast > ema_medium > ema_slow  AND  close > ema_fast
    SHORT: ema_fast < ema_medium < ema_slow  AND  close < ema_fast

  The EMA periods are Fibonacci numbers (8, 21, 89). This is not arbitrary —
  Fibonacci-based EMAs naturally align with how institutional traders think
  about medium and long-term trend structure. They are used by professional
  trend-following funds globally.

  The EMA stack requirement (all three aligned) prevents whipsawing. A single
  EMA crossover generates too many false signals. Requiring all three to agree
  means the trend must be established at three time scales simultaneously.

  Additionally requiring close > ema_fast (for longs) ensures price is
  ABOVE the fastest EMA — we are entering INTO momentum, not chasing.

Why 8/21/89 specifically:
  8 EMA:  Captures the current 2-week trend (8 trading days)
  21 EMA: Captures the monthly trend (21 trading days ~ 1 month)
  89 EMA: Captures the quarterly trend (89 trading days ~ 4 months)

  When all three agree, the trend is confirmed across 3 different time horizons.
  This multi-timeframe confirmation is the core of the Signal Agreement Filter.

Signal values:
  +1 = Long signal (uptrend aligned, buy)
   0 = Neutral (no signal, EMAs not fully stacked or conflicting)
  -1 = Short signal (downtrend aligned, sell)
"""

import pandas as pd
import numpy as np

from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Core signal logic ─────────────────────────────────────────────────────────

def tma_signal(
    df: pd.DataFrame,
    market: str = "UNKNOWN",
) -> pd.DataFrame:
    """
    Calculate the Triple Moving Average signal for every bar.

    Requires that calculate_indicators() has been called first
    (needs ema_fast, ema_medium, ema_slow columns).

    Args:
        df:     DataFrame with EMA columns from calculate_indicators()
        market: Market code for logging

    Returns:
        DataFrame with new columns added:
          tma_signal  - int signal: +1 (long), 0 (neutral), -1 (short)
          tma_long    - bool: True when long conditions met
          tma_short   - bool: True when short conditions met
          tma_aligned - bool: True when EMAs are stacked (any direction)

    Example:
        df = calculate_indicators(clean_df, cfg, "ES")
        df = tma_signal(df, "ES")
        long_bars = df[df["tma_signal"] == 1]
        print(f"TMA long signals: {len(long_bars)}")
    """
    required_cols = ["ema_fast", "ema_medium", "ema_slow", "Close"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        log.error("{market}: TMA signal requires columns: {cols}", market=market, cols=missing)
        df["tma_signal"]  = 0
        df["tma_long"]    = False
        df["tma_short"]   = False
        df["tma_aligned"] = False
        return df

    df = df.copy()

    # ── Long condition: fast > medium > slow AND close > fast ──────────────────
    # The close > fast EMA condition ensures we're entering INTO momentum
    ema_stack_long = (
        (df["ema_fast"]   > df["ema_medium"]) &
        (df["ema_medium"] > df["ema_slow"])   &
        (df["Close"]      > df["ema_fast"])
    )

    # ── Short condition: fast < medium < slow AND close < fast ─────────────────
    ema_stack_short = (
        (df["ema_fast"]   < df["ema_medium"]) &
        (df["ema_medium"] < df["ema_slow"])   &
        (df["Close"]      < df["ema_fast"])
    )

    df["tma_long"]    = ema_stack_long
    df["tma_short"]   = ema_stack_short
    df["tma_aligned"] = ema_stack_long | ema_stack_short

    # Signal: +1 for long, -1 for short, 0 for neutral
    df["tma_signal"] = 0
    df.loc[ema_stack_long,  "tma_signal"] = 1
    df.loc[ema_stack_short, "tma_signal"] = -1

    # ── Detect signal transitions (new signals only, not continuation) ─────────
    # A "new" long signal is when tma_signal goes from 0/-1 to +1
    # These are the actual entry signals (not every bar in a trend)
    prev_signal = df["tma_signal"].shift(1).fillna(0)
    df["tma_new_long"]  = (df["tma_signal"] == 1)  & (prev_signal != 1)
    df["tma_new_short"] = (df["tma_signal"] == -1) & (prev_signal != -1)

    # ── Logging ────────────────────────────────────────────────────────────────
    valid_bars   = df["ema_slow"].notna().sum()
    long_signals  = int(df["tma_long"].sum())
    short_signals = int(df["tma_short"].sum())
    new_longs     = int(df["tma_new_long"].sum())
    new_shorts    = int(df["tma_new_short"].sum())

    log.info(
        "{market}: TMA signals over {n} valid bars: "
        "long_bars={l} ({lpct:.0%}), short_bars={s} ({spct:.0%}), "
        "new_entries: long={nl}, short={ns}",
        market=market,
        n=valid_bars,
        l=long_signals,  lpct=long_signals  / max(valid_bars, 1),
        s=short_signals, spct=short_signals / max(valid_bars, 1),
        nl=new_longs,
        ns=new_shorts,
    )

    return df


# ── Exit signal ───────────────────────────────────────────────────────────────

def tma_exit_signal(df: pd.DataFrame) -> pd.DataFrame:
    """
    Determine when to exit a TMA-initiated position.

    Exit rules:
      Exit long:  When ema_fast crosses below ema_medium (trend weakening)
                  OR when tma_signal flips to -1 (trend reversed)
      Exit short: When ema_fast crosses above ema_medium
                  OR when tma_signal flips to +1

    This gives TMA trades more room than the DCS 20-bar exit.
    The EMA crossback allows the trend to have minor pullbacks.

    Args:
        df: DataFrame with tma_signal column

    Returns:
        DataFrame with tma_exit_long and tma_exit_short columns added.
    """
    df = df.copy()

    if "tma_signal" not in df.columns:
        df["tma_exit_long"]  = False
        df["tma_exit_short"] = False
        return df

    prev_signal = df["tma_signal"].shift(1).fillna(0)

    # Exit long: signal was +1, now is 0 or -1
    df["tma_exit_long"]  = (prev_signal == 1)  & (df["tma_signal"] != 1)
    # Exit short: signal was -1, now is 0 or +1
    df["tma_exit_short"] = (prev_signal == -1) & (df["tma_signal"] != -1)

    return df
