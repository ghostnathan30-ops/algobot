"""
AlgoBot — Yahoo Finance Intraday Downloader
============================================
Module:  src/utils/yf_intraday.py
Phase:   5B — ORB Backtest
Purpose: Downloads recent intraday OHLCV data from Yahoo Finance for
         CME futures and ETF proxies. No API key required.

Yahoo Finance intraday limits (as of 2026):
  1-minute bars:  last 7 calendar days
  5-minute bars:  last 60 calendar days   <-- PRIMARY for ORB backtest
  15-minute bars: last 60 calendar days
  30-minute bars: last 60 calendar days
  1-hour bars:    last 730 calendar days (2 years)

Why use Yahoo Finance for intraday?
  It's free, requires no API key, and provides actual CME futures tickers
  (ES=F, NQ=F, GC=F, CL=F, ZB=F) — not just ETF proxies. This means
  price levels are real futures prices, which is correct for backtesting.

  Limitation: Only ~60 days of 5-min history. This is a proof-of-concept
  window. For multi-year intraday backtesting, a paid data source is needed
  (Rithmic via Topstep, or Kinetick via NinjaTrader).

Coverage:
  ES=F  -> E-mini S&P 500 Futures      5-min, 60 days
  NQ=F  -> E-mini Nasdaq-100 Futures   5-min, 60 days
  GC=F  -> Gold Futures                5-min, 60 days
  CL=F  -> Crude Oil Futures           5-min, 60 days
  ZB=F  -> 30-Year T-Bond Futures      5-min, 60 days (sometimes patchy)
  6E=F  -> Euro FX Futures             5-min, 60 days (yfinance uses EURUSD=X)

Usage:
    from src.utils.yf_intraday import download_intraday, load_rth_intraday

    # Download 5-min ES data (last 60 days)
    df = download_intraday("ES", interval="5m")
    print(f"Downloaded {len(df)} 5-min bars")

    # RTH-only 5-min ES (9:30 AM - 4:00 PM ET)
    df_rth = load_rth_intraday("ES", interval="5m")
    print(f"RTH bars: {len(df_rth)}")
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

from src.utils.logger import get_logger

log = get_logger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.parent
INTRADAY_DIR = PROJECT_ROOT / "data" / "raw" / "intraday"
INTRADAY_DIR.mkdir(parents=True, exist_ok=True)

# ── Ticker mapping: AlgoBot market code -> Yahoo Finance symbol ───────────────
YF_INTRADAY_TICKERS = {
    "ES":  "ES=F",       # E-mini S&P 500 futures
    "NQ":  "NQ=F",       # E-mini Nasdaq-100 futures
    "GC":  "GC=F",       # Gold futures
    "CL":  "CL=F",       # Crude Oil futures
    "ZB":  "ZB=F",       # 30-Year T-Bond futures
    "6E":  "EURUSD=X",   # Euro/USD (best forex proxy available on yfinance)
}

# ── Max lookback by interval (Yahoo Finance limits) ───────────────────────────
YF_INTERVAL_MAX_PERIOD = {
    "1m":  "7d",    # 7 calendar days
    "2m":  "60d",   # 60 calendar days
    "5m":  "60d",   # 60 calendar days  <- primary for ORB
    "15m": "60d",   # 60 calendar days
    "30m": "60d",   # 60 calendar days
    "60m": "730d",  # 2 years
    "1h":  "730d",  # 2 years
    "1d":  "max",   # full history
}

# Regular Trading Hours for CME equity index futures (ET)
RTH_START = "09:30"
RTH_END   = "16:00"


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _cache_path(market: str, interval: str) -> Path:
    """Cache file path for a given market and interval."""
    return INTRADAY_DIR / f"yf_{market}_{interval}_recent.parquet"


def _cache_is_fresh(cache_file: Path, max_age_hours: float = 4.0) -> bool:
    """Return True if cache file exists and is less than max_age_hours old."""
    if not cache_file.exists():
        return False
    age_hours = (time.time() - cache_file.stat().st_mtime) / 3600.0
    return age_hours < max_age_hours


# ── Core download ──────────────────────────────────────────────────────────────

def download_intraday(
    market: str,
    interval: str = "5m",
    use_cache: bool = True,
    force_refresh: bool = False,
    max_cache_age_hours: float = 4.0,
) -> pd.DataFrame:
    """
    Download intraday OHLCV data from Yahoo Finance for a single market.

    Uses the maximum available lookback period for the given interval.
    Results are cached to Parquet. Cache is considered fresh for 4 hours
    (intraday data changes during the trading day).

    Args:
        market:              AlgoBot market code ("ES", "NQ", "GC", "CL", "ZB", "6E")
        interval:            Bar size: "1m", "5m", "15m", "30m", "1h"
        use_cache:           Return cached data if fresh (default True)
        force_refresh:       Force re-download regardless of cache age
        max_cache_age_hours: How old the cache can be before refreshing

    Returns:
        DataFrame with DatetimeIndex (timezone-aware, localized to ET) and
        columns: Open, High, Low, Close, Volume.
        Returns empty DataFrame if download fails.

    Example:
        df = download_intraday("ES", interval="5m")
        print(f"ES 5-min bars: {len(df)}")
        print(f"Date range: {df.index[0]} to {df.index[-1]}")
    """
    if market not in YF_INTRADAY_TICKERS:
        log.error("Unknown market '{m}'. Valid: {v}", m=market, v=list(YF_INTRADAY_TICKERS.keys()))
        return pd.DataFrame()

    ticker = YF_INTRADAY_TICKERS[market]
    period = YF_INTERVAL_MAX_PERIOD.get(interval, "60d")
    cache_file = _cache_path(market, interval)

    # ── Cache check ───────────────────────────────────────────────────────────
    if use_cache and not force_refresh and _cache_is_fresh(cache_file, max_cache_age_hours):
        log.debug("{market} {interval}: Loading from fresh cache", market=market, interval=interval)
        return pd.read_parquet(cache_file)

    log.info("{market}: Downloading {interval} intraday from Yahoo Finance (period={p})",
             market=market, interval=interval, p=period)

    try:
        raw = yf.download(
            ticker,
            period=period,
            interval=interval,
            auto_adjust=True,
            progress=False,
            timeout=30,
        )

        if raw is None or raw.empty:
            log.warning("{market}: Yahoo Finance returned empty DataFrame for {interval}",
                        market=market, interval=interval)
            return pd.DataFrame()

        # Flatten MultiIndex columns (yfinance >= 0.2 wraps in MultiIndex)
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        raw.columns = [c.title() for c in raw.columns]

        keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in raw.columns]
        df = raw[keep].copy()

        # Ensure float types
        for col in ["Open", "High", "Low", "Close"]:
            if col in df.columns:
                df[col] = df[col].astype(float)
        if "Volume" in df.columns:
            df["Volume"] = df["Volume"].astype(float)

        # Drop rows with all NaN prices
        df.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)

        # Convert timezone to US/Eastern for RTH filtering
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("America/New_York")
        else:
            df.index = df.index.tz_convert("America/New_York")

        df.index.name = "Timestamp"

        log.info(
            "{market}: Downloaded {n} {interval} bars | {start} to {end}",
            market=market, interval=interval, n=len(df),
            start=df.index[0].strftime("%Y-%m-%d %H:%M"),
            end=df.index[-1].strftime("%Y-%m-%d %H:%M"),
        )

        # Cache result
        if use_cache:
            try:
                df.to_parquet(cache_file)
                log.debug("{market} {interval}: Cached {n} bars", market=market, interval=interval, n=len(df))
            except Exception as e:
                log.warning("Cache save failed: {err}", err=str(e))

        return df

    except Exception as e:
        log.error("{market}: Yahoo Finance intraday download failed: {err}", market=market, err=str(e))
        return pd.DataFrame()


# ── RTH filter ─────────────────────────────────────────────────────────────────

def filter_rth(df: pd.DataFrame, market: str = "ES") -> pd.DataFrame:
    """
    Filter an intraday DataFrame to Regular Trading Hours only.

    For ES and NQ (equity index futures): 9:30 AM - 4:00 PM ET.
    For GC, CL, ZB (commodity/bond futures): 8:30 AM - 3:15 PM ET (approx).
    For 6E (forex): 24-hour market, keep 8:00 AM - 5:00 PM ET for US session.

    Args:
        df:     Intraday DataFrame with timezone-aware DatetimeIndex (ET).
        market: Market code to determine appropriate RTH window.

    Returns:
        Filtered DataFrame with only RTH bars.
    """
    if df.empty:
        return df

    # Ensure ET timezone
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC").tz_convert("America/New_York")
    elif str(df.index.tz) != "America/New_York":
        df.index = df.index.tz_convert("America/New_York")

    rth_windows = {
        "ES": ("09:30", "16:00"),
        "NQ": ("09:30", "16:00"),
        "GC": ("08:20", "13:30"),  # COMEX gold RTH
        "CL": ("09:00", "14:30"),  # NYMEX crude RTH
        "ZB": ("08:30", "15:00"),  # CBOT bond RTH
        "6E": ("08:00", "17:00"),  # CME FX primary session
    }

    start_t, end_t = rth_windows.get(market.upper(), ("09:30", "16:00"))

    filtered = df.between_time(start_t, end_t)
    log.debug("{market}: RTH filter {s}-{e}: {n} bars (from {total})",
              market=market, s=start_t, e=end_t, n=len(filtered), total=len(df))
    return filtered


# ── Convenience loader ─────────────────────────────────────────────────────────

def load_rth_intraday(
    market: str,
    interval: str = "5m",
    use_cache: bool = True,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Download intraday data and apply RTH filter in one call.

    This is the primary function used by the ORB backtest.

    Args:
        market:        Market code ("ES", "NQ", etc.)
        interval:      Bar size ("5m", "15m", "1h", etc.)
        use_cache:     Use cached data if fresh
        force_refresh: Force re-download

    Returns:
        RTH-filtered intraday DataFrame.

    Example:
        df = load_rth_intraday("ES", "5m")
        # Now df contains only 9:30 AM - 4:00 PM ET bars
        print(df.between_time("09:30", "10:00").head(6))  # Opening range bars
    """
    df = download_intraday(market, interval, use_cache, force_refresh)
    if df.empty:
        return df
    return filter_rth(df, market)


# ── Multi-market download ──────────────────────────────────────────────────────

def download_all_intraday(
    markets: list[str] = None,
    interval: str = "5m",
    use_cache: bool = True,
    force_refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    Download intraday data for multiple markets.

    Args:
        markets:       List of market codes. Default: ["ES", "NQ"]
                       (Only these two are used for intraday ORB)
        interval:      Bar size ("5m", "15m", etc.)
        use_cache:     Use cached data if fresh
        force_refresh: Force re-download

    Returns:
        dict mapping market code -> RTH-filtered intraday DataFrame.
        Failed markets excluded from result.

    Example:
        data = download_all_intraday(["ES", "NQ"], "5m")
        print(f"ES bars: {len(data['ES'])}")
        print(f"NQ bars: {len(data['NQ'])}")
    """
    if markets is None:
        markets = ["ES", "NQ"]

    results = {}
    for i, market in enumerate(markets):
        df = load_rth_intraday(market, interval, use_cache, force_refresh)
        if not df.empty:
            results[market] = df
        else:
            log.warning("{market}: Failed to download {interval} intraday data", market=market, interval=interval)

        if i < len(markets) - 1:
            time.sleep(0.5)  # Rate limit courtesy

    log.info("Intraday download complete: {markets} at {interval}",
             markets=list(results.keys()), interval=interval)
    return results


# ── Data quality summary ───────────────────────────────────────────────────────

def summarize_intraday(data: dict[str, pd.DataFrame]) -> None:
    """Print a quick summary of downloaded intraday data quality."""
    if not data:
        print("No intraday data available.")
        return

    print(f"\n{'Market':<8} {'Interval':<10} {'Bars':<8} {'Start':<22} {'End':<22} {'Days':<6}")
    print("-" * 76)
    for market, df in data.items():
        if df.empty:
            print(f"{market:<8} {'--':<10} {'0':<8} {'empty':<22} {'empty':<22} {'0':<6}")
            continue
        n_days = df.index.normalize().nunique()
        print(
            f"{market:<8} {'5m':<10} {len(df):<8} "
            f"{str(df.index[0].strftime('%Y-%m-%d %H:%M')):<22} "
            f"{str(df.index[-1].strftime('%Y-%m-%d %H:%M')):<22} "
            f"{n_days:<6}"
        )
    print()
