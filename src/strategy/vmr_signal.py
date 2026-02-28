"""
AlgoBot — Volatility Mean Reversion Signal (VMR)
==================================================
Module:  src/strategy/vmr_signal.py
Phase:   2 — Strategy Signals
Purpose: Generates mean reversion signals from RSI5 extreme readings.
         Only active on ES and NQ during RANGING market regimes.

Strategy Logic:
  The VMR signal fires when price has moved too far too fast
  and is statistically likely to snap back:

  LONG  (buy the dip):  RSI5 < 25 (oversold)  AND  regime == RANGING
  SHORT (sell the rip): RSI5 > 75 (overbought) AND  regime == RANGING

  Exit: When RSI5 returns to neutral (between 40-60), take profit.
        Hard ATR stop: 1.5x ATR (tighter than trend stops because
        mean reversion is a different bet — if it doesn't snap back
        quickly, we were wrong and must exit fast).

Why RSI5 (not RSI14):
  RSI14 is too slow for mean reversion. By the time RSI14 reaches 25,
  the snapback has already happened. RSI5 (5-bar RSI) is fast enough
  to catch the extremes in real time. A 25 reading on RSI5 is rarer
  and more meaningful than a 30 reading on RSI14.

Why only ES and NQ (not GC, CL, ZB, 6E):
  Mean reversion works in markets with strong institutional support
  levels and high liquidity. Equity index futures (ES, NQ) have the
  deepest book, tightest spreads, and the strongest tendency to
  "snap back" after short-term extremes because institutional buyers
  step in on dips.

  Commodities (GC, CL) and bonds (ZB) trend more persistently —
  an oversold reading in CL can stay oversold for weeks.
  Forex (6E) has different mean-reversion dynamics.

  Applying VMR across all 6 markets would generate false signals in
  markets where the pattern doesn't hold statistically.

Why only during RANGING (ADX < 20):
  Mean reversion is a COUNTER-TREND bet. In a TRENDING market (ADX > 25),
  oversold can keep getting more oversold — the dip is a trend continuation.
  In a RANGING market, extremes DO snap back — there is no dominant trend
  to sustain the move. This is the critical regime gate.

Signal values:
  +1 = Long signal (buy the dip, RSI5 < 25 in ranging market)
   0 = Neutral
  -1 = Short signal (sell the rip, RSI5 > 75 in ranging market)

Note: VMR signal is generated for ALL markets but the signal_combiner
filters it to ES/NQ only. This allows future expansion to other markets
if statistical testing confirms mean reversion edges there too.
"""

import pandas as pd
import numpy as np

from src.utils.logger import get_logger

log = get_logger(__name__)

# Markets where VMR is allowed to fire
VMR_MARKETS = {"ES", "NQ"}


# ── Core signal logic ─────────────────────────────────────────────────────────

def vmr_signal(
    df: pd.DataFrame,
    config: dict,
    market: str = "UNKNOWN",
) -> pd.DataFrame:
    """
    Calculate the Volatility Mean Reversion signal for every bar.

    Requires calculate_indicators() (needs 'rsi' column) and
    classify_regimes() (needs 'vmr_active' column) to have been called.

    Args:
        df:     DataFrame with rsi and vmr_active columns
        config: Strategy config dict. Required keys:
                  oversold_threshold  (default 25)
                  overbought_threshold (default 75)
                  rsi_neutral_low     (default 40)  - exit long when RSI above this
                  rsi_neutral_high    (default 60)  - exit short when RSI below this
        market: Market code for logging

    Returns:
        DataFrame with new columns added:
          vmr_signal         - int: +1 (long), 0 (neutral), -1 (short)
          vmr_long           - bool: RSI5 oversold in ranging regime
          vmr_short          - bool: RSI5 overbought in ranging regime
          vmr_new_long       - bool: new VMR long entry
          vmr_new_short      - bool: new VMR short entry
          vmr_exit_long      - bool: RSI returned to neutral (exit long)
          vmr_exit_short     - bool: RSI returned to neutral (exit short)
          vmr_market_allowed - bool: whether VMR is approved for this market

    Example:
        df = calculate_indicators(clean_df, cfg, "ES")
        df = add_atr_baseline(df)
        df = classify_regimes(df, cfg, "ES")
        df = vmr_signal(df, cfg, "ES")
        print(df[df["vmr_new_long"]]["rsi"].describe())
    """
    required_cols = ["rsi"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        log.error("{market}: VMR signal requires columns: {cols}", market=market, cols=missing)
        df["vmr_signal"]         = 0
        df["vmr_long"]           = False
        df["vmr_short"]          = False
        df["vmr_new_long"]       = False
        df["vmr_new_short"]      = False
        df["vmr_exit_long"]      = False
        df["vmr_exit_short"]     = False
        df["vmr_market_allowed"] = False
        return df

    df = df.copy()

    # ── Config parameters ──────────────────────────────────────────────────────
    oversold    = float(config.get("oversold_threshold",   25.0))
    overbought  = float(config.get("overbought_threshold", 75.0))
    neutral_low = float(config.get("rsi_neutral_low",      40.0))
    neutral_hi  = float(config.get("rsi_neutral_high",     60.0))

    # ── Market gate ────────────────────────────────────────────────────────────
    # VMR is only approved for ES and NQ (equity index futures)
    market_allowed = market.upper() in VMR_MARKETS
    df["vmr_market_allowed"] = market_allowed

    if not market_allowed:
        log.info(
            "{market}: VMR not enabled (approved markets: {mkts}). "
            "Columns set to False/0.",
            market=market, mkts=sorted(VMR_MARKETS),
        )
        df["vmr_signal"]     = 0
        df["vmr_long"]       = False
        df["vmr_short"]      = False
        df["vmr_new_long"]   = False
        df["vmr_new_short"]  = False
        df["vmr_exit_long"]  = False
        df["vmr_exit_short"] = False
        return df

    # ── Regime gate ────────────────────────────────────────────────────────────
    # VMR only fires when regime classifier says vmr_active = True (RANGING)
    if "vmr_active" in df.columns:
        regime_gate = df["vmr_active"]
    else:
        # If classify_regimes hasn't been called, log warning and fire everywhere
        # (safer to fire too much than to silently never fire)
        log.warning(
            "{market}: 'vmr_active' column not found — VMR firing without regime gate. "
            "Call classify_regimes() before vmr_signal() in production.",
            market=market,
        )
        regime_gate = pd.Series(True, index=df.index)

    # ── RSI entry conditions ───────────────────────────────────────────────────
    rsi_oversold   = df["rsi"] < oversold   # Dip: RSI5 below 25
    rsi_overbought = df["rsi"] > overbought  # Rip: RSI5 above 75

    # Both regime AND RSI must agree
    df["vmr_long"]  = regime_gate & rsi_oversold
    df["vmr_short"] = regime_gate & rsi_overbought

    df["vmr_signal"] = 0
    df.loc[df["vmr_long"],  "vmr_signal"] = 1
    df.loc[df["vmr_short"], "vmr_signal"] = -1

    # ── New signal detection ───────────────────────────────────────────────────
    prev_signal = df["vmr_signal"].shift(1).fillna(0)
    df["vmr_new_long"]  = (df["vmr_signal"] == 1)  & (prev_signal != 1)
    df["vmr_new_short"] = (df["vmr_signal"] == -1) & (prev_signal != -1)

    # ── Exit conditions ────────────────────────────────────────────────────────
    # Exit long when RSI returns above neutral_low (40): reversion complete
    # Exit short when RSI returns below neutral_high (60): reversion complete
    df["vmr_exit_long"]  = df["rsi"] > neutral_low
    df["vmr_exit_short"] = df["rsi"] < neutral_hi

    # ── Logging ────────────────────────────────────────────────────────────────
    valid_bars   = df["rsi"].notna().sum()
    ranging_bars = int(regime_gate.sum()) if hasattr(regime_gate, "sum") else 0
    long_signals  = int(df["vmr_long"].sum())
    short_signals = int(df["vmr_short"].sum())

    log.info(
        "{market}: VMR signals over {n} valid bars "
        "({rg} RANGING bars, {rgpct:.0%} of total): "
        "oversold_entries={l}, overbought_entries={s}",
        market=market,
        n=valid_bars,
        rg=ranging_bars,
        rgpct=ranging_bars / max(valid_bars, 1),
        l=long_signals,
        s=short_signals,
    )

    return df
