"""
AlgoBot — Technical Indicators
================================
Module:  src/strategy/indicators.py
Phase:   2 — Strategy Signals
Purpose: Calculates all technical indicators used by TMA, DCS, and VMR
         signal generators. Single source of truth for all indicator math.

Why one indicators module:
  Having all indicator calculations in one place means:
  - No duplicated EMA or ATR logic across multiple signal files
  - Easy to verify indicator math against reference implementations
  - Simple to swap indicator library (e.g., ta -> talib) in one file
  - All signals use IDENTICAL indicator values — no subtle discrepancies

Indicator library:
  Uses the 'ta' library (pip install ta>=0.11.0).
  This covers all our needs: EMA, ATR, RSI, ADX, Donchian Channel.
  The 'ta' library wraps pandas operations and is Python 3.11 compatible.

Output convention:
  calculate_indicators() returns the input DataFrame with new columns added.
  Column naming is strict and consistent — all downstream code depends on:
    ema_fast, ema_medium, ema_slow
    atr, atr_pct
    rsi
    adx, di_plus, di_minus
    donchian_high, donchian_low, donchian_mid
    donchian_exit_high, donchian_exit_low

All columns are float64. NaN values appear at the start of the series
(before enough bars exist to calculate the indicator) and are handled
by each signal generator individually.
"""

import numpy as np
import pandas as pd

import ta
import ta.trend
import ta.momentum
import ta.volatility

from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Main calculation function ─────────────────────────────────────────────────

def calculate_indicators(
    df: pd.DataFrame,
    config: dict,
    market: str = "UNKNOWN",
) -> pd.DataFrame:
    """
    Calculate all technical indicators and add them as new columns.

    Reads all parameters from the config dict (which comes from config.yaml).
    Does not modify the input DataFrame — returns a copy with new columns.

    Args:
        df:     Clean OHLCV DataFrame (output of data_cleaner.clean_market_data)
        config: Strategy config dict. Required keys:
                  ema_fast, ema_medium, ema_slow     (TMA periods)
                  atr_period                          (ATR period)
                  rsi_period                          (RSI period)
                  adx_period                          (ADX period)
                  entry_period, exit_period           (Donchian periods)
        market: Market code for logging

    Returns:
        DataFrame with all original columns PLUS indicator columns:
          ema_fast, ema_medium, ema_slow, atr, atr_pct,
          rsi, adx, di_plus, di_minus,
          donchian_high, donchian_low, donchian_mid,
          donchian_exit_high, donchian_exit_low

    Example:
        from src.utils.data_downloader import download_market
        from src.utils.data_cleaner import clean_market_data
        from src.strategy.indicators import calculate_indicators
        import yaml

        with open("config/config.yaml") as f:
            cfg = yaml.safe_load(f)["strategy"]

        raw   = download_market("ES", "2020-01-01", "2024-12-31")
        clean, _ = clean_market_data(raw, "ES")
        ind   = calculate_indicators(clean, cfg, "ES")
        print(ind[["ema_fast", "ema_medium", "ema_slow", "atr", "rsi", "adx"]].tail())
    """
    if df.empty:
        log.warning("{market}: Empty DataFrame — cannot calculate indicators", market=market)
        return df.copy()

    df = df.copy()

    # ── Extract config parameters ──────────────────────────────────────────────
    ema_fast_period   = int(config.get("ema_fast",     8))
    ema_medium_period = int(config.get("ema_medium",  21))
    ema_slow_period   = int(config.get("ema_slow",    89))
    atr_period        = int(config.get("atr_period",  20))
    rsi_period        = int(config.get("rsi_period",   5))
    adx_period        = int(config.get("adx_period",  14))
    entry_period      = int(config.get("entry_period",55))
    exit_period       = int(config.get("exit_period", 20))

    min_bars = max(ema_slow_period, entry_period, adx_period) + 5
    if len(df) < min_bars:
        log.warning(
            "{market}: Only {n} bars — need at least {req} for all indicators. "
            "Results will have significant NaN padding.",
            market=market, n=len(df), req=min_bars,
        )

    log.info(
        "{market}: Calculating indicators on {n} bars "
        "(EMA {f}/{m}/{s}, ATR {a}, RSI {r}, ADX {adx}, DC {e}/{x})",
        market=market, n=len(df),
        f=ema_fast_period, m=ema_medium_period, s=ema_slow_period,
        a=atr_period, r=rsi_period, adx=adx_period,
        e=entry_period, x=exit_period,
    )

    # ── Triple Moving Average (EMA 8/21/89) ───────────────────────────────────
    df["ema_fast"]   = ta.trend.EMAIndicator(df["Close"], window=ema_fast_period).ema_indicator()
    df["ema_medium"] = ta.trend.EMAIndicator(df["Close"], window=ema_medium_period).ema_indicator()
    df["ema_slow"]   = ta.trend.EMAIndicator(df["Close"], window=ema_slow_period).ema_indicator()

    # ── ATR — Average True Range ───────────────────────────────────────────────
    # Used for: position sizing, stop placement, regime detection
    atr_indicator = ta.volatility.AverageTrueRange(
        high=df["High"], low=df["Low"], close=df["Close"],
        window=atr_period,
    )
    df["atr"] = atr_indicator.average_true_range()

    # ATR as percentage of price (normalizes across different-priced markets)
    # ATR_pct for ES at 5000 and ATR_pct for GC at 2000 are comparable
    df["atr_pct"] = df["atr"] / df["Close"]

    # ── RSI — Relative Strength Index ─────────────────────────────────────────
    # Short period (5) for mean reversion — captures overbought/oversold faster
    df["rsi"] = ta.momentum.RSIIndicator(df["Close"], window=rsi_period).rsi()

    # ── ADX — Average Directional Index ───────────────────────────────────────
    # ADX measures trend strength, NOT direction
    # ADX > 25: trending (trade trend signals)
    # ADX < 20: ranging (trade mean reversion or sit out)
    adx_indicator = ta.trend.ADXIndicator(
        high=df["High"], low=df["Low"], close=df["Close"],
        window=adx_period,
    )
    df["adx"]      = adx_indicator.adx()
    df["di_plus"]  = adx_indicator.adx_pos()   # +DI: upward pressure
    df["di_minus"] = adx_indicator.adx_neg()   # -DI: downward pressure

    # ── Donchian Channel (entry: 55-period) ───────────────────────────────────
    # Classic Turtle Trading System 2 breakout levels
    # Long signal:  Close > 55-bar high (new 55-day high)
    # Short signal: Close < 55-bar low  (new 55-day low)
    df["donchian_high"] = df["High"].rolling(window=entry_period).max()
    df["donchian_low"]  = df["Low"].rolling(window=entry_period).min()
    df["donchian_mid"]  = (df["donchian_high"] + df["donchian_low"]) / 2

    # ── Donchian Channel (exit: 20-period) ────────────────────────────────────
    # Exit long when price crosses below 20-bar low
    # Exit short when price crosses above 20-bar high
    df["donchian_exit_high"] = df["High"].rolling(window=exit_period).max()
    df["donchian_exit_low"]  = df["Low"].rolling(window=exit_period).min()

    # ── Validate output ────────────────────────────────────────────────────────
    nan_counts = df[["ema_fast", "ema_medium", "ema_slow",
                     "atr", "rsi", "adx",
                     "donchian_high", "donchian_low"]].isnull().sum()
    max_nan = nan_counts.max()

    log.info(
        "{market}: Indicators calculated. Max NaN warmup bars: {n} "
        "(expected ~{exp} for slow EMA/Donchian)",
        market=market, n=int(max_nan), exp=max(ema_slow_period, entry_period),
    )

    return df


# ── Rolling ATR baseline (for regime detection) ───────────────────────────────

def add_atr_baseline(
    df: pd.DataFrame,
    atr_col: str  = "atr",
    window: int   = 252,
) -> pd.DataFrame:
    """
    Add a rolling 1-year ATR baseline for regime volatility comparison.

    The regime classifier compares current ATR to this baseline to detect
    HIGH_VOL (ATR > 1.5x baseline) and CRISIS (ATR > 2.5x baseline) regimes.

    Args:
        df:      DataFrame with 'atr' column (output of calculate_indicators)
        atr_col: Name of ATR column to baseline (default 'atr')
        window:  Rolling window in bars (default 252 = 1 trading year)

    Returns:
        DataFrame with 'atr_baseline' and 'atr_ratio' columns added.
    """
    df = df.copy()
    df["atr_baseline"] = df[atr_col].rolling(window=window, min_periods=20).mean()
    df["atr_ratio"]    = df[atr_col] / df["atr_baseline"]
    return df
