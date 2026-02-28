"""
AlgoBot — Backtest Data Loader
================================
Module:  src/backtest/data_loader.py
Phase:   3 — Backtesting Engine
Purpose: Loads historical market data and runs the full signal pipeline
         so the backtest engine receives ready-to-use DataFrames.

The full pipeline applied to each market:
  1. download_market()          -- Yahoo Finance OHLCV with caching
  2. clean_market_data()        -- Validate, remove outliers, fill gaps
  3. calculate_indicators()     -- EMA, ATR, RSI, ADX, Donchian
  4. add_atr_baseline()         -- Rolling ATR baseline for regime detection
  5. classify_regimes()         -- 5-state regime (TRENDING/RANGING/etc.)
  6. tma_signal()               -- Triple Moving Average signal
  7. tma_exit_signal()          -- TMA exit conditions
  8. dcs_signal()               -- Donchian Channel breakout signal
  9. vmr_signal()               -- Volatility Mean Reversion signal
  10. combine_signals()          -- Signal Agreement Filter
  11. add_position_sizes()       -- ATR-based 1% risk sizing

Output: dict of market_code -> DataFrame, all aligned to the same dates.

Usage:
    from src.backtest.data_loader import load_market_data, load_all_markets

    # Load a single market (2020-2024)
    df = load_market_data("ES", "2020-01-01", "2024-12-31", config)

    # Load all 6 markets for a full backtest
    data = load_all_markets("2000-01-01", "2024-12-31", config,
                            account_equity=150000.0)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd
import yaml

from src.utils.logger import get_logger
from src.utils.data_downloader import download_market, download_all_markets
from src.utils.data_cleaner import clean_market_data, clean_all_markets, align_dates
from src.strategy.indicators import calculate_indicators, add_atr_baseline
from src.strategy.regime_classifier import classify_regimes
from src.strategy.tma_signal import tma_signal, tma_exit_signal
from src.strategy.dcs_signal import dcs_signal
from src.strategy.vmr_signal import vmr_signal
from src.strategy.htf_bias import add_htf_bias
from src.strategy.signal_combiner import combine_signals
from src.strategy.position_sizer import add_position_sizes

log = get_logger(__name__)

# Default markets to include in a full backtest
ALL_MARKETS = ["ES", "NQ", "GC", "CL", "ZB", "6E"]


# ── Single market loader ───────────────────────────────────────────────────────

def load_market_data(
    market: str,
    start: str,
    end: str,
    config: dict,
    account_equity: float = 150_000.0,
    use_etf_sizing: bool = True,
) -> pd.DataFrame:
    """
    Load, clean, and signal-process a single market for backtesting.

    Args:
        market:         Market code ("ES", "NQ", "GC", "CL", "ZB", "6E")
        start:          Start date string "YYYY-MM-DD"
        end:            End date string "YYYY-MM-DD"
        config:         Full config dict (from config.yaml)
        account_equity: Starting account size for position sizing
        use_etf_sizing: True (backtest) uses fractional ETF units

    Returns:
        DataFrame with all OHLCV, indicator, regime, signal, and
        position-size columns. Ready to feed into BacktestEngine.

    Raises:
        RuntimeError: If download or cleaning fails.
    """
    strat_cfg = config.get("strategy", config)   # Handle both full config and strategy-only

    log.info("{market}: Loading data {start} to {end}", market=market, start=start, end=end)

    # ── 1. Download ────────────────────────────────────────────────────────────
    raw_df = download_market(market, start, end)
    if raw_df is None or raw_df.empty:
        raise RuntimeError(f"{market}: download returned empty DataFrame")

    # ── 2. Clean ───────────────────────────────────────────────────────────────
    clean_df, report = clean_market_data(raw_df, market)
    if not report.passed_validation:
        log.warning("{market}: Cleaning report had issues: {errs}",
                    market=market, errs=report.validation_errors)

    # ── 3-4. Indicators + ATR baseline ────────────────────────────────────────
    df = calculate_indicators(clean_df, strat_cfg, market)
    df = add_atr_baseline(df)

    # ── 5. Regime ──────────────────────────────────────────────────────────────
    regime_cfg = config.get("regime", strat_cfg)
    df = classify_regimes(df, regime_cfg, market)

    # ── 6-8. Signals ──────────────────────────────────────────────────────────
    df = tma_signal(df, market)
    df = tma_exit_signal(df)
    df = dcs_signal(df, market)
    df = vmr_signal(df, strat_cfg, market)

    # ── 9. HTF bias (Phase 5) ─────────────────────────────────────────────────
    df = add_htf_bias(df, config, market)

    # ── 10. Signal combiner + HTF bias gate ──────────────────────────────────
    df = combine_signals(df, market, config)

    # ── 11. Position sizing ───────────────────────────────────────────────────
    df = add_position_sizes(df, market, strat_cfg, account_equity=account_equity,
                            use_etf_sizing=use_etf_sizing)

    log.info(
        "{market}: Pipeline complete. {n} bars, {entries} entry signals "
        "({tl} trend-long, {ts} trend-short, {vmr} VMR)",
        market=market,
        n=len(df),
        entries=int(df["combined_new_entry"].sum()),
        tl=int((df["combined_signal"] == "AGREE_LONG").sum()),
        ts=int((df["combined_signal"] == "AGREE_SHORT").sum()),
        vmr=int(df["combined_is_vmr"].sum()),
    )

    return df


# ── All-market loader ──────────────────────────────────────────────────────────

def load_all_markets(
    start: str,
    end: str,
    config: dict,
    markets: Optional[list] = None,
    account_equity: float = 150_000.0,
    use_etf_sizing: bool = True,
    align: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    Load all markets and optionally align them to the same trading dates.

    Args:
        start:          Start date string "YYYY-MM-DD"
        end:            End date string "YYYY-MM-DD"
        config:         Full config dict
        markets:        List of market codes (default: all 6)
        account_equity: Starting account size for position sizing
        use_etf_sizing: True for fractional ETF units (backtest)
        align:          True = align all markets to intersection of dates
                        False = keep each market's own date range

    Returns:
        dict mapping market_code -> processed DataFrame
        All DataFrames share the same DatetimeIndex if align=True.
    """
    if markets is None:
        markets = ALL_MARKETS

    log.info("Loading {n} markets for backtest: {start} to {end}",
             n=len(markets), start=start, end=end)

    strat_cfg = config.get("strategy", config)

    # ── Download all raw data first (uses caching efficiently) ─────────────────
    log.info("Downloading raw data for all markets...")
    raw_data = download_all_markets(start=start, end=end)
    cleaned_data, _ = clean_all_markets(raw_data)

    if align:
        aligned_data = align_dates(cleaned_data, method="intersection")
        log.info("Aligned to {n} common trading dates",
                 n=len(next(iter(aligned_data.values()))))
    else:
        aligned_data = cleaned_data

    # ── Run signal pipeline on each market ────────────────────────────────────
    result: dict[str, pd.DataFrame] = {}
    regime_cfg = config.get("regime", strat_cfg)

    for market in markets:
        if market not in aligned_data:
            log.warning("{market}: Not found in downloaded data, skipping", market=market)
            continue

        df = aligned_data[market].copy()

        try:
            df = calculate_indicators(df, strat_cfg, market)
            df = add_atr_baseline(df)
            df = classify_regimes(df, regime_cfg, market)
            df = tma_signal(df, market)
            df = tma_exit_signal(df)
            df = dcs_signal(df, market)
            df = vmr_signal(df, strat_cfg, market)
            df = add_htf_bias(df, config, market)
            df = combine_signals(df, market, config)
            df = add_position_sizes(df, market, strat_cfg,
                                    account_equity=account_equity,
                                    use_etf_sizing=use_etf_sizing)
            result[market] = df

        except Exception as exc:
            log.error("{market}: Pipeline failed: {err}", market=market, err=exc)
            raise

    log.info(
        "All {n} markets loaded. Common date range: {start} to {end}",
        n=len(result),
        start=next(iter(result.values())).index[0].date() if result else "N/A",
        end=next(iter(result.values())).index[-1].date() if result else "N/A",
    )

    return result


# ── Config loader helper ───────────────────────────────────────────────────────

def load_config(config_path: Optional[str] = None) -> dict:
    """
    Load config.yaml from the standard location.

    Args:
        config_path: Optional override path. Default: AlgoBot/config/config.yaml

    Returns:
        Full config dictionary.
    """
    if config_path is None:
        config_path = Path(__file__).parents[2] / "config" / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)
