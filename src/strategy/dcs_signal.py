"""
AlgoBot — Donchian Channel System Signal (DCS)
================================================
Module:  src/strategy/dcs_signal.py
Phase:   2 — Strategy Signals
Purpose: Generates breakout signals from the 55/20 Donchian Channel System.

Strategy Logic:
  LONG:  Close breaks above the 55-bar Donchian high (new 55-day high)
         Exit when Close falls below the 20-bar Donchian low
  SHORT: Close breaks below the 55-bar Donchian low (new 55-day low)
         Exit when Close rises above the 20-bar Donchian high

  This is Turtle Trading System 2 (the longer of the two Turtle systems).
  The original Turtle Traders used this exact system in the 1980s and
  generated extraordinary returns across commodities. It works because
  price making a new N-day high is evidence that buyers are dominant
  and the trend is strong enough to sustain a position.

Why 55/20 specifically:
  55-day entry: ~2.5 months of price history. A new 55-day high means
  price has not been this high in 2.5 months — that is a meaningful
  breakout, not noise. This was System 2 in the original Turtle rules.

  20-day exit: ~1 month. When price breaks the 20-day low, momentum
  is reversing. The trend that triggered the 55-day entry is weakening.

The relationship to TMA:
  TMA and DCS are complementary but independent systems. TMA measures
  trend quality (EMA alignment). DCS measures price magnitude (breakout).
  Both systems can be long simultaneously during a strong trend. When
  they AGREE, the Signal Agreement Filter fires — this is the primary
  edge that targets our 2.5+ Profit Factor.

Signal values:
  +1 = Long signal (new 55-bar high — breakout up)
   0 = Neutral
  -1 = Short signal (new 55-bar low — breakout down)
"""

import pandas as pd
import numpy as np

from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Core signal logic ─────────────────────────────────────────────────────────

def dcs_signal(
    df: pd.DataFrame,
    market: str = "UNKNOWN",
) -> pd.DataFrame:
    """
    Calculate the Donchian Channel System signal for every bar.

    Requires calculate_indicators() to have been called first
    (needs donchian_high, donchian_low, donchian_exit_high, donchian_exit_low).

    Args:
        df:     DataFrame with Donchian columns from calculate_indicators()
        market: Market code for logging

    Returns:
        DataFrame with new columns added:
          dcs_signal     - int signal: +1 (long), 0 (neutral), -1 (short)
          dcs_long       - bool: currently in 55-bar long territory
          dcs_short      - bool: currently in 55-bar short territory
          dcs_new_long   - bool: new breakout long (first bar above 55-high)
          dcs_new_short  - bool: new breakout short (first bar below 55-low)
          dcs_exit_long  - bool: price fell below 20-bar low (exit long signal)
          dcs_exit_short - bool: price rose above 20-bar high (exit short signal)

    Example:
        df = calculate_indicators(clean_df, cfg, "GC")
        df = dcs_signal(df, "GC")
        breakouts = df[df["dcs_new_long"] | df["dcs_new_short"]]
        print(f"Total DCS breakout entries: {len(breakouts)}")
    """
    required_cols = ["donchian_high", "donchian_low",
                     "donchian_exit_high", "donchian_exit_low", "Close"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        log.error("{market}: DCS signal requires columns: {cols}", market=market, cols=missing)
        df["dcs_signal"]     = 0
        df["dcs_long"]       = False
        df["dcs_short"]      = False
        df["dcs_new_long"]   = False
        df["dcs_new_short"]  = False
        df["dcs_exit_long"]  = False
        df["dcs_exit_short"] = False
        return df

    df = df.copy()

    # ── Entry signals: price breaking 55-bar channel ───────────────────────────
    # IMPORTANT: Compare close to PREVIOUS bar's channel level (shift by 1)
    # This prevents lookahead bias — we can only act on yesterday's channel
    prev_donchian_high = df["donchian_high"].shift(1)
    prev_donchian_low  = df["donchian_low"].shift(1)

    # Long: close exceeds the previous bar's 55-day high
    dcs_long_signal  = df["Close"] > prev_donchian_high
    # Short: close falls below the previous bar's 55-day low
    dcs_short_signal = df["Close"] < prev_donchian_low

    # Ensure they can't both be true simultaneously (edge case: wide-ranging bar)
    # In the rare case where both are triggered, neither fires (NaN bar)
    both_triggered = dcs_long_signal & dcs_short_signal
    dcs_long_signal  = dcs_long_signal  & ~both_triggered
    dcs_short_signal = dcs_short_signal & ~both_triggered

    df["dcs_long"]  = dcs_long_signal
    df["dcs_short"] = dcs_short_signal

    # Signal: +1 long, -1 short, 0 neutral
    df["dcs_signal"] = 0
    df.loc[dcs_long_signal,  "dcs_signal"] = 1
    df.loc[dcs_short_signal, "dcs_signal"] = -1

    # ── New breakout entries (transition from non-long to long) ────────────────
    prev_signal = df["dcs_signal"].shift(1).fillna(0)
    df["dcs_new_long"]  = (df["dcs_signal"] == 1)  & (prev_signal != 1)
    df["dcs_new_short"] = (df["dcs_signal"] == -1) & (prev_signal != -1)

    # ── Exit signals: price breaking 20-bar exit channel ──────────────────────
    # Exit long when close falls below the 20-bar exit low
    # Exit short when close rises above the 20-bar exit high
    prev_exit_high = df["donchian_exit_high"].shift(1)
    prev_exit_low  = df["donchian_exit_low"].shift(1)

    df["dcs_exit_long"]  = df["Close"] < prev_exit_low
    df["dcs_exit_short"] = df["Close"] > prev_exit_high

    # ── Logging ────────────────────────────────────────────────────────────────
    valid_bars   = df["donchian_high"].notna().sum()
    long_signals  = int(df["dcs_long"].sum())
    short_signals = int(df["dcs_short"].sum())
    new_longs     = int(df["dcs_new_long"].sum())
    new_shorts    = int(df["dcs_new_short"].sum())

    log.info(
        "{market}: DCS signals over {n} valid bars: "
        "long_bars={l} ({lpct:.0%}), short_bars={s} ({spct:.0%}), "
        "new_breakouts: long={nl}, short={ns}",
        market=market,
        n=valid_bars,
        l=long_signals,  lpct=long_signals  / max(valid_bars, 1),
        s=short_signals, spct=short_signals / max(valid_bars, 1),
        nl=new_longs,
        ns=new_shorts,
    )

    return df
