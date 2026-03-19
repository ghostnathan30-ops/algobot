"""
AlgoBot — Data Downloader
==========================
Module:  src/utils/data_downloader.py
Phase:   1 — Data Infrastructure
Purpose: Downloads and caches historical price data for all 6 markets
         and macro data from FRED. Single entry point for all data acquisition.

Data source strategy:
  Primary source (free, used now):
    Yahoo Finance via yfinance — ETF proxies and available futures data.
    Coverage: 20–25 years depending on instrument.

  Upgrade path (when moving to full futures backtesting):
    QuantConnect Lean — true continuous futures contracts, 25+ years.
    Norgate Data — best retail quality, $270/year.
    These are NOT used here. This module handles free sources only.

Market ticker mapping (Yahoo Finance):
  ES  → SPY   (S&P 500 ETF, 1993+)       or  ^GSPC (index, 1993+)
  NQ  → QQQ   (Nasdaq-100 ETF, 1999+)    or  ^NDX  (index, 1985+)
  GC  → GC=F  (Gold futures, yfinance)   or  GLD   (ETF, 2004+)
  CL  → CL=F  (Crude Oil futures)        or  USO   (ETF, 2006+)
  ZB  → TLT   (20+yr Bond ETF, 2002+)   or  ZB=F  (futures)
  6E  → EURUSD=X (Forex pair, 2000+)

Note on ETF proxies vs futures:
  ETF proxies are not dollar-equivalent to futures contracts.
  They are used only for SIGNAL validation (does the trend signal work?)
  Position sizing is calculated separately using actual contract specs
  from config.yaml. When going live, QuantConnect provides true futures data.

Caching:
  All downloaded data is saved to data/raw/<symbol>_<start>_<end>.parquet
  On subsequent calls, cache is used unless force_refresh=True.
  Cache avoids hammering Yahoo Finance API and speeds up development.
"""

import os
import time
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from src.utils.logger import get_logger

log = get_logger(__name__)

# ── Load environment variables ────────────────────────────────────────────────
load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent.parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Market ticker configuration ───────────────────────────────────────────────
# Primary: Most reliable source for 20+ year history
# Futures: Direct futures contract (shorter history, but correct price scale)
# Notes: History from when the ETF/contract started trading

MARKET_CONFIG = {
    "ES": {
        "primary":  "SPY",       # S&P 500 ETF — 1993 to present
        "index":    "^GSPC",     # S&P 500 Index — 1927 to present
        "futures":  "ES=F",      # ES futures (yfinance, limited history)
        "note":     "Use SPY as primary. Scale: 1 SPY ≈ 0.1 × ES contract value.",
    },
    "NQ": {
        "primary":  "QQQ",       # Nasdaq-100 ETF — 1999 to present
        "index":    "^NDX",      # Nasdaq-100 Index — 1985 to present
        "futures":  "NQ=F",      # NQ futures (limited history)
        "note":     "Use QQQ as primary. Scale differs from NQ contract.",
    },
    "GC": {
        "primary":  "GC=F",      # Gold futures from yfinance — good history
        "etf":      "GLD",       # Gold ETF — 2004 to present (shorter)
        "futures":  "GC=F",
        "note":     "GC=F from yfinance has good 20+ year history.",
    },
    "CL": {
        "primary":  "CL=F",      # Crude Oil futures from yfinance
        "etf":      "USO",       # Oil ETF — 2006 only (too short)
        "futures":  "CL=F",
        "note":     "CL=F has good history. USO only from 2006, avoid.",
    },
    "ZB": {
        "primary":  "TLT",       # 20+ Year Treasury Bond ETF — 2002 to present
        "futures":  "ZB=F",      # 30-year bond futures
        "note":     "TLT used as proxy. ZB=F from yfinance has limited history.",
    },
    "6E": {
        "primary":  "EURUSD=X",  # Euro/USD forex — 2000 to present
        "futures":  "6E=F",      # Euro FX futures (limited yfinance history)
        "note":     "EURUSD=X has good 20+ year history.",
    },
    "RTY": {
        "primary":  "IWM",       # iShares Russell 2000 ETF -- 2000 to present
        "futures":  "RTY=F",     # E-mini Russell 2000 futures
        "note":     "IWM is the standard Russell 2000 proxy. $50/point futures.",
    },
    "YM": {
        "primary":  "DIA",       # SPDR Dow Jones ETF -- 1998 to present
        "futures":  "YM=F",      # E-mini Dow Jones futures
        "note":     "DIA is the standard Dow Jones proxy. $5/point futures.",
    },
}

# FRED macro series used for regime detection and research
FRED_SERIES = {
    "VIX":          "VIXCLS",       # CBOE Volatility Index — 1990+
    "FED_FUNDS":    "FEDFUNDS",     # Federal Funds Rate — 1954+
    "YIELD_10Y":    "DGS10",        # 10-Year Treasury Yield — 1962+
    "YIELD_2Y":     "DGS2",         # 2-Year Treasury Yield — 1976+
    "CPI":          "CPIAUCSL",     # Consumer Price Index — 1947+
    "YIELD_CURVE":  "T10Y2Y",       # 10Y-2Y Spread (recession indicator) — 1976+
}


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _cache_path(symbol: str, start: str, end: str) -> Path:
    """Build the cache file path for a given symbol and date range."""
    safe_symbol = symbol.replace("=", "_").replace("^", "").replace("/", "_")
    return RAW_DATA_DIR / f"{safe_symbol}_{start}_{end}.parquet"


def _load_from_cache(cache_file: Path) -> pd.DataFrame | None:
    """Load a DataFrame from parquet cache. Returns None if cache miss."""
    if cache_file.exists():
        log.debug("Cache hit: {f}", f=cache_file.name)
        return pd.read_parquet(cache_file)
    return None


def _save_to_cache(df: pd.DataFrame, cache_file: Path) -> None:
    """Save a DataFrame to parquet cache."""
    df.to_parquet(cache_file)
    log.debug("Cached to: {f}", f=cache_file.name)


# ── Core download functions ───────────────────────────────────────────────────

def download_yahoo(
    ticker: str,
    start: str,
    end: str,
    use_cache: bool = True,
    force_refresh: bool = False,
    max_retries: int = 3,
) -> pd.DataFrame:
    """
    Download OHLCV data from Yahoo Finance for a single ticker.

    Downloads daily bars. Columns returned: Open, High, Low, Close, Volume.
    Adjusted Close is included as 'Adj Close' when available (ETFs/stocks).
    For futures and forex, only unadjusted OHLCV is available.

    Args:
        ticker:        Yahoo Finance ticker symbol (e.g., "SPY", "GC=F", "EURUSD=X")
        start:         Start date string "YYYY-MM-DD"
        end:           End date string "YYYY-MM-DD"
        use_cache:     Return cached data if available (default True)
        force_refresh: Ignore cache and re-download (default False)
        max_retries:   Number of download attempts on failure (default 3)

    Returns:
        DataFrame with DatetimeIndex and columns: Open, High, Low, Close, Volume
        Returns empty DataFrame on persistent failure (logged as error).

    Example:
        df = download_yahoo("SPY", "2000-01-01", "2024-12-31")
        print(f"Downloaded {len(df)} bars")
    """
    cache_file = _cache_path(ticker, start, end)

    if use_cache and not force_refresh:
        cached = _load_from_cache(cache_file)
        if cached is not None:
            return cached

    log.info("Downloading {ticker} from Yahoo Finance ({start} to {end})",
             ticker=ticker, start=start, end=end)

    for attempt in range(1, max_retries + 1):
        try:
            raw = yf.download(
                ticker,
                start=start,
                end=end,
                auto_adjust=True,   # Adjust for splits and dividends
                progress=False,
                timeout=30,
            )

            if raw.empty:
                log.warning("No data returned for {ticker} (attempt {a}/{n})",
                            ticker=ticker, a=attempt, n=max_retries)
                if attempt < max_retries:
                    time.sleep(2 ** attempt)  # Exponential backoff
                continue

            # Flatten MultiIndex columns if present (yfinance >= 0.2 can return these)
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)

            # Standardize column names
            raw.columns = [c.title() for c in raw.columns]

            # Keep only OHLCV columns
            keep_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"]
                         if c in raw.columns]
            df = raw[keep_cols].copy()

            # Ensure DatetimeIndex with UTC timezone removed (date only)
            df.index = pd.to_datetime(df.index).tz_localize(None)
            df.index.name = "Date"

            # Drop rows where all price columns are NaN
            df.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)

            log.info("Downloaded {n} bars for {ticker} ({start} to {end})",
                     n=len(df), ticker=ticker, start=start, end=end)

            # Cache save is separate from download — a cache failure must NEVER
            # trigger a retry of the download itself.
            if use_cache:
                try:
                    _save_to_cache(df, cache_file)
                except Exception as cache_err:
                    log.warning("Cache save failed for {ticker} (data still usable): {err}",
                                ticker=ticker, err=str(cache_err))

            return df  # Always return the df even if caching failed

        except Exception as e:
            log.error("Yahoo download attempt {a}/{n} failed for {ticker}: {err}",
                      a=attempt, n=max_retries, ticker=ticker, err=str(e))
            if attempt < max_retries:
                time.sleep(2 ** attempt)

    log.error("All {n} attempts failed for {ticker}. Returning empty DataFrame.",
              n=max_retries, ticker=ticker)
    return pd.DataFrame()


def download_fred(
    series_id: str,
    start: str,
    end: str,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> pd.Series:
    """
    Download a macro data series from the FRED API.

    Requires FRED_API_KEY to be set in .env file.
    Get a free key at: https://fred.stlouisfed.org/docs/api/api_key.html

    Args:
        series_id:  FRED series identifier (e.g., "VIXCLS", "FEDFUNDS")
        start:      Start date string "YYYY-MM-DD"
        end:        End date string "YYYY-MM-DD"
        use_cache:  Return cached data if available
        force_refresh: Ignore cache

    Returns:
        Pandas Series with DatetimeIndex. Returns empty Series if API key
        is not configured or download fails — does not crash the pipeline.

    Example:
        vix = download_fred("VIXCLS", "2000-01-01", "2024-12-31")
        print(f"VIX data: {len(vix)} observations")
    """
    api_key = os.environ.get("FRED_API_KEY", "")

    if not api_key or api_key == "your_fred_api_key_here":
        log.warning(
            "FRED_API_KEY not configured in .env — skipping FRED download for {sid}. "
            "Get a free key at: https://fred.stlouisfed.org/docs/api/api_key.html",
            sid=series_id
        )
        return pd.Series(dtype=float, name=series_id)

    cache_file = _cache_path(f"FRED_{series_id}", start, end)

    if use_cache and not force_refresh:
        cached = _load_from_cache(cache_file)
        if cached is not None:
            # Cache is a DataFrame, return as Series
            return cached.iloc[:, 0]

    log.info("Downloading FRED series: {sid} ({start} to {end})",
             sid=series_id, start=start, end=end)

    try:
        from fredapi import Fred
        fred = Fred(api_key=api_key)
        series = fred.get_series(
            series_id,
            observation_start=start,
            observation_end=end
        )
        series.name = series_id
        series.index = pd.to_datetime(series.index).tz_localize(None)

        log.info("Downloaded {n} observations for FRED {sid}",
                 n=len(series), sid=series_id)

        if use_cache:
            _save_to_cache(series.to_frame(), cache_file)

        return series

    except Exception as e:
        log.error("FRED download failed for {sid}: {err}", sid=series_id, err=str(e))
        return pd.Series(dtype=float, name=series_id)


# ── Market-level download ─────────────────────────────────────────────────────

def download_market(
    market: str,
    start: str,
    end: str,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Download price data for a single AlgoBot market (ES, NQ, GC, CL, ZB, 6E).

    Uses the primary source defined in MARKET_CONFIG. Falls back to the
    secondary source if the primary returns insufficient data.

    Args:
        market:  Market code ("ES", "NQ", "GC", "CL", "ZB", "6E")
        start:   Start date "YYYY-MM-DD"
        end:     End date "YYYY-MM-DD"
        use_cache: Use cached data if available
        force_refresh: Force re-download

    Returns:
        DataFrame with OHLCV data. Has an additional 'market' column.
        Returns empty DataFrame if all sources fail.

    Example:
        es_data = download_market("ES", "2000-01-01", "2024-12-31")
        print(f"ES bars: {len(es_data)}")
    """
    if market not in MARKET_CONFIG:
        log.error("Unknown market: {m}. Must be one of: {valid}",
                  m=market, valid=list(MARKET_CONFIG.keys()))
        return pd.DataFrame()

    config  = MARKET_CONFIG[market]
    primary = config["primary"]

    log.info("Fetching {market} data using primary source: {src}",
             market=market, src=primary)

    df = download_yahoo(primary, start, end, use_cache, force_refresh)

    # Fallback: if primary returns less than 1 year of data, try index
    if len(df) < 250 and "index" in config:
        log.warning("{market} primary source ({src}) returned only {n} bars. "
                    "Trying index source: {idx}",
                    market=market, src=primary, n=len(df), idx=config["index"])
        df = download_yahoo(config["index"], start, end, use_cache, force_refresh)

    if df.empty:
        log.error("All sources failed for {market}", market=market)
        return pd.DataFrame()

    # Tag data with market name
    df["market"] = market
    log.info("{market}: {n} bars | {start} to {end}",
             market=market, n=len(df),
             start=df.index[0].strftime("%Y-%m-%d"),
             end=df.index[-1].strftime("%Y-%m-%d"))

    return df


def download_all_markets(
    start: str = "2000-01-01",
    end: str   = "2024-12-31",
    markets:   list[str] = None,
    use_cache: bool = True,
    force_refresh: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    Download price data for all 6 AlgoBot markets in one call.

    This is the main entry point called by the backtesting engine.
    Downloads sequentially with 1-second pause between requests
    to be respectful of Yahoo Finance rate limits.

    Args:
        start:         Start date "YYYY-MM-DD" (default: "2000-01-01")
        end:           End date "YYYY-MM-DD" (default: "2024-12-31")
        markets:       List of market codes. Default: all 6 markets.
        use_cache:     Use cached data if available (default True)
        force_refresh: Force re-download of all data (default False)

    Returns:
        Dictionary mapping market code → DataFrame.
        e.g., {"ES": df_es, "NQ": df_nq, "GC": df_gc, ...}
        Failed markets are excluded from the dict (logged as errors).

    Example:
        all_data = download_all_markets("2000-01-01", "2024-12-31")
        print(f"Markets downloaded: {list(all_data.keys())}")
        print(f"ES bars: {len(all_data['ES'])}")
    """
    if markets is None:
        markets = list(MARKET_CONFIG.keys())  # All 6 by default

    log.info("Starting download for {n} markets: {mkts}",
             n=len(markets), mkts=markets)

    results = {}
    failed  = []

    for i, market in enumerate(markets):
        df = download_market(market, start, end, use_cache, force_refresh)

        if df.empty:
            failed.append(market)
        else:
            results[market] = df

        # Rate limit courtesy pause between requests (skip after last)
        if i < len(markets) - 1:
            time.sleep(1.0)

    # Summary
    log.info("Download complete. Success: {s} | Failed: {f}",
             s=list(results.keys()), f=failed)

    if failed:
        log.warning("Failed markets will be excluded from backtesting: {f}",
                    f=failed)

    return results


def download_macro_data(
    start: str = "2000-01-01",
    end: str   = "2024-12-31",
    use_cache: bool = True,
) -> dict[str, pd.Series]:
    """
    Download all macro data series from FRED.

    Downloads VIX, Fed Funds Rate, yield curve, and CPI.
    These are used by the regime classifier and as research context.
    Requires FRED_API_KEY in .env. Skips gracefully if key not configured.

    Args:
        start:     Start date "YYYY-MM-DD"
        end:       End date "YYYY-MM-DD"
        use_cache: Use cached data if available

    Returns:
        Dictionary mapping short name → Series.
        e.g., {"VIX": series_vix, "FED_FUNDS": series_ff, ...}
        Missing series (API key not set, download failed) excluded from dict.

    Example:
        macro = download_macro_data("2000-01-01", "2024-12-31")
        if "VIX" in macro:
            print(f"Average VIX: {macro['VIX'].mean():.1f}")
    """
    log.info("Downloading macro data from FRED ({start} to {end})",
             start=start, end=end)

    results = {}
    for name, series_id in FRED_SERIES.items():
        series = download_fred(series_id, start, end, use_cache)
        if not series.empty:
            results[name] = series

    log.info("Macro data downloaded: {names}", names=list(results.keys()))
    return results


# ── Data summary utilities ────────────────────────────────────────────────────

def summarize_data(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Generate a summary table of downloaded data quality.

    For each market, shows: bar count, date range, NaN count, and
    whether the data covers the full backtesting period requested.

    Args:
        data: Dictionary of market → DataFrame (from download_all_markets)

    Returns:
        DataFrame summary table. Printed in lab reports.

    Example:
        summary = summarize_data(all_data)
        print(summary.to_string())
    """
    rows = []
    for market, df in data.items():
        if df.empty:
            continue
        nan_count = df[["Open", "High", "Low", "Close"]].isnull().sum().sum()
        rows.append({
            "Market":     market,
            "Source":     MARKET_CONFIG[market]["primary"],
            "Bars":       len(df),
            "Start":      df.index[0].strftime("%Y-%m-%d"),
            "End":        df.index[-1].strftime("%Y-%m-%d"),
            "NaN_prices": int(nan_count),
            "Years":      round(len(df) / 252, 1),
        })

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows).set_index("Market")
