"""
AlgoBot — Volume Profile Computation
=====================================
Module:  src/strategy/volume_profile.py
Purpose: Compute Volume Profile metrics from intraday OHLCV 1H bar data and
         attach the *previous* day's profile to the DataFrame so downstream
         signal generators can use VPOC / VAH / VAL / LVN as confirmation
         layers with zero lookahead bias (all values are shift(1) = prev day).

Metrics produced per trading day:
  - VPOC  — Volume Point of Control (price bin with highest volume)
  - VAH   — Value Area High  (70% value area, upper boundary)
  - VAL   — Value Area Low   (70% value area, lower boundary)
  - LVN   — Low Volume Nodes (bins with < 20% of VPOC volume)

Columns added to the DataFrame (previous-day profile, no lookahead):
  vp_vpoc               — prev-day VPOC price
  vp_vah                — prev-day Value Area High
  vp_val                — prev-day Value Area Low
  vp_has_lvn_near_price — bool: close within 0.3% of any LVN level
  vp_price_in_value_area— bool: val <= close <= vah
  vp_above_vpoc         — bool: close > vpoc
  vp_poc_distance_pct   — float: abs(close - vpoc) / close * 100

Usage:
    from src.strategy.volume_profile import add_volume_profile_columns

    df = add_volume_profile_columns(df_1h, market="NQ")
    # df now has vp_* columns ready for signal filters
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Allow importing from project root when run directly
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import get_logger

log = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Tick sizes by market (minimum price increment)
TICK_SIZES: dict[str, float] = {
    "NQ":  0.25,
    "MNQ": 0.25,
    "ES":  0.25,
    "MES": 0.25,
    "GC":  0.10,
    "MGC": 0.10,
    "CL":  0.01,
    "MCL": 0.01,
}

_VALUE_AREA_PCT   = 0.70   # Standard 70% value area
_LVN_THRESHOLD    = 0.20   # Bins with volume < 20% of VPOC are LVN
_LVN_PROXIMITY    = 0.003  # 0.3% proximity to flag vp_has_lvn_near_price
_DEFAULT_N_BINS   = 200    # Resolution: good accuracy without excessive compute


# ── Private helpers ────────────────────────────────────────────────────────────

def _build_daily_profile(
    day_df: pd.DataFrame,
    tick_size: float = 0.25,
    n_bins: int = _DEFAULT_N_BINS,
) -> dict:
    """
    Build a Volume Profile for a single trading day.

    Parameters
    ----------
    day_df : pd.DataFrame
        Rows for one trading day. Required columns: open, high, low, close, volume.
        Column names are case-insensitive (lowercased internally).
    tick_size : float
        Minimum price increment for the instrument.
    n_bins : int
        Number of price bins to divide the day's range into.

    Returns
    -------
    dict with keys:
        bins         — np.array of bin midpoint prices
        volumes      — np.array of volume per bin
        vpoc         — float, price of highest-volume bin
        vah          — float, Value Area High
        val          — float, Value Area Low
        lvn_levels   — list[float], bin prices with volume < 20% of VPOC volume
        total_volume — float
        date         — str, trading date
    """
    # Normalise column names to lowercase
    df = day_df.copy()
    df.columns = [c.lower() for c in df.columns]

    date_str = str(df.index[0].date()) if hasattr(df.index[0], "date") else str(df.index[0])[:10]

    # Guard: drop zero-volume bars; if nothing left return empty profile
    df = df[df["volume"] > 0]
    if df.empty:
        log.warning("volume_profile: no volume data for date {d}", d=date_str)
        return _empty_profile(date_str)

    price_min = df["low"].min()
    price_max = df["high"].max()

    # Degenerate case: entire day is a single price (e.g. circuit breaker)
    if price_max <= price_min:
        price_min = price_min - tick_size
        price_max = price_max + tick_size

    # Build evenly-spaced bin edges; bin midpoints are used as reference prices
    bin_edges = np.linspace(price_min, price_max, n_bins + 1)
    bin_mids  = (bin_edges[:-1] + bin_edges[1:]) / 2.0
    bin_width = bin_edges[1] - bin_edges[0]
    volumes   = np.zeros(n_bins, dtype=np.float64)

    # Distribute each bar's volume across the bins it spans
    for _, row in df.iterrows():
        bar_low  = row["low"]
        bar_high = row["high"]
        bar_vol  = row["volume"]

        if bar_high == bar_low:
            # Doji / single-price bar — all volume to nearest bin
            idx = int(np.argmin(np.abs(bin_mids - bar_low)))
            volumes[idx] += bar_vol
        else:
            # Find bins overlapping [bar_low, bar_high]
            lo_idx = int(np.searchsorted(bin_edges, bar_low,  side="left"))
            hi_idx = int(np.searchsorted(bin_edges, bar_high, side="right"))
            lo_idx = max(0, lo_idx - 1)
            hi_idx = min(n_bins - 1, hi_idx)

            bar_range = bar_high - bar_low
            for b in range(lo_idx, hi_idx + 1):
                overlap_lo  = max(bar_low,       bin_edges[b])
                overlap_hi  = min(bar_high,      bin_edges[b + 1])
                overlap_len = max(0.0, overlap_hi - overlap_lo)
                volumes[b] += bar_vol * (overlap_len / bar_range)

    total_volume = float(volumes.sum())
    if total_volume == 0:
        log.warning("volume_profile: zero total volume for date {d}", d=date_str)
        return _empty_profile(date_str)

    vpoc_idx  = int(np.argmax(volumes))
    vpoc      = float(bin_mids[vpoc_idx])
    vah, val  = _compute_vah_val(bin_mids, volumes, vpoc_idx)

    lvn_cutoff  = volumes[vpoc_idx] * _LVN_THRESHOLD
    lvn_levels  = [float(bin_mids[i]) for i in range(n_bins) if volumes[i] < lvn_cutoff]

    return {
        "bins":         bin_mids,
        "volumes":      volumes,
        "vpoc":         vpoc,
        "vah":          vah,
        "val":          val,
        "lvn_levels":   lvn_levels,
        "total_volume": total_volume,
        "date":         date_str,
    }


def _empty_profile(date_str: str) -> dict:
    """Return a sentinel profile with NaN values for missing/empty days."""
    return {
        "bins":         np.array([], dtype=np.float64),
        "volumes":      np.array([], dtype=np.float64),
        "vpoc":         float("nan"),
        "vah":          float("nan"),
        "val":          float("nan"),
        "lvn_levels":   [],
        "total_volume": 0.0,
        "date":         date_str,
    }


def _compute_vah_val(
    bins: np.ndarray,
    volumes: np.ndarray,
    vpoc_idx: int,
) -> tuple[float, float]:
    """
    Compute Value Area High and Low using the standard 70% algorithm.

    Starting from the VPOC bin, alternately expand upward or downward by adding
    the adjacent bin with the higher volume next, until the accumulated volume
    reaches 70% of total volume.

    Parameters
    ----------
    bins      : array of bin midpoint prices (ascending)
    volumes   : array of volume per bin (aligned with bins)
    vpoc_idx  : index of the VPOC bin

    Returns
    -------
    (vah, val) : tuple of floats
    """
    total    = float(volumes.sum())
    target   = total * _VALUE_AREA_PCT
    n        = len(bins)

    accumulated = float(volumes[vpoc_idx])
    lo_idx      = vpoc_idx
    hi_idx      = vpoc_idx

    while accumulated < target:
        can_expand_up   = hi_idx + 1 < n
        can_expand_down = lo_idx - 1 >= 0

        if not can_expand_up and not can_expand_down:
            break  # Entire profile is within value area

        vol_up   = float(volumes[hi_idx + 1]) if can_expand_up   else -1.0
        vol_down = float(volumes[lo_idx - 1]) if can_expand_down else -1.0

        # Add the higher-volume side first (standard TPO/VP convention)
        if vol_up >= vol_down:
            hi_idx     += 1
            accumulated += vol_up
        else:
            lo_idx     -= 1
            accumulated += vol_down

    return float(bins[hi_idx]), float(bins[lo_idx])


# ── Public API ─────────────────────────────────────────────────────────────────

def add_volume_profile_columns(
    df: pd.DataFrame,
    market: str = "NQ",
    tick_size: Optional[float] = None,
    n_bins: int = _DEFAULT_N_BINS,
) -> pd.DataFrame:
    """
    Compute Volume Profile for each trading day and attach the *previous* day's
    profile as new columns on the DataFrame (no lookahead bias).

    Parameters
    ----------
    df : pd.DataFrame
        1-hour OHLCV bars with a timezone-aware or naive DatetimeIndex.
        Required columns (case-insensitive): open, high, low, close, volume.
    market : str
        Instrument ticker. Used to look up tick_size when not provided explicitly.
        Supported: NQ, MNQ, ES, MES, GC, MGC, CL, MCL.
    tick_size : float | None
        Override the tick size. If None, resolved from TICK_SIZES[market].
    n_bins : int
        Number of price bins per day (default 200).

    Returns
    -------
    pd.DataFrame
        Input df with the following columns added:
          vp_vpoc                — prev-day VPOC price
          vp_vah                 — prev-day Value Area High
          vp_val                 — prev-day Value Area Low
          vp_has_lvn_near_price  — bool: close within 0.3% of any LVN
          vp_price_in_value_area — bool: val <= close <= vah
          vp_above_vpoc          — bool: close > vpoc
          vp_poc_distance_pct    — float: abs(close - vpoc) / close * 100
        First day's rows will have NaN / False for all vp_* columns.
    """
    if tick_size is None:
        tick_size = TICK_SIZES.get(market.upper(), 0.25)
        log.debug(
            "volume_profile: market={m} tick_size={t}",
            m=market, t=tick_size,
        )

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]

    required = {"open", "high", "low", "close", "volume"}
    missing  = required - set(df.columns)
    if missing:
        raise ValueError(f"volume_profile: DataFrame missing columns: {missing}")

    # Derive the trading date for each row
    if hasattr(df.index, "date"):
        df["_date"] = df.index.date
    else:
        df["_date"] = pd.to_datetime(df.index).date

    trading_days = sorted(df["_date"].unique())
    log.info(
        "volume_profile: building profiles for {n} days ({m})",
        n=len(trading_days), m=market,
    )

    # Build profile for every trading day
    profiles: dict[object, dict] = {}
    for day in trading_days:
        day_mask = df["_date"] == day
        day_df   = df.loc[day_mask, ["open", "high", "low", "close", "volume"]]
        profiles[day] = _build_daily_profile(day_df, tick_size=tick_size, n_bins=n_bins)

    # Map each row to the *previous* day's profile (shift by 1 day)
    day_to_prev: dict[object, object | None] = {}
    for i, day in enumerate(trading_days):
        day_to_prev[day] = trading_days[i - 1] if i > 0 else None

    # Initialise output columns with NaN / False
    df["vp_vpoc"]                = float("nan")
    df["vp_vah"]                 = float("nan")
    df["vp_val"]                 = float("nan")
    df["vp_has_lvn_near_price"]  = False
    df["vp_price_in_value_area"] = False
    df["vp_above_vpoc"]          = False
    df["vp_poc_distance_pct"]    = float("nan")

    for day in trading_days:
        prev_day = day_to_prev[day]
        if prev_day is None:
            continue  # First day — no prior profile, leave NaN

        prof = profiles[prev_day]
        if np.isnan(prof["vpoc"]):
            continue  # Prev day had no usable data

        mask  = df["_date"] == day
        close = df.loc[mask, "close"]

        vpoc = prof["vpoc"]
        vah  = prof["vah"]
        val  = prof["val"]
        lvns = prof["lvn_levels"]

        df.loc[mask, "vp_vpoc"] = vpoc
        df.loc[mask, "vp_vah"]  = vah
        df.loc[mask, "vp_val"]  = val

        # LVN proximity: True if close is within _LVN_PROXIMITY of any LVN level
        if lvns:
            lvn_arr = np.array(lvns)
            near_lvn = close.apply(
                lambda c: bool(np.any(np.abs(lvn_arr - c) / c <= _LVN_PROXIMITY))
            )
            df.loc[mask, "vp_has_lvn_near_price"] = near_lvn.values

        # Value area membership
        df.loc[mask, "vp_price_in_value_area"] = (
            (close >= val) & (close <= vah)
        ).values

        # Above VPOC flag
        df.loc[mask, "vp_above_vpoc"] = (close > vpoc).values

        # Distance from VPOC as percentage of close
        df.loc[mask, "vp_poc_distance_pct"] = (
            (close - vpoc).abs() / close * 100.0
        ).values

    # Restore original column casing (drop internal helper)
    df = df.drop(columns=["_date"])

    log.info("volume_profile: columns added — vp_vpoc/vah/val/lvn/in_va/above_poc/dist")
    return df


def get_profile_for_date(
    df: pd.DataFrame,
    date_str: str,
    market: str = "NQ",
    tick_size: Optional[float] = None,
    n_bins: int = _DEFAULT_N_BINS,
) -> Optional[dict]:
    """
    Return the Volume Profile dict for a specific date string (YYYY-MM-DD).

    Useful for debugging and visual inspection of a single day's profile.

    Parameters
    ----------
    df       : 1-hour OHLCV DataFrame with DatetimeIndex
    date_str : Target date as 'YYYY-MM-DD'
    market   : Instrument ticker (used for default tick_size)
    tick_size: Override tick size; resolved from market if None
    n_bins   : Number of price bins

    Returns
    -------
    dict | None — Profile dict (same schema as _build_daily_profile) or None
                  if the date is not present in df.
    """
    if tick_size is None:
        tick_size = TICK_SIZES.get(market.upper(), 0.25)

    df_work = df.copy()
    df_work.columns = [c.lower() for c in df_work.columns]

    if hasattr(df_work.index, "date"):
        dates = df_work.index.date
    else:
        dates = pd.to_datetime(df_work.index).date

    target = pd.Timestamp(date_str).date()
    mask   = dates == target
    if not mask.any():
        log.warning("volume_profile: date {d} not found in DataFrame", d=date_str)
        return None

    day_df = df_work.loc[mask, ["open", "high", "low", "close", "volume"]]
    return _build_daily_profile(day_df, tick_size=tick_size, n_bins=n_bins)
