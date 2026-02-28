"""
AlgoBot — Continuous Contract Handler
=======================================
Module:  src/utils/continuous_contract.py
Phase:   1 — Data Infrastructure
Purpose: Detects and corrects futures contract rollover artifacts in price data.

The Rollover Problem:
  Futures contracts expire. When a contract expires, traders roll to the next
  contract (e.g., ES March 2024 → ES June 2024). The two contracts often trade
  at different prices due to cost-of-carry. This creates an artificial price
  gap in a "continuous" futures series.

  Example:
    ES March contract closes at 5100.00
    ES June contract opens at 5105.00 (new front month)
    Raw data shows a 5-point overnight gap — but no actual price move occurred.
    A trend signal calculated across this gap would see a false breakout.

The Panama Canal Adjustment (Backward Ratio Method):
  This is the standard method used by professional quant firms.
  At each rollover point, we apply a multiplicative ratio to all HISTORICAL
  prices so that the series is continuous without the artificial gap.

  Why ratio (not additive): Multiplicative adjustment preserves the percentage
  returns history. An additive adjustment can push old prices into negative
  territory for commodities that had large price level changes.

  Note: After adjustment, old prices are not the "real" prices — they are
  adjusted prices used purely for signal calculation. Position sizing always
  uses the CURRENT (unadjusted) price from config.yaml contract specs.

Current Project Context:
  In Phase 1-4 (backtesting with ETF proxies from Yahoo Finance), this module
  is mostly unused — ETFs don't have rollover issues.

  This module becomes CRITICAL in Phase 6-7 when using actual futures data
  from QuantConnect or Norgate, or when connecting to Rithmic for live data.
  It is built now so it's ready when needed.
"""

import numpy as np
import pandas as pd

from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Rollover detection ────────────────────────────────────────────────────────

def detect_rollover_gaps(
    df: pd.DataFrame,
    threshold_pct: float = 0.01,
    market: str = "UNKNOWN",
) -> list[pd.Timestamp]:
    """
    Detect dates where a futures rollover gap occurred.

    A rollover gap is identified as an overnight price jump that is:
      a) Larger than threshold_pct (e.g., 1% of price)
      b) Occurs as a gap between close[t] and open[t+1]
      c) Is NOT accompanied by a correspondingly large intraday range
         (genuine gaps are price level shifts, not volatile bars)

    Args:
        df:            OHLCV DataFrame with DatetimeIndex
        threshold_pct: Minimum gap size as fraction of price (default 1%)
                       1% is appropriate for equity index futures.
                       Increase to 2-3% for commodities (CL, GC).
        market:        Market name for logging

    Returns:
        List of Timestamps where rollovers were detected.
        An empty list means no rollovers found (clean data or ETF proxy).

    Example:
        rollover_dates = detect_rollover_gaps(df, threshold_pct=0.01, market="ES")
        print(f"Rollovers detected: {len(rollover_dates)}")
    """
    if "Close" not in df.columns or "Open" not in df.columns:
        log.warning("{market}: Cannot detect rollovers — missing Open or Close column",
                    market=market)
        return []

    if len(df) < 2:
        return []

    # Calculate overnight gap: (today's open - yesterday's close) / yesterday's close
    prev_close    = df["Close"].shift(1)
    overnight_gap = (df["Open"] - prev_close).abs() / prev_close

    # Calculate intraday range as fraction of price
    intraday_range = (df["High"] - df["Low"]) / df["Low"]

    # A rollover gap is large overnight but NOT a large intraday bar
    # (real volatile days have both a gap AND large intraday range)
    rollover_mask = (
        (overnight_gap > threshold_pct) &
        (overnight_gap > intraday_range * 0.5)
    )

    rollover_dates = df.index[rollover_mask].tolist()

    if rollover_dates:
        log.info(
            "{market}: Detected {n} potential rollover dates "
            "(gap > {t:.1%}): {dates}",
            market=market,
            n=len(rollover_dates),
            t=threshold_pct,
            dates=[d.strftime("%Y-%m-%d") for d in rollover_dates[:5]],
        )
    else:
        log.info("{market}: No rollover gaps detected (threshold: {t:.1%})",
                 market=market, t=threshold_pct)

    return rollover_dates


# ── Panama Canal adjustment ───────────────────────────────────────────────────

def apply_panama_adjustment(
    df: pd.DataFrame,
    rollover_dates: list[pd.Timestamp],
    market: str = "UNKNOWN",
) -> pd.DataFrame:
    """
    Apply the Panama Canal backward ratio adjustment at each rollover date.

    For each rollover date (working backward from the most recent):
      ratio = close[rollover_date - 1] / open[rollover_date]
      All OHLC prices BEFORE the rollover are multiplied by this ratio.

    This makes the series continuous by adjusting historical prices
    to match the current contract's price level.

    IMPORTANT: The adjusted prices are only for SIGNAL CALCULATION.
    Never use adjusted prices for P&L calculation or position sizing.
    Those always use the actual current market price.

    Args:
        df:             OHLCV DataFrame with DatetimeIndex
        rollover_dates: List of rollover dates from detect_rollover_gaps()
        market:         Market name for logging

    Returns:
        DataFrame with adjusted OHLC prices. Volume is NOT adjusted.
        Original DataFrame is not modified (returns a copy).

    Example:
        rollover_dates = detect_rollover_gaps(raw_df, market="ES")
        adjusted_df    = apply_panama_adjustment(raw_df, rollover_dates, "ES")
    """
    if not rollover_dates:
        log.info("{market}: No rollover dates provided — no adjustment needed",
                 market=market)
        return df.copy()

    df = df.copy()
    price_cols = ["Open", "High", "Low", "Close"]
    existing   = [c for c in price_cols if c in df.columns]

    # Sort rollover dates newest to oldest (apply adjustments backward)
    sorted_dates = sorted(rollover_dates, reverse=True)
    cumulative_ratio = 1.0

    for rollover_date in sorted_dates:
        if rollover_date not in df.index:
            log.warning("{market}: Rollover date {d} not in DataFrame index — skipping",
                        market=market, d=rollover_date.strftime("%Y-%m-%d"))
            continue

        # Get the date immediately before the rollover
        df_before_rollover = df[df.index < rollover_date]
        if df_before_rollover.empty:
            continue

        prev_date  = df_before_rollover.index[-1]
        prev_close = df.loc[prev_date, "Close"]
        roll_open  = df.loc[rollover_date, "Open"]

        if roll_open == 0 or pd.isna(roll_open) or pd.isna(prev_close):
            log.warning("{market}: Cannot calculate ratio at {d} — invalid prices",
                        market=market, d=rollover_date.strftime("%Y-%m-%d"))
            continue

        ratio = prev_close / roll_open
        cumulative_ratio *= ratio

        # Apply ratio to all bars BEFORE this rollover date
        mask = df.index < rollover_date
        df.loc[mask, existing] *= ratio

        log.debug("{market}: Rollover {d}: ratio={r:.6f} (cumulative={cr:.6f})",
                  market=market,
                  d=rollover_date.strftime("%Y-%m-%d"),
                  r=ratio,
                  cr=cumulative_ratio)

    log.info("{market}: Panama adjustment applied. {n} rollovers. "
             "Cumulative ratio: {r:.4f}",
             market=market, n=len(sorted_dates), r=cumulative_ratio)

    return df


# ── Full pipeline ─────────────────────────────────────────────────────────────

def build_continuous_series(
    df: pd.DataFrame,
    market: str,
    threshold_pct: float = 0.01,
) -> pd.DataFrame:
    """
    Build a continuous, rollover-adjusted futures price series.

    Combines rollover detection and Panama adjustment into one call.
    This is the function called by the backtesting data loader.

    Args:
        df:            Raw OHLCV DataFrame (may contain rollover gaps)
        market:        Market code ("ES", "NQ", "GC", "CL", "ZB", "6E")
        threshold_pct: Gap size threshold for rollover detection

    Returns:
        Adjusted DataFrame with continuous, gap-free prices.
        Same structure as input but with adjusted OHLC values.

    Note:
        For ETF proxies (used in Phase 1-4 backtesting), this function
        returns the DataFrame unchanged because ETFs have no rollover gaps.
        The function is safe to call on any data.

    Example:
        raw_df        = download_market("ES", "2000-01-01", "2024-12-31")
        continuous_df = build_continuous_series(raw_df, "ES")
        # continuous_df is adjusted — safe for signal calculation
    """
    log.info("{market}: Building continuous series from {n} bars",
             market=market, n=len(df))

    rollover_dates = detect_rollover_gaps(df, threshold_pct=threshold_pct, market=market)

    if not rollover_dates:
        log.info("{market}: Series is already continuous — no adjustment needed", market=market)
        return df.copy()

    adjusted_df = apply_panama_adjustment(df, rollover_dates, market=market)

    log.info("{market}: Continuous series complete ({n} bars, {r} rollovers corrected)",
             market=market, n=len(adjusted_df), r=len(rollover_dates))

    return adjusted_df


# ── Rollover calendar (for reference) ────────────────────────────────────────

# Standard CME futures rollover months (front month transitions)
# These are the months when each contract series rolls to the next expiry.
# Used for advanced rollover scheduling (not yet implemented — Phase 6+)

CME_ROLLOVER_MONTHS = {
    "ES":  [3, 6, 9, 12],    # March, June, September, December (quarterly)
    "NQ":  [3, 6, 9, 12],    # Same as ES
    "GC":  [2, 4, 6, 8, 10, 12],  # Bi-monthly
    "CL":  list(range(1, 13)),     # Monthly (every month)
    "ZB":  [3, 6, 9, 12],    # Quarterly
    "6E":  [3, 6, 9, 12],    # Quarterly
}

# Standard roll date: approximately 8 business days before expiration
# Exact dates vary by contract and are published by CME each year.
# For automated rollover scheduling in Phase 6+, use the CME contract
# specifications directly from the Rithmic or IBKR data feed.
