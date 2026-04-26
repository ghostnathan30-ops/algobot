"""
AlgoBot — TradingView / yfinance Data Feed
============================================
Module:  src/execution/tv_data_feed.py
Purpose: Provides real-time (cached) price data and OHLCV bars via yfinance.
         Used by PaperSimulator to:
           - Monitor open positions (stop/target checks every 60 seconds)
           - Compute last price for dashboard position display
           - Load intraday bars for strategy reference

         This replaces IBKR's reqHistoricalData() + reqMktData() for the
         tv_paper trading mode. No IBKR connection required.

Mapping:
    ES  → ES=F   (S&P 500 E-mini continuous contract)
    NQ  → NQ=F   (Nasdaq-100 E-mini continuous contract)
    GC  → GC=F   (Gold futures continuous contract)
    CL  → CL=F   (WTI Crude Oil continuous contract)
    6E  → EURUSD=X
    RTY → RTY=F
    YM  → YM=F

Usage:
    feed = TVDataFeed()
    price = feed.get_last_price("ES")   # returns float e.g. 5820.25
    bars  = feed.get_intraday_bars("NQ", interval="5m", days=2)
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import pytz
import yfinance as yf

from src.utils.logger import get_logger

log = get_logger(__name__)

ET = pytz.timezone("America/New_York")

# ── Ticker mapping ─────────────────────────────────────────────────────────────

MARKET_TO_TICKER: dict[str, str] = {
    "ES":  "ES=F",
    "NQ":  "NQ=F",
    "GC":  "GC=F",
    "CL":  "CL=F",
    "6E":  "EURUSD=X",
    "RTY": "RTY=F",
    "YM":  "YM=F",
    "ZB":  "ZB=F",
    # Micros — map to same underlying futures
    "MES": "ES=F",
    "MNQ": "NQ=F",
    "MGC": "GC=F",
    "MCL": "CL=F",
}

# Cache TTL — don't re-fetch the same ticker more than once per minute
_CACHE_TTL_S = 60


class TVDataFeed:
    """
    yfinance-backed price feed with per-market caching.

    Thread-safe: get_last_price() and get_intraday_bars() can be called
    from both the asyncio monitor task and the main thread.
    """

    def __init__(self, cache_ttl_s: int = _CACHE_TTL_S) -> None:
        self._ttl    = cache_ttl_s
        self._cache: dict[str, tuple[float, datetime]] = {}  # market → (price, fetched_at)
        self._lock   = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_last_price(self, market: str) -> Optional[float]:
        """
        Return the most recent price for the given market.

        Uses a 60-second cache to avoid hammering yfinance during the
        position monitor loop (which calls this for each open position).

        Returns None if the feed is unavailable (market closed, API error).
        """
        ticker = MARKET_TO_TICKER.get(market.upper())
        if ticker is None:
            log.warning("TVDataFeed: unknown market {m}", m=market)
            return None

        with self._lock:
            cached = self._cache.get(market)
            if cached is not None:
                price, fetched_at = cached
                if (datetime.utcnow() - fetched_at).total_seconds() < self._ttl:
                    return price

        price = self._fetch_price(ticker)
        if price is not None:
            with self._lock:
                self._cache[market] = (price, datetime.utcnow())
        return price

    def get_intraday_bars(
        self,
        market: str,
        interval: str = "5m",
        days: int = 2,
    ) -> pd.DataFrame:
        """
        Return OHLCV bars for the given market.

        Args:
            market:   AlgoBot market code (ES, NQ, GC, CL …)
            interval: yfinance interval string ("1m", "5m", "15m", "1h", "1d")
            days:     Number of calendar days of history to fetch (max 60 for 1m/5m)

        Returns:
            DataFrame with columns [Open, High, Low, Close, Volume],
            DatetimeIndex in US/Eastern timezone.
            Empty DataFrame on failure.
        """
        ticker = MARKET_TO_TICKER.get(market.upper())
        if ticker is None:
            log.warning("TVDataFeed.get_intraday_bars: unknown market {m}", m=market)
            return pd.DataFrame()

        try:
            period = f"{days}d"
            df = yf.download(
                ticker,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=True,
            )
            if df.empty:
                return pd.DataFrame()

            # Flatten multi-level columns that yfinance sometimes returns
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()

            # Ensure index is timezone-aware in ET
            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            df.index = df.index.tz_convert(ET)

            return df.dropna()

        except Exception as e:
            log.warning("TVDataFeed.get_intraday_bars failed for {m}: {e}", m=market, e=e)
            return pd.DataFrame()

    def invalidate(self, market: str) -> None:
        """Remove a market's cached price (forces a fresh fetch on next call)."""
        with self._lock:
            self._cache.pop(market, None)

    def invalidate_all(self) -> None:
        """Clear the entire price cache."""
        with self._lock:
            self._cache.clear()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _fetch_price(self, ticker: str) -> Optional[float]:
        """
        Fetch the most recent price for a yfinance ticker.

        Strategy:
          1. fast_info.last_price  — near-real-time, <0.3s (yfinance 0.2+)
          2. fast_info.previous_close  — fallback when market is closed
          3. 1-minute bar download — last resort

        Returns None on any failure.
        """
        try:
            t = yf.Ticker(ticker)
            fi = t.fast_info
            price = getattr(fi, "last_price", None)
            if price and float(price) > 0:
                return float(price)

            # Fallback: previous close (used outside RTH)
            price = getattr(fi, "previous_close", None)
            if price and float(price) > 0:
                log.debug("TVDataFeed: {t} using previous_close fallback", t=ticker)
                return float(price)

        except Exception as e:
            log.debug("TVDataFeed.fast_info failed for {t}: {e}", t=ticker, e=e)

        # Last resort: download last 1-minute bar
        try:
            df = yf.download(ticker, period="1d", interval="1m", progress=False, auto_adjust=True)
            if not df.empty:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                price = float(df["Close"].iloc[-1])
                if price > 0:
                    return price
        except Exception as e:
            log.warning("TVDataFeed._fetch_price: all methods failed for {t}: {e}", t=ticker, e=e)

        return None
