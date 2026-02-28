"""
AlgoBot — Data Cleaner & Validator
====================================
Module:  src/utils/data_cleaner.py
Phase:   1 — Data Infrastructure
Purpose: Validates, cleans, and aligns raw market data before it reaches
         the strategy signal calculators.

Why cleaning matters for backtesting:
  Dirty data creates phantom signals. A single bad bar — a price spike,
  a missing date, or a rollover artifact — can trigger a false trade
  that inflates backtest results. Every bar we feed to the signal engine
  must be verified clean.

Cleaning pipeline (applied in order):
  1. validate_ohlcv()     → Structural checks: high≥low, close in range, etc.
  2. remove_outliers()    → Statistical: remove price spikes > N sigma
  3. fill_gaps()          → Fill or flag missing trading days
  4. normalize_dtypes()   → Ensure float64, proper DatetimeIndex
  5. align_dates()        → Sync multiple markets to identical trading dates

Each function returns both the cleaned DataFrame AND a CleaningReport
that documents what was found and changed. This report goes into LAB_002.
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional

from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class CleaningReport:
    """
    Records every change made during the cleaning pipeline for one market.
    Used to generate the data quality section of lab reports.
    """
    market:               str
    original_bar_count:   int                = 0
    final_bar_count:      int                = 0
    nan_bars_found:       int                = 0
    nan_bars_filled:      int                = 0
    nan_bars_dropped:     int                = 0
    outlier_bars_removed: int                = 0
    gaps_detected:        int                = 0
    gaps_filled:          int                = 0
    validation_errors:    list[str]          = field(default_factory=list)
    warnings:             list[str]          = field(default_factory=list)
    passed_validation:    bool               = True

    def summary(self) -> str:
        """Return a one-line summary of cleaning results."""
        return (
            f"{self.market}: "
            f"{self.original_bar_count}->{self.final_bar_count} bars | "
            f"NaN filled: {self.nan_bars_filled} | "
            f"Outliers removed: {self.outlier_bars_removed} | "
            f"Gaps filled: {self.gaps_filled} | "
            f"{'PASS' if self.passed_validation else 'FAIL'}"
        )


# ── Validation ────────────────────────────────────────────────────────────────

def validate_ohlcv(df: pd.DataFrame, market: str = "UNKNOWN") -> tuple[pd.DataFrame, CleaningReport]:
    """
    Validate OHLC structural integrity. Catch data errors that would create
    impossible or misleading price bars.

    Checks performed:
      1. Required columns exist (Open, High, Low, Close)
      2. No negative prices
      3. High >= Low on every bar (impossible otherwise)
      4. High >= Open and High >= Close (high must be the highest)
      5. Low  <= Open and Low  <= Close (low must be the lowest)
      6. Open, High, Low, Close are all finite numbers (no inf)

    Bars that fail any check are flagged. If more than 5% of bars fail,
    the report marks passed_validation=False and logs an error.

    Args:
        df:     Raw OHLCV DataFrame with DatetimeIndex
        market: Market name for logging and report labeling

    Returns:
        Tuple of (cleaned_df, CleaningReport)
        cleaned_df has invalid bars dropped.
    """
    report = CleaningReport(market=market, original_bar_count=len(df))
    df = df.copy()

    price_cols = ["Open", "High", "Low", "Close"]
    missing_cols = [c for c in price_cols if c not in df.columns]
    if missing_cols:
        msg = f"Missing required columns: {missing_cols}"
        report.validation_errors.append(msg)
        report.passed_validation = False
        log.error("{market} validation failed: {msg}", market=market, msg=msg)
        return df, report

    # Count NaN values before doing anything
    nan_mask = df[price_cols].isnull().any(axis=1)
    report.nan_bars_found = int(nan_mask.sum())

    # Check 1: No negative prices
    negative_mask = (df[price_cols] < 0).any(axis=1)
    if negative_mask.any():
        count = int(negative_mask.sum())
        report.validation_errors.append(f"{count} bars with negative prices")
        df = df[~negative_mask]
        log.warning("{market}: Removed {n} bars with negative prices", market=market, n=count)

    # Check 2: High >= Low
    hl_invalid = df["High"] < df["Low"]
    if hl_invalid.any():
        count = int(hl_invalid.sum())
        report.validation_errors.append(f"{count} bars where High < Low")
        df = df[~hl_invalid]
        log.warning("{market}: Removed {n} bars where High < Low", market=market, n=count)

    # Check 3: High is highest (High >= Open and High >= Close)
    high_invalid = (df["High"] < df["Open"]) | (df["High"] < df["Close"])
    if high_invalid.any():
        count = int(high_invalid.sum())
        report.warnings.append(f"{count} bars where High < Open or Close")
        # Don't remove these — could be minor float rounding — just log
        log.debug("{market}: {n} bars where High < Open or Close (float rounding?)",
                  market=market, n=count)

    # Check 4: Low is lowest (Low <= Open and Low <= Close)
    low_invalid = (df["Low"] > df["Open"]) | (df["Low"] > df["Close"])
    if low_invalid.any():
        count = int(low_invalid.sum())
        report.warnings.append(f"{count} bars where Low > Open or Close")
        log.debug("{market}: {n} bars where Low > Open or Close (float rounding?)",
                  market=market, n=count)

    # Check 5: No infinite values
    inf_mask = np.isinf(df[price_cols].values).any(axis=1)
    if inf_mask.any():
        count = int(inf_mask.sum())
        report.validation_errors.append(f"{count} bars with infinite values")
        df = df[~inf_mask]
        log.warning("{market}: Removed {n} bars with infinite values", market=market, n=count)

    report.final_bar_count = len(df)

    # If more than 5% of bars had errors, mark as failed
    if report.validation_errors:
        error_pct = (report.original_bar_count - report.final_bar_count) / max(report.original_bar_count, 1)
        if error_pct > 0.05:
            report.passed_validation = False
            log.error("{market}: {pct:.1%} of bars removed — data quality too low",
                      market=market, pct=error_pct)
        else:
            log.info("{market}: Validation complete. {n} bars removed ({pct:.1%})",
                     market=market, n=report.original_bar_count - report.final_bar_count, pct=error_pct)
    else:
        log.info("{market}: Validation passed — no structural errors found", market=market)

    return df, report


# ── Outlier removal ───────────────────────────────────────────────────────────

def remove_outliers(
    df: pd.DataFrame,
    report: CleaningReport,
    sigma: float = 5.0,
    window: int  = 20,
) -> tuple[pd.DataFrame, CleaningReport]:
    """
    Remove price bars that are statistically implausible — spikes that
    represent data errors rather than real market moves.

    Method: Rolling Z-score on Close price.
      For each bar: z = (close - rolling_mean) / rolling_std
      If |z| > sigma threshold: bar flagged as outlier and removed.

    Why 5 sigma: A genuine 5-sigma move in price (≈ 1-in-3.5 million probability
    under normality) almost certainly represents a data error, not a real trade.
    Real market crashes (2008, 2020 COVID) typically reach 3–4 sigma.

    Why rolling not global: Market volatility changes over time (low vol in
    2017, high vol in 2020). Using a local 20-bar window adapts to regime.

    Args:
        df:      OHLCV DataFrame (already validated)
        report:  Existing CleaningReport to append outlier info to
        sigma:   Z-score threshold (default 5.0 — very conservative)
        window:  Rolling window for mean/std calculation (default 20 bars)

    Returns:
        Tuple of (cleaned_df, updated_report)
    """
    df = df.copy()

    if "Close" not in df.columns or len(df) < window + 1:
        return df, report

    # Calculate log returns (better behaved than price levels for z-scoring)
    log_returns = np.log(df["Close"] / df["Close"].shift(1)).dropna()

    # Rolling z-score of log returns
    rolling_mean = log_returns.rolling(window=window, min_periods=5).mean()
    rolling_std  = log_returns.rolling(window=window, min_periods=5).std()

    # Avoid division by zero
    rolling_std = rolling_std.replace(0, np.nan)

    z_scores = (log_returns - rolling_mean) / rolling_std
    outlier_dates = z_scores[z_scores.abs() > sigma].index

    if len(outlier_dates) > 0:
        log.warning("{market}: Removing {n} outlier bars (|z| > {s} sigma): {dates}",
                    market=report.market, n=len(outlier_dates),
                    s=sigma, dates=outlier_dates.strftime("%Y-%m-%d").tolist()[:5])
        df = df.drop(index=outlier_dates, errors="ignore")
        report.outlier_bars_removed = len(outlier_dates)
        report.final_bar_count = len(df)
    else:
        log.info("{market}: No outliers detected (threshold: {s} sigma)",
                 market=report.market, s=sigma)

    return df, report


# ── Gap filling ───────────────────────────────────────────────────────────────

def fill_gaps(
    df: pd.DataFrame,
    report: CleaningReport,
    method: str = "ffill",
    max_gap_days: int = 5,
) -> tuple[pd.DataFrame, CleaningReport]:
    """
    Detect and fill missing NaN values in OHLCV data.

    Missing data in a time series creates false signals. If bar T is NaN
    and we use it in a 20-bar EMA calculation, the EMA is wrong for 20+ bars.

    Strategy:
      - Fill NaN Close/Open/High/Low with forward-fill (use last known price)
      - NaN Volume filled with 0 (unknown volume → assume no volume)
      - If a NaN gap spans more than max_gap_days: drop those bars instead
        of filling, as 5+ days of missing data is likely a data source problem

    Args:
        df:           OHLCV DataFrame
        report:       CleaningReport to update
        method:       Fill method: "ffill" (forward fill) or "drop"
        max_gap_days: Maximum consecutive NaN days to fill (default 5)

    Returns:
        Tuple of (cleaned_df, updated_report)
    """
    df = df.copy()
    price_cols = ["Open", "High", "Low", "Close"]

    nan_mask = df[price_cols].isnull().any(axis=1)
    report.nan_bars_found = int(nan_mask.sum())

    if report.nan_bars_found == 0:
        log.info("{market}: No NaN values found — gap fill not needed",
                 market=report.market)
        return df, report

    log.info("{market}: Found {n} bars with NaN prices — filling with {m}",
             market=report.market, n=report.nan_bars_found, m=method)

    # Find consecutive NaN runs and check their length
    nan_groups = (nan_mask != nan_mask.shift()).cumsum()
    for group_id in nan_groups[nan_mask].unique():
        group_dates = df.index[nan_mask & (nan_groups == group_id)]
        if len(group_dates) > max_gap_days:
            # Gap too large to fill — drop these rows
            df = df.drop(index=group_dates)
            report.nan_bars_dropped += len(group_dates)
            log.warning("{market}: Dropped {n}-day NaN gap starting {start}",
                        market=report.market, n=len(group_dates),
                        start=group_dates[0].strftime("%Y-%m-%d"))

    # Forward fill remaining (small) NaN gaps
    filled_before = df[price_cols].isnull().sum().sum()
    df[price_cols] = df[price_cols].ffill()
    filled_after   = df[price_cols].isnull().sum().sum()
    report.nan_bars_filled = int(filled_before - filled_after)

    # Fill volume separately
    if "Volume" in df.columns:
        df["Volume"] = df["Volume"].fillna(0)

    report.final_bar_count = len(df)
    report.gaps_filled     = report.nan_bars_filled

    log.info("{market}: Gap fill complete. Filled: {f}, Dropped: {d}",
             market=report.market, f=report.nan_bars_filled, d=report.nan_bars_dropped)

    return df, report


# ── Type normalization ────────────────────────────────────────────────────────

def normalize_dtypes(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure all price columns are float64 and index is DatetimeIndex.

    Some Yahoo Finance downloads return object-type columns or have timezone
    info on the index that can cause issues in calculations.

    Args:
        df: OHLCV DataFrame

    Returns:
        DataFrame with standardized dtypes.
    """
    df = df.copy()
    price_cols = ["Open", "High", "Low", "Close"]

    for col in price_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")

    if "Volume" in df.columns:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0).astype("float64")

    # Remove timezone info if present
    if hasattr(df.index, "tz") and df.index.tz is not None:
        df.index = df.index.tz_localize(None)

    df.index = pd.to_datetime(df.index)
    df.index.name = "Date"
    df = df.sort_index()

    return df


# ── Full pipeline ─────────────────────────────────────────────────────────────

def clean_market_data(
    df: pd.DataFrame,
    market: str,
    sigma: float = 5.0,
) -> tuple[pd.DataFrame, CleaningReport]:
    """
    Run the complete cleaning pipeline on a single market's OHLCV data.

    This is the main entry point. Call this on every DataFrame before
    passing it to any strategy signal calculator.

    Pipeline order:
      1. normalize_dtypes  — ensure correct data types
      2. validate_ohlcv    — structural integrity checks
      3. remove_outliers   — statistical spike removal
      4. fill_gaps         — NaN filling / dropping

    Args:
        df:     Raw OHLCV DataFrame from data_downloader
        market: Market code for logging ("ES", "NQ", etc.)
        sigma:  Outlier detection threshold in standard deviations

    Returns:
        Tuple of (clean_df, CleaningReport)
        clean_df is ready to be passed to indicators and signals.

    Example:
        raw_data  = download_market("ES", "2000-01-01", "2024-12-31")
        clean, report = clean_market_data(raw_data, "ES")
        print(report.summary())
    """
    log.info("Starting cleaning pipeline for {market} ({n} bars)",
             market=market, n=len(df))

    # Step 1: Type normalization (no report needed)
    df = normalize_dtypes(df)

    # Step 2: Structural validation
    df, report = validate_ohlcv(df, market)

    # Step 3: Outlier removal
    df, report = remove_outliers(df, report, sigma=sigma)

    # Step 4: Gap filling
    df, report = fill_gaps(df, report)

    log.info("Cleaning complete for {market}: {summary}",
             market=market, summary=report.summary())

    return df, report


def clean_all_markets(
    raw_data: dict[str, pd.DataFrame],
    sigma: float = 5.0,
) -> tuple[dict[str, pd.DataFrame], dict[str, CleaningReport]]:
    """
    Run the cleaning pipeline on all markets at once.

    Args:
        raw_data: Dict of market → raw DataFrame (from download_all_markets)
        sigma:    Outlier threshold in standard deviations

    Returns:
        Tuple of:
          - Dict of market → cleaned DataFrame
          - Dict of market → CleaningReport (for lab report documentation)

    Example:
        raw     = download_all_markets("2000-01-01", "2024-12-31")
        clean, reports = clean_all_markets(raw)
        for mkt, report in reports.items():
            print(report.summary())
    """
    cleaned = {}
    reports = {}

    for market, df in raw_data.items():
        if df.empty:
            log.warning("Skipping cleaning for {market} — empty DataFrame", market=market)
            continue
        clean_df, report = clean_market_data(df, market, sigma=sigma)
        if not clean_df.empty:
            cleaned[market] = clean_df
            reports[market] = report

    return cleaned, reports


# ── Date alignment ────────────────────────────────────────────────────────────

def align_dates(
    data: dict[str, pd.DataFrame],
    method: str = "intersection",
) -> dict[str, pd.DataFrame]:
    """
    Align all market DataFrames to the same set of trading dates.

    Different markets have different trading calendars.
    Example: Gold trades on some days when US equity markets are closed.
    For multi-market backtesting, all markets must share the same index.

    Methods:
      "intersection": Only keep dates where ALL markets have data (strictest)
      "union":        Keep all dates, NaN for markets closed on that day (lenient)

    The "intersection" method is safer for backtesting — we never trade
    on a day when we don't have data for all markets.

    Args:
        data:   Dict of market → cleaned DataFrame
        method: Alignment method ("intersection" or "union")

    Returns:
        Dict of market → DataFrame, all with identical DatetimeIndex.

    Example:
        aligned = align_dates(cleaned_data, method="intersection")
        # All DataFrames now have identical index
        dates = aligned["ES"].index
        print(f"Common trading days: {len(dates)}")
    """
    if not data:
        return {}

    if len(data) == 1:
        return data  # Nothing to align

    log.info("Aligning {n} markets using '{method}' method",
             n=len(data), method=method)

    indices = [df.index for df in data.values()]

    if method == "intersection":
        common_index = indices[0]
        for idx in indices[1:]:
            common_index = common_index.intersection(idx)
    elif method == "union":
        common_index = indices[0]
        for idx in indices[1:]:
            common_index = common_index.union(idx)
    else:
        raise ValueError(f"Unknown alignment method: {method}. Use 'intersection' or 'union'.")

    aligned = {}
    for market, df in data.items():
        aligned_df = df.reindex(common_index)
        # Forward fill any newly created NaN from union alignment
        if method == "union":
            aligned_df[["Open", "High", "Low", "Close"]] = (
                aligned_df[["Open", "High", "Low", "Close"]].ffill()
            )
        aligned[market] = aligned_df
        log.debug("{market}: {before} -> {after} bars after alignment",
                  market=market, before=len(df), after=len(aligned_df))

    log.info("Alignment complete. Common dates: {n} | Range: {start} to {end}",
             n=len(common_index),
             start=common_index[0].strftime("%Y-%m-%d"),
             end=common_index[-1].strftime("%Y-%m-%d"))

    return aligned
