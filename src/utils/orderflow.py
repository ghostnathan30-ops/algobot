"""
AlgoBot -- Order Flow Utilities (VWAP + Synthetic Delta)
=========================================================
Module:  src/utils/orderflow.py
Phase:   5E -- Order Flow Filters
Purpose: Compute intraday VWAP and synthetic cumulative delta from OHLCV
         1-hour bars to filter FHB and ORB signals for order flow alignment.

Why VWAP matters for intraday strategies:
  VWAP is the single most-watched institutional reference price.  Institutions
  benchmark execution against VWAP.  A first-hour breakout is significantly
  stronger when price is already on the correct side of VWAP:
    LONG breakout above VWAP  -> institutions accumulating, breakout has fuel
    LONG breakout below VWAP  -> fighting institutional selling pressure
    SHORT breakout below VWAP -> institutions distributing, continuation likely
    SHORT breakout above VWAP -> short-squeeze risk, lower probability

Why Synthetic Delta:
  True cumulative delta requires tick-level data (every trade tagged as
  buyer-initiated or seller-initiated).  We don't have tick data yet (need
  Sierra Chart + Rithmic).  As a proxy, we estimate buying/selling pressure
  from each bar's OHLC relationship to its high-low range:

    buying_pct  = (Close - Low)  / (High - Low)   -- how much bar closed near high
    selling_pct = (High - Close) / (High - Low)   -- how much bar closed near low
    bar_delta   = (buying_pct - selling_pct) * Volume

  This captures the DIRECTION of volume pressure even without tick data.
  Accuracy is lower than true delta but better than no delta at all.

Academic basis:
  - Madhavan (2000): VWAP as benchmark for institutional execution quality
  - Grinblatt (2001): Institutional buy-side anchors on VWAP for large orders
  - Easley (2002): Volume-weighted price action as proxy for order imbalance

Usage:
    from src.utils.orderflow import add_daily_vwap, add_synthetic_delta

    df = add_daily_vwap(df_1h)
    df = add_synthetic_delta(df_1h)

    # At signal bar:
    is_above_vwap    = row['Close'] > row['vwap']
    delta_bullish    = row['cum_delta'] > 0
    delta_confirming = row['bar_delta'] > 0   # last bar had buying pressure
"""

from __future__ import annotations

import pandas as pd
import numpy as np


# ── Public API ──────────────────────────────────────────────────────────────

def add_daily_vwap(df: pd.DataFrame, price_col: str = "Close",
                   volume_col: str = "Volume") -> pd.DataFrame:
    """
    Add a 'vwap' column (daily anchored VWAP, resets each calendar day).

    VWAP = sum(typical_price * volume) / sum(volume), calculated cumulatively
    from the first bar of each trading day.

    Typical price = (High + Low + Close) / 3

    Args:
        df:          DataFrame with DatetimeIndex and OHLCV columns.
        price_col:   Column to use as close price (default 'Close').
        volume_col:  Volume column (default 'Volume').

    Returns:
        df copy with new columns:
          vwap          -- daily anchored VWAP
          vwap_upper    -- VWAP + 1 ATR  (dynamic resistance)
          vwap_lower    -- VWAP - 1 ATR  (dynamic support)
          above_vwap    -- bool: Close > vwap
    """
    df = df.copy()

    # Typical price
    if "High" in df.columns and "Low" in df.columns:
        typical = (df["High"] + df["Low"] + df[price_col]) / 3.0
    else:
        typical = df[price_col]

    # Volume safety: fill zeros to avoid divide-by-zero
    vol = df[volume_col].copy().clip(lower=1)

    # Trading day group: normalize index to date
    if hasattr(df.index, "normalize"):
        day_key = df.index.normalize()
    else:
        day_key = pd.Series(df.index).dt.normalize().values

    tpv = typical * vol          # typical_price * volume per bar
    cum_tpv = tpv.groupby(day_key).cumsum()
    cum_vol = vol.groupby(day_key).cumsum()

    df["vwap"]       = (cum_tpv / cum_vol).round(4)
    df["above_vwap"] = df[price_col] > df["vwap"]

    # VWAP bands using rolling std of typical price (approximate ATR alternative)
    daily_std = tpv.groupby(day_key).transform(
        lambda x: x.expanding().std().fillna(0)
    ) / vol.clip(lower=1)
    df["vwap_upper"] = (df["vwap"] + daily_std).round(4)
    df["vwap_lower"] = (df["vwap"] - daily_std).round(4)

    return df


def add_synthetic_delta(df: pd.DataFrame,
                        volume_col: str = "Volume",
                        reset_daily: bool = True) -> pd.DataFrame:
    """
    Add synthetic cumulative delta columns.

    True delta requires tick data. This proxy estimates buying vs selling
    pressure from each bar's OHLC position within its high-low range.

    Formula per bar:
        range    = High - Low  (use 0.0001 floor to avoid divide-by-zero)
        buy_frac = (Close - Low)  / range   -- 1.0 if close at high (full buying)
        sel_frac = (High - Close) / range   -- 1.0 if close at low (full selling)
        bar_delta = (buy_frac - sel_frac) * Volume

    Cumulative delta is optionally reset each trading day.

    Args:
        df:           DataFrame with OHLCV columns and DatetimeIndex.
        volume_col:   Volume column name.
        reset_daily:  If True, cumulative delta resets at each trading day open.

    Returns:
        df copy with new columns:
          bar_delta      -- per-bar synthetic delta (positive = net buying)
          cum_delta      -- cumulative delta (daily-anchored if reset_daily=True)
          delta_positive -- bool: cum_delta > 0 (net buying pressure today)
    """
    df = df.copy()

    if "High" not in df.columns or "Low" not in df.columns:
        df["bar_delta"]      = 0.0
        df["cum_delta"]      = 0.0
        df["delta_positive"] = False
        return df

    hl_range = (df["High"] - df["Low"]).clip(lower=1e-6)
    buy_frac  = (df["Close"] - df["Low"])  / hl_range
    sell_frac = (df["High"]  - df["Close"]) / hl_range

    vol = df[volume_col].clip(lower=0)
    df["bar_delta"] = ((buy_frac - sell_frac) * vol).round(0)

    if reset_daily:
        if hasattr(df.index, "normalize"):
            day_key = df.index.normalize()
        else:
            day_key = pd.Series(df.index).dt.normalize().values
        df["cum_delta"] = df["bar_delta"].groupby(day_key).cumsum().round(0)
    else:
        df["cum_delta"] = df["bar_delta"].cumsum().round(0)

    df["delta_positive"] = df["cum_delta"] > 0
    return df


def vwap_signal_aligned(row: pd.Series,
                         direction: str,
                         vwap_col: str = "vwap",
                         price_col: str = "Close") -> bool:
    """
    Return True if the trade direction is aligned with VWAP.

    LONG  = price must be ABOVE vwap (buying above institutional benchmark)
    SHORT = price must be BELOW vwap (selling below institutional benchmark)

    Args:
        row:       Single bar row (pd.Series with vwap and Close columns).
        direction: 'LONG' or 'SHORT'.
        vwap_col:  VWAP column name.
        price_col: Price column to compare against VWAP.

    Returns:
        True if order flow is aligned with the trade direction.
    """
    try:
        price = float(row[price_col])
        vwap  = float(row[vwap_col])
    except (KeyError, TypeError, ValueError):
        return True   # no data -> don't block

    if direction.upper() == "LONG":
        return price >= vwap
    elif direction.upper() == "SHORT":
        return price <= vwap
    return True


def delta_signal_aligned(row: pd.Series,
                          direction: str,
                          cum_delta_col: str = "cum_delta",
                          bar_delta_col: str = "bar_delta",
                          require_both: bool = False) -> bool:
    """
    Return True if synthetic delta confirms the trade direction.

    For LONG:  cum_delta > 0 (net buying pressure on the day)
    For SHORT: cum_delta < 0 (net selling pressure on the day)

    If require_both is True, BOTH cum_delta AND bar_delta must confirm.

    Args:
        row:            Single bar row.
        direction:      'LONG' or 'SHORT'.
        cum_delta_col:  Cumulative delta column name.
        bar_delta_col:  Per-bar delta column name.
        require_both:   If True, both cumulative AND last bar must confirm.

    Returns:
        True if delta is aligned or data unavailable.
    """
    try:
        cum_d = float(row[cum_delta_col])
        bar_d = float(row[bar_delta_col])
    except (KeyError, TypeError, ValueError):
        return True   # no data -> don't block

    direction = direction.upper()

    if require_both:
        if direction == "LONG":
            return cum_d > 0 and bar_d > 0
        elif direction == "SHORT":
            return cum_d < 0 and bar_d < 0
    else:
        if direction == "LONG":
            return cum_d > 0
        elif direction == "SHORT":
            return cum_d < 0

    return True


def get_orderflow_summary(df: pd.DataFrame,
                           signal_bar_idx,
                           direction: str) -> dict:
    """
    Return a human-readable order flow summary for a signal bar.
    Useful for trade logging and post-analysis.

    Args:
        df:              Full 1-hour DataFrame with vwap and delta columns.
        signal_bar_idx:  Index value of the signal bar.
        direction:       'LONG' or 'SHORT'.

    Returns:
        dict with vwap, cum_delta, bar_delta, vwap_aligned, delta_aligned, score
    """
    try:
        row = df.loc[signal_bar_idx]
    except KeyError:
        return {"error": "index not found"}

    vwap        = float(row.get("vwap",      float("nan")))
    cum_delta   = float(row.get("cum_delta", float("nan")))
    bar_delta   = float(row.get("bar_delta", float("nan")))
    price       = float(row.get("Close",     float("nan")))

    vwap_ok  = vwap_signal_aligned(row, direction)
    delta_ok = delta_signal_aligned(row, direction)

    # Score: 0, 1, or 2 confirms
    score = int(vwap_ok) + int(delta_ok)

    return {
        "price":         round(price,     4),
        "vwap":          round(vwap,      4),
        "above_vwap":    price >= vwap if not (pd.isna(price) or pd.isna(vwap)) else None,
        "cum_delta":     int(cum_delta)  if not pd.isna(cum_delta) else None,
        "bar_delta":     int(bar_delta)  if not pd.isna(bar_delta) else None,
        "vwap_aligned":  vwap_ok,
        "delta_aligned": delta_ok,
        "of_score":      score,          # 0=bad, 1=partial, 2=full confirm
        "direction":     direction,
    }
