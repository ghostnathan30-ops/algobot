"""
AlgoBot — QuantConnect Data Downloader
========================================
Module:  src/utils/qc_downloader.py
Phase:   5 — MTF Architecture
Purpose: Downloads historical intraday futures data from QuantConnect's
         REST API and caches it locally as Parquet files.

Why QuantConnect?
  Yahoo Finance provides reliable daily bars but has no historical intraday
  futures data. QuantConnect has 5+ years of 1-minute futures data for
  ES, NQ, GC, CL, ZB, and 6E — exactly what the ORB intraday strategy needs.

Authentication:
  QuantConnect uses HTTP Basic Auth.
  Credentials stored in .env (never in code):
    QC_USER_ID    = your numeric user ID  (from QC > Account Settings)
    QC_API_TOKEN  = your API token        (from QC > Account Settings > API Access)

  To generate your API token:
    1. Log in to quantconnect.com
    2. Go to Account (top right) > My Account
    3. Scroll to "API Access" section
    4. Click "Create API Token" — copy it immediately (shown once)
    5. Add to AlgoBot/.env:
         QC_USER_ID=12345
         QC_API_TOKEN=abcdef1234567890...

Data format:
  QuantConnect stores intraday futures data as per-resolution CSV files.
  We download, decompress, and convert to Parquet for fast local access.
  Stored in: data/raw/intraday/<MARKET>_<resolution>_<start>_<end>.parquet

Resolutions:
  "minute" = 1-minute bars (most granular, largest files)
  "hour"   = 1-hour bars
  "daily"  = daily bars (use Yahoo for daily instead — more history)

Supported markets (QuantConnect tickers):
  ES -> ES (CME E-mini S&P 500 Futures)
  NQ -> NQ (CME E-mini Nasdaq-100 Futures)
  GC -> GC (COMEX Gold Futures)

Usage:
    from src.utils.qc_downloader import download_qc_intraday, load_intraday

    # Download 5 years of 1-minute ES data
    df = download_qc_intraday(
        market="ES",
        resolution="minute",
        start="2020-01-01",
        end="2024-12-31",
    )

    # Resample to 5-minute bars
    df_5min = df.resample("5min").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum"
    }).dropna()
"""

from __future__ import annotations

import base64
import io
import os
import time
import zipfile
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv

from src.utils.logger import get_logger

log = get_logger(__name__)

load_dotenv()

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT    = Path(__file__).parent.parent.parent
INTRADAY_DIR    = PROJECT_ROOT / "data" / "raw" / "intraday"
INTRADAY_DIR.mkdir(parents=True, exist_ok=True)

# ── QuantConnect API base ──────────────────────────────────────────────────────
QC_API_BASE = "https://www.quantconnect.com/api/v2"

# ── Market mapping: AlgoBot code -> QuantConnect ticker ───────────────────────
QC_MARKET_MAP = {
    "ES": {"qc_ticker": "ES", "exchange": "cme",   "asset_class": "future"},
    "NQ": {"qc_ticker": "NQ", "exchange": "cme",   "asset_class": "future"},
    "GC": {"qc_ticker": "GC", "exchange": "comex", "asset_class": "future"},
    "CL": {"qc_ticker": "CL", "exchange": "nymex", "asset_class": "future"},
    "ZB": {"qc_ticker": "ZB", "exchange": "cbot",  "asset_class": "future"},
    "6E": {"qc_ticker": "6E", "exchange": "cme",   "asset_class": "future"},
}

# ── Valid resolutions ──────────────────────────────────────────────────────────
VALID_RESOLUTIONS = {"minute", "hour", "daily"}


# ── Authentication ─────────────────────────────────────────────────────────────

def _get_auth_header() -> dict:
    """
    Build the HTTP Basic Auth header from environment variables.

    Returns:
        dict with "Authorization" header value.

    Raises:
        EnvironmentError: If QC_USER_ID or QC_API_TOKEN not set in .env.
    """
    user_id   = os.environ.get("QC_USER_ID",   "").strip()
    api_token = os.environ.get("QC_API_TOKEN",  "").strip()

    if not user_id or user_id == "your_user_id_here":
        raise EnvironmentError(
            "QC_USER_ID not set in .env file.\n"
            "Get it from: quantconnect.com > Account > My Account\n"
            "Add to AlgoBot/.env:  QC_USER_ID=12345"
        )
    if not api_token or api_token == "your_api_token_here":
        raise EnvironmentError(
            "QC_API_TOKEN not set in .env file.\n"
            "Generate at: quantconnect.com > Account > My Account > API Access\n"
            "Add to AlgoBot/.env:  QC_API_TOKEN=your_token_here"
        )

    credentials = f"{user_id}:{api_token}"
    encoded     = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {encoded}"}


def _credentials_available() -> bool:
    """Return True if QC credentials are configured in .env."""
    user_id   = os.environ.get("QC_USER_ID",  "").strip()
    api_token = os.environ.get("QC_API_TOKEN", "").strip()
    return (
        bool(user_id) and user_id != "your_user_id_here" and
        bool(api_token) and api_token != "your_api_token_here"
    )


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _intraday_cache_path(market: str, resolution: str, start: str, end: str) -> Path:
    """Build the cache file path for intraday data."""
    return INTRADAY_DIR / f"{market}_{resolution}_{start}_{end}.parquet"


def _load_intraday_cache(cache_file: Path) -> Optional[pd.DataFrame]:
    """Load intraday data from parquet cache. Returns None on cache miss."""
    if cache_file.exists():
        log.debug("Intraday cache hit: {f}", f=cache_file.name)
        df = pd.read_parquet(cache_file)
        return df
    return None


def _save_intraday_cache(df: pd.DataFrame, cache_file: Path) -> None:
    """Save intraday DataFrame to parquet cache."""
    df.to_parquet(cache_file)
    log.debug("Intraday cached: {f} ({n} bars)", f=cache_file.name, n=len(df))


# ── QC API request helper ─────────────────────────────────────────────────────

def _qc_get(endpoint: str, params: dict = None, timeout: int = 60) -> dict:
    """
    Make an authenticated GET request to the QuantConnect API.

    Args:
        endpoint: API endpoint path (e.g., "/data/read")
        params:   Query parameters dict
        timeout:  Request timeout in seconds

    Returns:
        JSON response as dict.

    Raises:
        requests.RequestException: On HTTP errors or timeouts.
        EnvironmentError: If credentials not configured.
    """
    headers = _get_auth_header()
    url     = f"{QC_API_BASE}{endpoint}"

    response = requests.get(url, headers=headers, params=params, timeout=timeout)
    response.raise_for_status()

    data = response.json()
    if not data.get("success", True):
        errors = data.get("errors", ["Unknown QC API error"])
        raise RuntimeError(f"QC API error: {errors}")

    return data


# ── Data download ──────────────────────────────────────────────────────────────

def _build_qc_file_path(market: str, resolution: str, dt: date) -> str:
    """
    Build the QuantConnect data file path for a given market, resolution, and date.

    QuantConnect stores data in a directory structure like:
      future/usa/minute/es/20240101_trade.zip

    Args:
        market:     AlgoBot market code ("ES", "NQ", etc.)
        resolution: "minute" | "hour" | "daily"
        dt:         The date for which to build the path.

    Returns:
        File path string for the QC data API.
    """
    if market not in QC_MARKET_MAP:
        raise ValueError(f"Unknown market: {market}. Must be one of {list(QC_MARKET_MAP.keys())}")

    ticker = QC_MARKET_MAP[market]["qc_ticker"].lower()
    date_str = dt.strftime("%Y%m%d")
    return f"future/usa/{resolution}/{ticker}/{date_str}_trade.zip"


def _parse_qc_csv(csv_content: str, resolution: str) -> pd.DataFrame:
    """
    Parse a QuantConnect CSV data file into a standardized OHLCV DataFrame.

    QC minute CSV format (space or comma delimited):
      timestamp, open, high, low, close, volume
    Where timestamp is milliseconds since midnight for minute data,
    or Unix milliseconds for daily data.

    Args:
        csv_content: Raw CSV string from QC zip file.
        resolution:  "minute" | "hour" | "daily"

    Returns:
        DataFrame with DatetimeIndex and columns: Open, High, Low, Close, Volume.
        Returns empty DataFrame if parsing fails.
    """
    try:
        df = pd.read_csv(
            io.StringIO(csv_content),
            header=None,
            names=["Timestamp", "Open", "High", "Low", "Close", "Volume"],
        )

        if df.empty:
            return pd.DataFrame()

        # QC futures prices are stored as scaled integers (divide by 10000 for most futures)
        # The scaling factor depends on the asset — for simplicity we detect based on price range
        price_cols = ["Open", "High", "Low", "Close"]
        for col in price_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0)

        # QC stores minute bar timestamps as milliseconds from midnight (local market time)
        # Convert to proper DatetimeIndex (will be adjusted with date context by caller)
        df["Timestamp"] = pd.to_numeric(df["Timestamp"], errors="coerce")

        df = df.dropna(subset=["Timestamp", "Open", "Close"])
        return df

    except Exception as e:
        log.error("QC CSV parse error: {err}", err=str(e))
        return pd.DataFrame()


def download_qc_intraday(
    market: str,
    resolution: str = "minute",
    start: str = "2020-01-01",
    end: str   = "2024-12-31",
    use_cache:     bool = True,
    force_refresh: bool = False,
) -> pd.DataFrame:
    """
    Download historical intraday futures data from QuantConnect.

    Downloads day-by-day from QC's data API, decompresses ZIP files,
    parses CSV content, and assembles a continuous OHLCV DataFrame.
    Results are cached to Parquet for fast subsequent access.

    Args:
        market:        AlgoBot market code ("ES", "NQ", "GC", "CL", "ZB", "6E")
        resolution:    Data resolution ("minute", "hour", "daily")
        start:         Start date "YYYY-MM-DD"
        end:           End date "YYYY-MM-DD"
        use_cache:     Return cached data if available (default True)
        force_refresh: Ignore cache and re-download (default False)

    Returns:
        DataFrame with DatetimeIndex (timestamps in ET) and columns:
        Open, High, Low, Close, Volume.
        Returns empty DataFrame if credentials not configured or download fails.

    Raises:
        EnvironmentError: If QC credentials not configured in .env.

    Example:
        # Download 1-min ES data for 2023
        df_1min = download_qc_intraday("ES", "minute", "2023-01-01", "2023-12-31")

        # Resample to 5-minute bars
        df_5min = df_1min.resample("5min").agg({
            "Open": "first", "High": "max",
            "Low": "min", "Close": "last", "Volume": "sum"
        }).dropna()
        print(f"5-min bars: {len(df_5min)}")
    """
    if resolution not in VALID_RESOLUTIONS:
        raise ValueError(f"Invalid resolution '{resolution}'. Must be one of {VALID_RESOLUTIONS}")

    if market not in QC_MARKET_MAP:
        raise ValueError(f"Unknown market '{market}'. Must be one of {list(QC_MARKET_MAP.keys())}")

    if not _credentials_available():
        log.warning(
            "QC credentials not configured. Add QC_USER_ID and QC_API_TOKEN to .env.\n"
            "See src/utils/qc_downloader.py module docstring for setup instructions.\n"
            "Returning empty DataFrame — intraday strategies will not backtest."
        )
        return pd.DataFrame()

    # ── Check cache ────────────────────────────────────────────────────────────
    cache_file = _intraday_cache_path(market, resolution, start, end)
    if use_cache and not force_refresh:
        cached = _load_intraday_cache(cache_file)
        if cached is not None and not cached.empty:
            log.info("{market} {res}: Loaded {n} bars from cache ({start} to {end})",
                     market=market, res=resolution, n=len(cached), start=start, end=end)
            return cached

    log.info("{market}: Downloading QC {res} data ({start} to {end})",
             market=market, res=resolution, start=start, end=end)

    # ── Build date range ───────────────────────────────────────────────────────
    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt   = datetime.strptime(end,   "%Y-%m-%d").date()

    all_frames: list[pd.DataFrame] = []
    current_dt = start_dt
    failed_days = 0
    headers    = _get_auth_header()

    while current_dt <= end_dt:
        # Skip weekends
        if current_dt.weekday() >= 5:
            current_dt += timedelta(days=1)
            continue

        file_path = _build_qc_file_path(market, resolution, current_dt)

        try:
            # QuantConnect data read endpoint
            url    = f"{QC_API_BASE}/data/read"
            params = {"filePath": file_path}
            resp   = requests.get(url, headers=headers, params=params, timeout=30)

            if resp.status_code == 404:
                # No data for this date (holiday, early close, etc.) — normal
                current_dt += timedelta(days=1)
                continue

            resp.raise_for_status()

            # Response may be raw ZIP bytes or JSON with base64 data
            content_type = resp.headers.get("Content-Type", "")

            if "application/zip" in content_type or resp.content[:2] == b"PK":
                # Direct ZIP file response
                zip_bytes = resp.content
            else:
                # JSON response with encoded data
                data = resp.json()
                if not data.get("success", True):
                    current_dt += timedelta(days=1)
                    continue
                encoded = data.get("data", "")
                if not encoded:
                    current_dt += timedelta(days=1)
                    continue
                zip_bytes = base64.b64decode(encoded)

            # Decompress and parse
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for name in zf.namelist():
                    if name.endswith(".csv"):
                        csv_text = zf.read(name).decode("utf-8", errors="replace")
                        day_df   = _parse_qc_csv(csv_text, resolution)

                        if not day_df.empty:
                            # Convert milliseconds-from-midnight to full timestamps
                            # QC minute data: Timestamp = ms since midnight ET
                            midnight = pd.Timestamp(current_dt)
                            day_df.index = midnight + pd.to_timedelta(
                                day_df["Timestamp"], unit="ms"
                            )
                            day_df = day_df.drop(columns=["Timestamp"])

                            # QC futures prices are scaled (x10000 in some cases)
                            # Auto-detect: if all prices look like integers > 10000,
                            # divide by 10000
                            sample_close = day_df["Close"].median()
                            if market == "ES" and sample_close > 100000:
                                for c in ["Open", "High", "Low", "Close"]:
                                    day_df[c] /= 10000.0
                            elif market == "NQ" and sample_close > 100000:
                                for c in ["Open", "High", "Low", "Close"]:
                                    day_df[c] /= 10000.0
                            elif sample_close > 10000:
                                for c in ["Open", "High", "Low", "Close"]:
                                    day_df[c] /= 10000.0

                            all_frames.append(day_df)
                        break  # Each ZIP has one CSV file

        except requests.exceptions.Timeout:
            log.warning("{market}: QC request timeout for {dt}", market=market, dt=current_dt)
            failed_days += 1
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code in (401, 403):
                log.error(
                    "QC API authentication failed. Check QC_USER_ID and QC_API_TOKEN in .env.\n"
                    "Error: {err}", err=str(e)
                )
                return pd.DataFrame()
            log.warning("{market}: HTTP error for {dt}: {err}", market=market, dt=current_dt, err=e)
            failed_days += 1
        except Exception as e:
            log.warning("{market}: Parse error for {dt}: {err}", market=market, dt=current_dt, err=e)
            failed_days += 1

        current_dt += timedelta(days=1)

        # Polite rate limiting (QC allows ~10 req/sec on free tier)
        time.sleep(0.12)

    # ── Assemble result ────────────────────────────────────────────────────────
    if not all_frames:
        log.error("{market}: No data downloaded from QC for {start} to {end}. "
                  "Failed days: {fd}", market=market, start=start, end=end, fd=failed_days)
        return pd.DataFrame()

    result = pd.concat(all_frames, axis=0)
    result = result.sort_index()
    result = result[~result.index.duplicated(keep="first")]
    result.index.name = "Timestamp"

    # Ensure correct column types
    for col in ["Open", "High", "Low", "Close"]:
        result[col] = result[col].astype(float)
    result["Volume"] = result["Volume"].astype(float)

    log.info(
        "{market}: QC {res} download complete. {n} bars ({start} to {end}). "
        "Failed days: {fd}",
        market=market, res=resolution, n=len(result),
        start=start, end=end, fd=failed_days,
    )

    # ── Save to cache ──────────────────────────────────────────────────────────
    if use_cache:
        try:
            _save_intraday_cache(result, cache_file)
        except Exception as e:
            log.warning("Failed to save intraday cache: {err}", err=str(e))

    return result


# ── Convenience loader ─────────────────────────────────────────────────────────

def load_intraday(
    market: str,
    resolution_minutes: int = 5,
    start: str = "2020-01-01",
    end: str   = "2024-12-31",
    rth_only: bool = True,
) -> pd.DataFrame:
    """
    Load intraday bars at a specified minute resolution, RTH-filtered.

    Downloads 1-minute data from QC (or loads from cache) and resamples
    to the requested resolution. Optionally filters to Regular Trading Hours
    (9:30 AM to 4:00 PM ET for ES/NQ).

    Args:
        market:             AlgoBot market code ("ES", "NQ")
        resolution_minutes: Bar size in minutes (1, 5, 15, 30, 60)
        start:              Start date "YYYY-MM-DD"
        end:                End date "YYYY-MM-DD"
        rth_only:           True = keep only 9:30-16:00 ET (RTH sessions)

    Returns:
        OHLCV DataFrame at the requested resolution with DatetimeIndex.

    Example:
        df_5min = load_intraday("ES", resolution_minutes=5,
                                start="2022-01-01", end="2024-12-31")
        print(f"5-min bars: {len(df_5min)}")
        print(df_5min.head())
    """
    # Download 1-min base data
    df_1min = download_qc_intraday(
        market=market, resolution="minute", start=start, end=end
    )

    if df_1min.empty:
        log.warning("{market}: No 1-min data available to resample", market=market)
        return pd.DataFrame()

    # ── Filter to RTH ──────────────────────────────────────────────────────────
    if rth_only:
        # RTH for CME equity index futures: 9:30 AM - 4:00 PM ET
        rth_start = "09:30"
        rth_end   = "16:00"
        df_1min = df_1min.between_time(rth_start, rth_end)
        log.debug("{market}: RTH filter applied. {n} 1-min bars remaining",
                  market=market, n=len(df_1min))

    if df_1min.empty:
        return pd.DataFrame()

    # ── Resample to target resolution ─────────────────────────────────────────
    if resolution_minutes == 1:
        return df_1min  # Already 1-min

    freq = f"{resolution_minutes}min"
    df_resampled = df_1min.resample(freq, closed="left", label="left").agg(
        {
            "Open":   "first",
            "High":   "max",
            "Low":    "min",
            "Close":  "last",
            "Volume": "sum",
        }
    ).dropna(subset=["Open", "Close"])

    log.info(
        "{market}: Resampled to {res}-min. {n} bars ({start} to {end})",
        market=market, res=resolution_minutes, n=len(df_resampled),
        start=start, end=end,
    )

    return df_resampled


# ── Credential check helper ────────────────────────────────────────────────────

def check_qc_credentials() -> bool:
    """
    Test whether the QuantConnect API credentials are valid.

    Makes a lightweight authenticated API call and checks the response.
    Use this during setup to confirm credentials work before downloading.

    Returns:
        True if credentials are valid, False otherwise.

    Example:
        from src.utils.qc_downloader import check_qc_credentials
        if check_qc_credentials():
            print("QC connection OK")
        else:
            print("Check QC_USER_ID and QC_API_TOKEN in .env")
    """
    if not _credentials_available():
        log.warning("QC credentials not found in .env")
        return False

    try:
        data = _qc_get("/authenticate")
        success = bool(data.get("success", False))
        if success:
            log.info("QC API authentication: OK")
        else:
            log.warning("QC API authentication: FAILED. Errors: {e}",
                        e=data.get("errors", []))
        return success

    except EnvironmentError as e:
        log.error("QC credentials error: {e}", e=str(e))
        return False
    except Exception as e:
        log.error("QC API connection failed: {e}", e=str(e))
        return False
