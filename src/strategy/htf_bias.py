"""
AlgoBot — Higher-Timeframe Bias Engine
========================================
Module:  src/strategy/htf_bias.py
Phase:   5 — MTF Architecture
Purpose: Computes weekly and monthly market bias (BULL / BEAR / NEUTRAL)
         from existing daily OHLCV data by resampling to higher timeframes.

Core concept: Top-Down Multi-Timeframe Analysis
  Before entering any trade, determine "who controls the market"
  at the higher timeframes. Only trade in their direction.

  Monthly bias = macro trend (1-6 months)
  Weekly bias  = intermediate trend (1-4 weeks)

  Combined, these two filters eliminate the single biggest failure
  in the Phase 4 backtest: VMR SHORT entries into strong uptrends,
  and trend entries against the dominant directional flow.

Bias values:
  "BULL"    — higher TF is bullish, prefer longs
  "BEAR"    — higher TF is bearish, prefer shorts
  "NEUTRAL" — no strong HTF conviction, both directions possible

Usage:
    from src.strategy.htf_bias import add_htf_bias

    # df is the daily DataFrame (output of calculate_indicators)
    df = add_htf_bias(df, config, market)

    # New columns added to df (forward-filled to daily frequency):
    #   htf_weekly_bias   -- "BULL" | "BEAR" | "NEUTRAL"
    #   htf_monthly_bias  -- "BULL" | "BEAR" | "NEUTRAL"
    #   htf_combined_bias -- "BULL" | "BEAR" | "NEUTRAL"
"""

from __future__ import annotations

import pandas as pd
import numpy as np

from src.utils.logger import get_logger

log = get_logger(__name__)

# ── Bias constants ─────────────────────────────────────────────────────────────
BULL    = "BULL"
BEAR    = "BEAR"
NEUTRAL = "NEUTRAL"


# ── Weekly bias ────────────────────────────────────────────────────────────────

def _compute_weekly_bias(daily_df: pd.DataFrame, cfg: dict) -> pd.Series:
    """
    Compute weekly bias from daily OHLCV data.

    Method:
      1. Resample daily bars to weekly (Friday close = canonical weekly bar).
      2. Compute EMA_fast and EMA_slow on weekly close prices.
      3. Compute ADX on weekly bars.
      4. Classify bias:
           EMA_fast > EMA_slow AND ADX >= adx_min  -> BULL
           EMA_fast < EMA_slow AND ADX >= adx_min  -> BEAR
           otherwise (ADX too low or EMAs crossing) -> NEUTRAL
      5. Forward-fill weekly values back to daily frequency so every
         daily bar knows the current weekly bias.

    Args:
        daily_df: Daily OHLCV DataFrame with DatetimeIndex.
        cfg:      htf_bias.weekly section of config.

    Returns:
        Series with same index as daily_df. Values: BULL / BEAR / NEUTRAL.
    """
    ema_fast_p = int(cfg.get("ema_fast", 8))
    ema_slow_p = int(cfg.get("ema_slow", 21))
    adx_p      = int(cfg.get("adx_period", 14))
    adx_min    = float(cfg.get("adx_min", 18))

    # ── Resample to weekly bars ────────────────────────────────────────────────
    weekly = daily_df[["Open", "High", "Low", "Close", "Volume"]].resample("W-FRI").agg(
        {
            "Open":   "first",
            "High":   "max",
            "Low":    "min",
            "Close":  "last",
            "Volume": "sum",
        }
    ).dropna(subset=["Close"])

    if len(weekly) < max(ema_fast_p, ema_slow_p, adx_p) + 5:
        log.warning("Weekly bias: not enough weeks ({n}) to compute indicators",
                    n=len(weekly))
        return pd.Series(NEUTRAL, index=daily_df.index, name="htf_weekly_bias")

    # ── EMA on weekly close ────────────────────────────────────────────────────
    weekly["ema_fast"] = weekly["Close"].ewm(span=ema_fast_p, adjust=False).mean()
    weekly["ema_slow"] = weekly["Close"].ewm(span=ema_slow_p, adjust=False).mean()

    # ── ADX on weekly bars (Wilder method) ────────────────────────────────────
    weekly = _add_adx(weekly, adx_p)

    # ── Classify weekly bias ───────────────────────────────────────────────────
    def _classify(row: pd.Series) -> str:
        if pd.isna(row["ema_fast"]) or pd.isna(row["ema_slow"]) or pd.isna(row["adx"]):
            return NEUTRAL
        adx_ok = row["adx"] >= adx_min
        if row["ema_fast"] > row["ema_slow"] and adx_ok:
            return BULL
        if row["ema_fast"] < row["ema_slow"] and adx_ok:
            return BEAR
        return NEUTRAL

    weekly["bias"] = weekly.apply(_classify, axis=1)

    # ── Reindex to daily and forward-fill ─────────────────────────────────────
    # Each daily bar inherits the bias of the most recent completed weekly bar.
    daily_bias = weekly["bias"].reindex(daily_df.index, method="ffill")

    # Fill any remaining NaN at the start (before first weekly bar) as NEUTRAL
    daily_bias = daily_bias.fillna(NEUTRAL)
    daily_bias.name = "htf_weekly_bias"

    return daily_bias


# ── Monthly bias ───────────────────────────────────────────────────────────────

def _compute_monthly_bias(daily_df: pd.DataFrame, cfg: dict) -> pd.Series:
    """
    Compute monthly bias from daily OHLCV data.

    Method:
      1. Resample daily bars to monthly (last trading day of month).
      2. Compute SMA(sma_period) on monthly close prices.
      3. Classify bias based on price distance from SMA:
           Close > SMA × (1 + neutral_band)  -> BULL
           Close < SMA × (1 - neutral_band)  -> BEAR
           Within neutral_band of SMA        -> NEUTRAL
      4. Forward-fill monthly values to daily frequency.

    Args:
        daily_df: Daily OHLCV DataFrame with DatetimeIndex.
        cfg:      htf_bias.monthly section of config.

    Returns:
        Series with same index as daily_df. Values: BULL / BEAR / NEUTRAL.
    """
    sma_period   = int(cfg.get("sma_period", 6))
    neutral_band = float(cfg.get("neutral_band", 0.015))

    # ── Resample to monthly bars ───────────────────────────────────────────────
    monthly = daily_df[["Open", "High", "Low", "Close", "Volume"]].resample("ME").agg(
        {
            "Open":   "first",
            "High":   "max",
            "Low":    "min",
            "Close":  "last",
            "Volume": "sum",
        }
    ).dropna(subset=["Close"])

    if len(monthly) < sma_period + 2:
        log.warning("Monthly bias: not enough months ({n}) to compute SMA({p})",
                    n=len(monthly), p=sma_period)
        return pd.Series(NEUTRAL, index=daily_df.index, name="htf_monthly_bias")

    # ── SMA on monthly close ───────────────────────────────────────────────────
    monthly["sma"] = monthly["Close"].rolling(sma_period, min_periods=sma_period).mean()

    # ── Classify monthly bias ─────────────────────────────────────────────────
    def _classify(row: pd.Series) -> str:
        if pd.isna(row["sma"]):
            return NEUTRAL
        upper = row["sma"] * (1.0 + neutral_band)
        lower = row["sma"] * (1.0 - neutral_band)
        if row["Close"] > upper:
            return BULL
        if row["Close"] < lower:
            return BEAR
        return NEUTRAL

    monthly["bias"] = monthly.apply(_classify, axis=1)

    # ── Reindex to daily and forward-fill ─────────────────────────────────────
    daily_bias = monthly["bias"].reindex(daily_df.index, method="ffill")
    daily_bias = daily_bias.fillna(NEUTRAL)
    daily_bias.name = "htf_monthly_bias"

    return daily_bias


# ── Combined bias ──────────────────────────────────────────────────────────────

def _compute_combined_bias(
    weekly_bias: pd.Series,
    monthly_bias: pd.Series,
    require_monthly_agreement: bool,
) -> pd.Series:
    """
    Combine weekly and monthly bias into a single trading bias.

    Logic:
      If require_monthly_agreement=False (default):
        Use weekly bias as primary. Monthly acts as a veto only on
        direct contradiction (weekly=BULL but monthly=BEAR -> NEUTRAL).
        This avoids over-filtering: monthly is laggy and would miss
        many good swing entries if required to agree.

      If require_monthly_agreement=True (strict mode):
        Both weekly and monthly must agree for BULL or BEAR.
        Any disagreement = NEUTRAL.

    Args:
        weekly_bias:               Series of BULL/BEAR/NEUTRAL per day.
        monthly_bias:              Series of BULL/BEAR/NEUTRAL per day.
        require_monthly_agreement: True = both must agree.

    Returns:
        Series "htf_combined_bias" with values BULL / BEAR / NEUTRAL.
    """
    combined = pd.Series(NEUTRAL, index=weekly_bias.index, name="htf_combined_bias")

    for idx in weekly_bias.index:
        wb = weekly_bias.loc[idx]
        mb = monthly_bias.loc[idx]

        if require_monthly_agreement:
            # Strict: both must agree
            if wb == BULL and mb == BULL:
                combined.loc[idx] = BULL
            elif wb == BEAR and mb == BEAR:
                combined.loc[idx] = BEAR
            else:
                combined.loc[idx] = NEUTRAL
        else:
            # Relaxed: weekly leads, monthly only vetoes direct contradictions
            if wb == BULL:
                combined.loc[idx] = NEUTRAL if mb == BEAR else BULL
            elif wb == BEAR:
                combined.loc[idx] = NEUTRAL if mb == BULL else BEAR
            else:
                # Weekly is NEUTRAL — check monthly for any directional signal
                combined.loc[idx] = mb

    return combined


# ── ADX helper ─────────────────────────────────────────────────────────────────

def _add_adx(df: pd.DataFrame, period: int) -> pd.DataFrame:
    """
    Add ADX column to a OHLCV DataFrame using Wilder smoothing.

    Works on any timeframe (daily, weekly, monthly).
    Modifies df in-place with column 'adx'.

    Args:
        df:     DataFrame with High, Low, Close columns.
        period: ADX smoothing period (typically 14).

    Returns:
        Same DataFrame with 'adx' column added.
    """
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]

    # True Range
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    # Directional movements
    up_move   = high - high.shift(1)
    down_move = low.shift(1) - low

    dm_plus  = np.where((up_move > down_move) & (up_move > 0), up_move,  0.0)
    dm_minus = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    dm_plus_s  = pd.Series(dm_plus,  index=df.index)
    dm_minus_s = pd.Series(dm_minus, index=df.index)

    # Wilder smoothing (equivalent to EMA with alpha = 1/period)
    alpha = 1.0 / period

    tr_s    = tr.ewm(alpha=alpha, adjust=False).mean()
    dmp_s   = dm_plus_s.ewm(alpha=alpha, adjust=False).mean()
    dmm_s   = dm_minus_s.ewm(alpha=alpha, adjust=False).mean()

    di_plus  = 100.0 * dmp_s  / tr_s.replace(0, np.nan)
    di_minus = 100.0 * dmm_s  / tr_s.replace(0, np.nan)

    dx_denom = (di_plus + di_minus).replace(0, np.nan)
    dx = 100.0 * (di_plus - di_minus).abs() / dx_denom

    df["adx"] = dx.ewm(alpha=alpha, adjust=False).mean()
    return df


# ── Public API ─────────────────────────────────────────────────────────────────

def add_htf_bias(df: pd.DataFrame, config: dict, market: str) -> pd.DataFrame:
    """
    Add higher-timeframe bias columns to a daily OHLCV DataFrame.

    This is the main public function. Call it after calculate_indicators()
    and before signal generation. It adds three columns:
      - htf_weekly_bias   : "BULL" | "BEAR" | "NEUTRAL" (weekly EMA alignment)
      - htf_monthly_bias  : "BULL" | "BEAR" | "NEUTRAL" (monthly SMA position)
      - htf_combined_bias : "BULL" | "BEAR" | "NEUTRAL" (combined decision)

    These columns are forward-filled — every daily bar has a value from
    the most recent completed higher-timeframe bar.

    Args:
        df:      Daily OHLCV DataFrame with DatetimeIndex and price columns
                 (Open, High, Low, Close, Volume). Must have at least
                 21 weeks (~5 months) of history for indicators to warm up.
        config:  Full config dict (or sub-dict containing 'htf_bias' key).
        market:  Market code for logging (e.g., "ES").

    Returns:
        DataFrame with 3 new HTF bias columns added. All other columns
        are unchanged. df is modified in-place and also returned.

    Raises:
        No exceptions — insufficient history returns NEUTRAL bias.

    Example:
        df = calculate_indicators(raw_df, cfg, "ES")
        df = add_htf_bias(df, config, "ES")
        print(df[["Close", "htf_weekly_bias", "htf_monthly_bias", "htf_combined_bias"]].tail())
    """
    htf_cfg = config.get("htf_bias", {})
    weekly_cfg  = htf_cfg.get("weekly",  {})
    monthly_cfg = htf_cfg.get("monthly", {})
    require_both = bool(htf_cfg.get("require_monthly_agreement", False))

    log.debug("{market}: Computing HTF bias ({n} daily bars)",
              market=market, n=len(df))

    # ── Compute per-timeframe bias ─────────────────────────────────────────────
    weekly_bias  = _compute_weekly_bias(df, weekly_cfg)
    monthly_bias = _compute_monthly_bias(df, monthly_cfg)

    # ── Combine ────────────────────────────────────────────────────────────────
    combined_bias = _compute_combined_bias(weekly_bias, monthly_bias, require_both)

    # ── Attach to DataFrame ────────────────────────────────────────────────────
    df["htf_weekly_bias"]   = weekly_bias
    df["htf_monthly_bias"]  = monthly_bias
    df["htf_combined_bias"] = combined_bias

    # ── Log summary ────────────────────────────────────────────────────────────
    if len(df) > 0:
        tail = df.tail(252)  # Last year
        w_bull = (tail["htf_weekly_bias"]   == BULL).sum()
        w_bear = (tail["htf_weekly_bias"]   == BEAR).sum()
        w_neut = (tail["htf_weekly_bias"]   == NEUTRAL).sum()
        c_bull = (tail["htf_combined_bias"] == BULL).sum()
        c_bear = (tail["htf_combined_bias"] == BEAR).sum()

        log.info(
            "{market}: HTF bias (last 252 days) "
            "weekly=[B:{wb} Br:{wbr} N:{wn}] "
            "combined=[B:{cb} Br:{cbr}]",
            market=market,
            wb=w_bull, wbr=w_bear, wn=w_neut,
            cb=c_bull, cbr=c_bear,
        )

    return df


def get_current_bias(df: pd.DataFrame) -> dict:
    """
    Return the current (most recent) HTF bias for a market.

    Convenience function for logging and paper trading status checks.

    Args:
        df: Daily DataFrame with htf_bias columns (output of add_htf_bias).

    Returns:
        dict with keys: weekly, monthly, combined.

    Example:
        bias = get_current_bias(df)
        print(f"ES bias: {bias['combined']}")  # "BULL"
    """
    if df.empty or "htf_combined_bias" not in df.columns:
        return {"weekly": NEUTRAL, "monthly": NEUTRAL, "combined": NEUTRAL}

    last = df.iloc[-1]
    return {
        "weekly":   str(last.get("htf_weekly_bias",   NEUTRAL)),
        "monthly":  str(last.get("htf_monthly_bias",  NEUTRAL)),
        "combined": str(last.get("htf_combined_bias", NEUTRAL)),
    }


def bias_allows_long(df: pd.DataFrame, index: int, use_combined: bool = True) -> bool:
    """
    Check if the HTF bias allows a LONG entry at a specific bar.

    A LONG entry is allowed when combined bias is BULL or NEUTRAL.
    Only blocked when combined bias is explicitly BEAR.

    Args:
        df:           Daily DataFrame with htf_bias columns.
        index:        Integer row index into df (iloc position).
        use_combined: True = use htf_combined_bias. False = weekly only.

    Returns:
        True if a long entry is allowed, False if blocked by HTF bias.
    """
    col = "htf_combined_bias" if use_combined else "htf_weekly_bias"
    if col not in df.columns or index >= len(df):
        return True  # No bias data = allow (fail-open, not fail-closed)
    bias = df.iloc[index][col]
    return bias != BEAR


def bias_allows_short(df: pd.DataFrame, index: int, use_combined: bool = True) -> bool:
    """
    Check if the HTF bias allows a SHORT entry at a specific bar.

    A SHORT entry is allowed when combined bias is BEAR or NEUTRAL.
    Only blocked when combined bias is explicitly BULL.

    Args:
        df:           Daily DataFrame with htf_bias columns.
        index:        Integer row index into df (iloc position).
        use_combined: True = use htf_combined_bias. False = weekly only.

    Returns:
        True if a short entry is allowed, False if blocked by HTF bias.
    """
    col = "htf_combined_bias" if use_combined else "htf_weekly_bias"
    if col not in df.columns or index >= len(df):
        return True  # No bias data = allow (fail-open)
    bias = df.iloc[index][col]
    return bias != BULL
