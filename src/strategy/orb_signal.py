"""
AlgoBot — Opening Range Breakout (ORB) Intraday Strategy
==========================================================
Module:  src/strategy/orb_signal.py
Phase:   5 — MTF Architecture
Purpose: Generate intraday entry signals on 5-minute bars using the
         Opening Range Breakout methodology, filtered by HTF bias.

Strategy Logic:
  The Opening Range Breakout is one of the most validated intraday
  strategies in existence. The "opening range" is the price range
  established in the first N minutes after market open.

  Academic basis:
    - Crabel (1990): "Day Trading With Short Term Price Patterns"
    - Toby Crabel's ORB is the founding work. Validated extensively since.
    - Studies by Larry Williams, Connors, and independent researchers
      confirm ORB edge in liquid futures markets (ES, NQ especially).

  Why it works:
    1. Institutional order flow concentrates at the open (index rebalancing,
       program trades, overnight news reactions). This creates the "auction"
       zone — the price range the market establishes through discovery.
    2. A clean breakout above/below this range signals that the institutional
       consensus has resolved: buyers won (long breakout) or sellers won
       (short breakout).
    3. The opening range becomes a key reference point for the day. Many
       professional traders and algorithms use it as their reference.
    4. The first breakout of the ORB tends to run because:
       - Stops cluster above/below the range (creating fuel on breakout)
       - Institutions fade failed ORBs aggressively, but follow confirmed ones

  Our implementation:
    - Opening range: 9:30 AM to 10:00 AM ET (first 6 bars of 5-min chart)
    - Entry: 1 tick above range high (long) or 1 tick below range low (short)
    - Stop: Opposite side of the opening range
    - Target: 2R (2x the initial risk = range width x 2)
    - Partial: Exit 50% at 1R, trail the remainder
    - Max hold: 24 bars (2 hours) — no midday chop exposure
    - HTF filter: Only trade in direction of weekly/monthly bias
    - One entry per market per day (the ORB is a once-per-session setup)

Signal columns added to the intraday DataFrame:
    orb_range_high      - High of the opening range (set at 10:00 AM)
    orb_range_low       - Low of the opening range (set at 10:00 AM)
    orb_range_complete  - True from 10:00 AM bar onward
    orb_long_signal     - True on the bar where close > range_high
    orb_short_signal    - True on the bar where close < range_low
    orb_htf_blocked     - True if HTF bias blocked the signal direction

Usage:
    from src.strategy.orb_signal import compute_orb_signals

    # df_5min = 5-minute OHLCV DataFrame with DatetimeIndex (ET timestamps)
    # htf_bias = "BULL" | "BEAR" | "NEUTRAL" for today
    df_5min = compute_orb_signals(df_5min, market="ES", htf_bias="BULL", config=config)

    long_entries = df_5min[df_5min["orb_long_signal"] & ~df_5min["orb_htf_blocked"]]
"""

from __future__ import annotations

from typing import Optional

import pandas as pd
import numpy as np

from src.utils.logger import get_logger

log = get_logger(__name__)

# ── HTF bias constants ─────────────────────────────────────────────────────────
_BULL    = "BULL"
_BEAR    = "BEAR"
_NEUTRAL = "NEUTRAL"


# ── Opening Range computation ──────────────────────────────────────────────────

def _compute_opening_range(
    day_df: pd.DataFrame,
    range_start: str,
    range_end: str,
) -> tuple[float | None, float | None]:
    """
    Compute the opening range high and low for a single day's 5-min data.

    Args:
        day_df:      5-min DataFrame for a single day (DatetimeIndex in ET).
        range_start: Start time string "HH:MM" (inclusive, ET).
        range_end:   End time string "HH:MM" (exclusive: range is [start, end)).

    Returns:
        (range_high, range_low) as floats, or (None, None) if insufficient data.
    """
    try:
        range_bars = day_df.between_time(range_start, range_end, inclusive="left")
        if len(range_bars) < 2:  # Need at least 2 bars to form a valid range
            return None, None
        return float(range_bars["High"].max()), float(range_bars["Low"].min())
    except Exception:
        return None, None


# ── Per-day ORB signals ────────────────────────────────────────────────────────

def _compute_day_orb(
    day_df: pd.DataFrame,
    range_high: float,
    range_low: float,
    config: dict,
    htf_bias: str,
    market: str,
) -> pd.DataFrame:
    """
    Compute ORB entry signals for a single day's 5-min bars.

    Called per trading day after the opening range is established.
    Only bars after the range_end_time are checked for breakout.

    Args:
        day_df:     5-min DataFrame for one trading day (with orb columns pre-added).
        range_high: Opening range high price.
        range_low:  Opening range low price.
        config:     Full config dict.
        htf_bias:   "BULL" | "BEAR" | "NEUTRAL" for this market today.
        market:     Market code for logging.

    Returns:
        day_df with orb signal columns populated.
    """
    orb_cfg = config.get("intraday", {}).get("orb", {})
    range_end_time   = orb_cfg.get("range_end_time",  "10:00")
    no_entry_after   = orb_cfg.get("no_entry_after_time", "11:30")
    entry_buf_ticks  = int(orb_cfg.get("entry_buffer_ticks", 1))

    # Get tick size for this market (buffer = N ticks above/below range)
    markets_cfg = config.get("markets", {})
    tick_size   = float(markets_cfg.get(market, {}).get("tick_size", 0.25))
    entry_buffer = entry_buf_ticks * tick_size

    # Set range values for all rows in this day
    day_df["orb_range_high"] = range_high
    day_df["orb_range_low"]  = range_low

    # Mark range as complete after range_end_time
    day_df["orb_range_complete"] = False
    try:
        post_range = day_df.between_time(range_end_time, "23:59")
        day_df.loc[post_range.index, "orb_range_complete"] = True
    except Exception:
        pass

    # ── Only check for breakout after range is complete ────────────────────────
    post_range_df = day_df[day_df["orb_range_complete"]].copy()

    if post_range_df.empty:
        return day_df

    # Filter out bars after no-entry-after cutoff
    try:
        tradeable = post_range_df.between_time(range_end_time, no_entry_after)
    except Exception:
        tradeable = post_range_df

    # Entry levels
    long_entry_level  = range_high + entry_buffer
    short_entry_level = range_low  - entry_buffer

    # ── HTF bias gate ──────────────────────────────────────────────────────────
    long_allowed  = (htf_bias != _BEAR)    # BULL or NEUTRAL -> allow long
    short_allowed = (htf_bias != _BULL)    # BEAR or NEUTRAL -> allow short

    # ── First breakout detection ───────────────────────────────────────────────
    long_fired  = False
    short_fired = False

    for idx in tradeable.index:
        bar = tradeable.loc[idx]

        # Long breakout: close clears the entry level (Close > range_high + buffer)
        if not long_fired and bar["Close"] > long_entry_level:
            if long_allowed:
                day_df.loc[idx, "orb_long_signal"]  = True
                day_df.loc[idx, "orb_htf_blocked"]  = False
            else:
                day_df.loc[idx, "orb_long_signal"]  = False
                day_df.loc[idx, "orb_htf_blocked"]  = True
            long_fired = True

        # Short breakout: close clears below entry level
        if not short_fired and bar["Close"] < short_entry_level:
            if short_allowed:
                day_df.loc[idx, "orb_short_signal"] = True
                day_df.loc[idx, "orb_htf_blocked"]  = False
            else:
                day_df.loc[idx, "orb_short_signal"] = False
                day_df.loc[idx, "orb_htf_blocked"]  = True
            short_fired = True

        if long_fired and short_fired:
            break  # Found both potential entries; first one wins per direction

    return day_df


# ── Main public function ───────────────────────────────────────────────────────

def compute_orb_signals(
    df_5min: pd.DataFrame,
    market: str,
    config: dict,
    htf_bias_series: Optional[pd.Series] = None,
    default_htf_bias: str = _NEUTRAL,
) -> pd.DataFrame:
    """
    Compute Opening Range Breakout signals on a 5-minute intraday DataFrame.

    Processes each trading day independently:
      1. Identify opening range (9:30-10:00 AM ET)
      2. Compute range_high and range_low
      3. After 10:00 AM, detect first breakout above/below range
      4. Apply HTF bias gate to filter signal direction

    Args:
        df_5min:         5-minute OHLCV DataFrame with DatetimeIndex (ET time).
                         Must have columns: Open, High, Low, Close, Volume.
        market:          Market code ("ES" or "NQ"). Only these two markets
                         are supported for intraday ORB.
        config:          Full config dict (contains intraday.orb parameters).
        htf_bias_series: Optional Series indexed by date with BULL/BEAR/NEUTRAL
                         values for each trading day. If None, uses default_htf_bias
                         for all days.
        default_htf_bias: Bias to use when htf_bias_series is None or date missing.

    Returns:
        df_5min with new columns:
          orb_range_high      - Opening range high (NaN before range complete)
          orb_range_low       - Opening range low
          orb_range_complete  - True from 10:00 AM onward
          orb_long_signal     - True on breakout long entry bar
          orb_short_signal    - True on breakout short entry bar
          orb_htf_blocked     - True when HTF bias blocked the signal

    Raises:
        ValueError: If market is not in supported intraday markets (ES, NQ).

    Example:
        df_5min = load_intraday("ES", resolution_minutes=5,
                                start="2023-01-01", end="2023-12-31")
        df_5min = compute_orb_signals(
            df_5min, market="ES", config=config,
            htf_bias_series=es_daily_df["htf_combined_bias"]
        )
        longs  = df_5min[df_5min["orb_long_signal"]]
        shorts = df_5min[df_5min["orb_short_signal"]]
        print(f"ORB signals: {len(longs)} longs, {len(shorts)} shorts")
    """
    # Validate market
    intraday_markets = config.get("intraday", {}).get("markets", ["ES", "NQ"])
    if market not in intraday_markets:
        raise ValueError(
            f"Market '{market}' not configured for intraday trading. "
            f"Intraday markets: {intraday_markets}"
        )

    if df_5min.empty:
        log.warning("{market}: Empty 5-min DataFrame passed to compute_orb_signals", market=market)
        return df_5min

    # ── Read config ────────────────────────────────────────────────────────────
    orb_cfg         = config.get("intraday", {}).get("orb", {})
    range_start     = orb_cfg.get("range_start_time", "09:30")
    range_end       = orb_cfg.get("range_end_time",   "10:00")

    # ── Initialize signal columns ──────────────────────────────────────────────
    df = df_5min.copy()
    df["orb_range_high"]     = float("nan")
    df["orb_range_low"]      = float("nan")
    df["orb_range_complete"] = False
    df["orb_long_signal"]    = False
    df["orb_short_signal"]   = False
    df["orb_htf_blocked"]    = False

    # ── Group by trading day and process each day independently ───────────────
    trading_dates = df.index.normalize().unique()
    total_long    = 0
    total_short   = 0
    total_blocked = 0

    for day in trading_dates:
        day_mask = df.index.normalize() == day
        day_df   = df[day_mask].copy()

        if len(day_df) < 3:
            continue  # Not enough bars for a meaningful day

        # ── Get HTF bias for this day ──────────────────────────────────────────
        if htf_bias_series is not None:
            # Find the most recent bias value on or before this trading day.
            # Convert everything to tz-naive date strings to avoid timezone/type issues.
            day_str = str(pd.Timestamp(day).date())
            try:
                # Build a string-indexed version of the bias series for safe lookup
                bias_str_idx = htf_bias_series.copy()
                bias_str_idx.index = [str(pd.Timestamp(d).date())
                                      for d in htf_bias_series.index]
                prior = bias_str_idx[bias_str_idx.index <= day_str]
                htf_bias = str(prior.iloc[-1]) if len(prior) > 0 else default_htf_bias
            except Exception:
                htf_bias = default_htf_bias
        else:
            htf_bias = default_htf_bias

        # ── Compute opening range for this day ────────────────────────────────
        range_high, range_low = _compute_opening_range(day_df, range_start, range_end)

        if range_high is None or range_low is None:
            log.debug("{market}: No opening range on {day} (insufficient data)",
                      market=market, day=day.date())
            continue

        if range_high <= range_low:
            log.debug("{market}: Invalid range on {day} (high <= low)", market=market, day=day.date())
            continue

        # ── Compute ORB signals for this day ──────────────────────────────────
        day_df = _compute_day_orb(day_df, range_high, range_low, config, htf_bias, market)

        # Write day results back to main DataFrame
        df.update(day_df)

        day_longs  = int(day_df["orb_long_signal"].sum())
        day_shorts = int(day_df["orb_short_signal"].sum())
        day_blocks = int(day_df["orb_htf_blocked"].sum())

        total_long    += day_longs
        total_short   += day_shorts
        total_blocked += day_blocks

    log.info(
        "{market}: ORB signals computed | "
        "Long={tl} Short={ts} HTF_blocked={tb} | "
        "Days processed={nd}",
        market=market,
        tl=total_long, ts=total_short, tb=total_blocked,
        nd=len(trading_dates),
    )

    return df


# ── ORB backtest metrics helper ────────────────────────────────────────────────

def summarize_orb_signals(df: pd.DataFrame, market: str) -> dict:
    """
    Summarize ORB signal statistics for reporting.

    Args:
        df:     DataFrame output of compute_orb_signals.
        market: Market code for display.

    Returns:
        dict with signal counts, HTF block rate, and days analyzed.
    """
    if df.empty:
        return {"market": market, "error": "empty DataFrame"}

    total_days = df.index.normalize().nunique()
    long_sigs  = int(df["orb_long_signal"].sum())  if "orb_long_signal"  in df.columns else 0
    short_sigs = int(df["orb_short_signal"].sum()) if "orb_short_signal" in df.columns else 0
    blocked    = int(df["orb_htf_blocked"].sum())  if "orb_htf_blocked"  in df.columns else 0

    signals_per_day = (long_sigs + short_sigs) / max(total_days, 1)
    block_rate      = blocked / max(long_sigs + short_sigs + blocked, 1)

    return {
        "market":           market,
        "trading_days":     total_days,
        "orb_long_signals": long_sigs,
        "orb_short_signals": short_sigs,
        "htf_blocked":      blocked,
        "signals_per_day":  round(signals_per_day, 2),
        "htf_block_rate_pct": round(block_rate * 100, 1),
    }
