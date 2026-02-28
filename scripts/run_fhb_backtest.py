"""
AlgoBot -- First Hour Breakout (FHB) Backtest Script
======================================================
Script:  scripts/run_fhb_backtest.py
Phase:   5C -- Exit Strategy Fix
Purpose: Test the First Hour Breakout strategy on ~730 days of 1-hour
         Yahoo Finance data (ES and NQ futures) with improved exit logic.

THREE EXIT STRATEGY IMPROVEMENTS vs Phase 5B baseline:
  1. ATR-based stop (0.75 * ATR14) instead of full first-hour range as stop.
     Tighter stop -> smaller risk -> 2R target achievable more often.

  2. Trail stop to breakeven after 1R partial exit.
     Once 50% exits at +1R, move stop to entry price on remaining 50%.
     This converts "partial stop" losses into breakeven exits (free trades).

  3. Volume confirmation filter.
     Only take signals when first-hour bar volume >= 80% of rolling 20-day
     average. Low-volume opens lack institutional conviction.

Strategy Logic (unchanged from 5B):
  - Opening range = first 1-hour bar (9:30-10:30 AM ET)
  - Breakout signal: hourly close outside range + entry buffer
  - 50% partial exit at 1R, trail to breakeven on remainder
  - Full target at 2R
  - Max hold: 5 hourly bars from entry (~5 hours)
  - HTF filter: weekly/monthly bias blocks counter-trend signals

Run:
    cd AlgoBot
    conda run -n algobot_env python scripts/run_fhb_backtest.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.data_downloader import download_market
from src.strategy.indicators import calculate_indicators, add_atr_baseline
from src.strategy.regime_classifier import classify_regimes
from src.strategy.htf_bias import add_htf_bias
from src.utils.logger import get_logger

log = get_logger(__name__)

# ============================================================
# STRATEGY PARAMETERS
# ============================================================

FHB_RANGE_START     = "09:30"   # First hour bar open
FHB_RANGE_END       = "10:30"   # First hour bar close (range window end)
FHB_NO_ENTRY_AFTER  = "13:00"   # No new entries after 1 PM ET
FHB_MAX_HOLD_BARS   = 5         # Max hold: 5 hourly bars from entry
FHB_PARTIAL_R       = 1.0       # Partial exit at 1R (50% of position)
FHB_TARGET_R        = 2.0       # Full target at 2R
FHB_PARTIAL_PCT     = 0.50      # 50% partial exit fraction
FHB_ENTRY_BUF_TICKS = 1         # 1 tick buffer above/below range boundary

# ---- EXIT STRATEGY IMPROVEMENTS (Phase 5C) ----
FHB_ATR_PERIOD      = 14        # ATR period (Wilder's) on 1-hour bars
FHB_ATR_STOP_MULT   = 0.75      # Stop distance = 0.75 * ATR14
                                 # Tighter than full-range stop, consistent
FHB_ATR_STOP_CAP    = 1.5       # Cap stop at 1.5x ATR (safety rail)
FHB_TRAIL_BREAKEVEN = True      # Move stop to entry after 1R partial fires
FHB_VOLUME_FILTER   = True      # Enable volume confirmation gate
FHB_VOLUME_LOOKBACK = 20        # Rolling trading-day window for volume avg
FHB_VOLUME_MIN_PCT  = 0.80      # Require >= 80% of avg first-hour volume


# ============================================================
# CONFIG AND DATA LOADING
# ============================================================

def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def download_1h_intraday(market: str) -> pd.DataFrame:
    """
    Download 730 days of 1-hour RTH data from Yahoo Finance for ES or NQ.
    Caches to parquet (4-hour freshness window).
    """
    import time as _time
    import yfinance as yf

    yf_tickers = {"ES": "ES=F", "NQ": "NQ=F"}
    if market not in yf_tickers:
        return pd.DataFrame()

    ticker     = yf_tickers[market]
    cache_dir  = PROJECT_ROOT / "data" / "raw" / "intraday"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"yf_{market}_1h_730d.parquet"

    if cache_file.exists():
        age_h = (_time.time() - cache_file.stat().st_mtime) / 3600.0
        if age_h < 4.0:
            print(f"  {market}: Loading 1h data from cache")
            return pd.read_parquet(cache_file)

    print(f"  {market}: Downloading 730 days of 1-hour data from Yahoo Finance...")
    try:
        raw = yf.download(ticker, period="730d", interval="1h",
                          auto_adjust=True, progress=False, timeout=30)
        if raw is None or raw.empty:
            print(f"  {market}: Empty response from Yahoo Finance")
            return pd.DataFrame()

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [c.title() for c in raw.columns]

        keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in raw.columns]
        df   = raw[keep].copy()
        for col in ["Open", "High", "Low", "Close"]:
            if col in df.columns:
                df[col] = df[col].astype(float)
        if "Volume" in df.columns:
            df["Volume"] = df["Volume"].astype(float)
        df.dropna(subset=["Open", "High", "Low", "Close"], how="all", inplace=True)

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert("America/New_York")
        else:
            df.index = df.index.tz_convert("America/New_York")
        df.index.name = "Timestamp"

        df = df.between_time("09:30", "16:00")

        n_days = df.index.normalize().nunique()
        print(f"  {market}: {len(df)} 1-hour RTH bars | "
              f"{df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')} "
              f"({n_days} days)")
        try:
            df.to_parquet(cache_file)
        except Exception:
            pass
        return df

    except Exception as e:
        print(f"  {market}: Download error: {e}")
        return pd.DataFrame()


def get_htf_bias_series(market: str, config: dict) -> pd.Series:
    """Compute daily HTF combined bias from 7 years of daily bar data."""
    print(f"  Computing HTF bias for {market}...")
    raw = download_market(market, "2019-01-01", "2025-12-31")
    if raw is None or raw.empty:
        print(f"  {market}: No daily data, using NEUTRAL bias")
        return pd.Series(dtype=str)

    strat_cfg  = config.get("strategy", config)
    regime_cfg = config.get("regime", strat_cfg)
    df = calculate_indicators(raw, strat_cfg, market)
    df = add_atr_baseline(df)
    df = classify_regimes(df, regime_cfg, market)
    df = add_htf_bias(df, config, market)

    bias = df["htf_combined_bias"].copy()
    bias.index = pd.to_datetime(bias.index).normalize()
    return bias


# ============================================================
# ATR AND VOLUME HELPERS (Phase 5C additions)
# ============================================================

def compute_1h_atr(df: pd.DataFrame, period: int = FHB_ATR_PERIOD) -> pd.Series:
    """
    Compute Wilder's ATR on 1-hour bars.

    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|).
    Wilder's smoothing uses EWM with alpha = 1/period.

    Returns a Series aligned with df.index.
    """
    h      = df["High"]
    lo     = df["Low"]
    prev_c = df["Close"].shift(1)
    tr = pd.concat(
        [h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(span=period, min_periods=period, adjust=False).mean()


def compute_first_hour_volumes(df_1h: pd.DataFrame) -> pd.Series:
    """
    Extract first-hour (9:30 AM bar) volume per trading day and compute
    a rolling 20-day average.

    Returns a DataFrame with columns: first_hour_vol, vol_avg20d, vol_ok.
    Indexed by calendar date (tz-naive).
    """
    # The 9:30 AM 1-hour bar has hour=9 in ET timezone
    first_bars = df_1h[df_1h.index.hour == 9].copy()
    daily_vol  = first_bars["Volume"].copy()
    daily_vol.index = daily_vol.index.normalize().tz_localize(None)

    # Handle duplicate dates (shouldn't happen, but guard against it)
    daily_vol = daily_vol[~daily_vol.index.duplicated(keep="last")]
    daily_vol = daily_vol.sort_index()

    vol_avg  = daily_vol.rolling(FHB_VOLUME_LOOKBACK, min_periods=5).mean()
    vol_ok   = daily_vol >= (vol_avg * FHB_VOLUME_MIN_PCT)

    return pd.DataFrame({
        "first_hour_vol": daily_vol,
        "vol_avg20d":     vol_avg,
        "vol_ok":         vol_ok,
    })


# ============================================================
# SIGNAL COMPUTATION
# ============================================================

def compute_fhb_signals(
    df_1h:          pd.DataFrame,
    market:         str,
    htf_bias_series: pd.Series,
    atr_series:     pd.Series,
    vol_table:      pd.DataFrame,
    config:         dict,
) -> pd.DataFrame:
    """
    Compute First Hour Breakout signals on 1-hour DataFrame.

    Adds columns:
      fhb_range_high    - High of the 9:30 hourly bar
      fhb_range_low     - Low of the 9:30 hourly bar
      fhb_atr           - ATR(14) value at signal bar (for ATR stop sizing)
      fhb_range_complete- True from 10:30 onwards
      fhb_long_signal   - True on breakout long bar
      fhb_short_signal  - True on breakout short bar
      fhb_htf_blocked   - True if HTF bias blocked the direction
      fhb_vol_blocked   - True if volume filter blocked the signal
    """
    markets_cfg  = config.get("markets", {})
    tick_size    = float(markets_cfg.get(market, {}).get("tick_size", 0.25))
    entry_buffer = FHB_ENTRY_BUF_TICKS * tick_size

    df = df_1h.copy()
    df["fhb_range_high"]     = float("nan")
    df["fhb_range_low"]      = float("nan")
    df["fhb_atr"]            = float("nan")
    df["fhb_range_complete"] = False
    df["fhb_long_signal"]    = False
    df["fhb_short_signal"]   = False
    df["fhb_htf_blocked"]    = False
    df["fhb_vol_blocked"]    = False

    # Merge ATR into the main DataFrame
    df["fhb_atr"] = atr_series.reindex(df.index)

    trading_dates = df.index.normalize().unique()
    total_long = total_short = total_blocked = total_vol_blocked = 0

    for day in trading_dates:
        day_str  = str(pd.Timestamp(day).date())
        day_date = pd.Timestamp(day).date()
        day_mask = df.index.normalize() == day
        day_df   = df[day_mask].copy()

        if len(day_df) < 3:
            continue

        # ---- First-hour bar (9:30 candle) ----------------------------------
        first_hour = day_df[day_df.index.hour == 9]
        if first_hour.empty:
            continue

        range_high = float(first_hour["High"].max())
        range_low  = float(first_hour["Low"].min())
        if range_high <= range_low:
            continue

        # ---- Volume filter -------------------------------------------------
        vol_ok = True
        if FHB_VOLUME_FILTER and day_date in vol_table.index:
            vol_ok = bool(vol_table.loc[day_date, "vol_ok"])
        elif FHB_VOLUME_FILTER and not vol_table.empty:
            # Might be missing early days before 20-day avg is available
            vol_ok = True  # Allow through if no vol history

        # ---- HTF bias ------------------------------------------------------
        try:
            if len(htf_bias_series) > 0:
                bias_str_idx = htf_bias_series.copy()
                bias_str_idx.index = [str(pd.Timestamp(d).date())
                                      for d in htf_bias_series.index]
                prior    = bias_str_idx[bias_str_idx.index <= day_str]
                htf_bias = str(prior.iloc[-1]) if len(prior) > 0 else "NEUTRAL"
            else:
                htf_bias = "NEUTRAL"
        except Exception:
            htf_bias = "NEUTRAL"

        long_allowed  = (htf_bias != "BEAR")
        short_allowed = (htf_bias != "BULL")

        # ---- Mark range complete from 10:30 onwards -------------------------
        try:
            post_range = day_df.between_time(FHB_RANGE_END, "23:59")
            day_df.loc[post_range.index, "fhb_range_complete"] = True
        except Exception:
            pass

        day_df["fhb_range_high"] = range_high
        day_df["fhb_range_low"]  = range_low

        # ---- Tradeable window (10:30 - 13:00) --------------------------------
        try:
            tradeable = day_df.between_time(FHB_RANGE_END, FHB_NO_ENTRY_AFTER)
        except Exception:
            tradeable = day_df[day_df["fhb_range_complete"]]

        long_entry_level  = range_high + entry_buffer
        short_entry_level = range_low  - entry_buffer
        long_fired  = False
        short_fired = False

        for idx in tradeable.index:
            bar = tradeable.loc[idx]

            if not long_fired and bar["Close"] > long_entry_level:
                if not vol_ok:
                    day_df.loc[idx, "fhb_vol_blocked"] = True
                    long_fired = True
                elif long_allowed:
                    day_df.loc[idx, "fhb_long_signal"]  = True
                else:
                    day_df.loc[idx, "fhb_htf_blocked"]  = True
                long_fired = True

            if not short_fired and bar["Close"] < short_entry_level:
                if not vol_ok:
                    day_df.loc[idx, "fhb_vol_blocked"] = True
                    short_fired = True
                elif short_allowed:
                    day_df.loc[idx, "fhb_short_signal"] = True
                else:
                    day_df.loc[idx, "fhb_htf_blocked"]  = True
                short_fired = True

            if long_fired and short_fired:
                break

        df.update(day_df)

        total_long         += int(day_df["fhb_long_signal"].sum())
        total_short        += int(day_df["fhb_short_signal"].sum())
        total_blocked      += int(day_df["fhb_htf_blocked"].sum())
        total_vol_blocked  += int(day_df["fhb_vol_blocked"].sum())

    log.info(
        "{market}: FHB v2 signals | Long={l} Short={s} HTF_blocked={b} Vol_blocked={v} | Days={d}",
        market=market, l=total_long, s=total_short,
        b=total_blocked, v=total_vol_blocked, d=len(trading_dates),
    )
    return df


# ============================================================
# TRADE SIMULATION (Phase 5C — with ATR stop + trail-to-BE)
# ============================================================

def simulate_fhb_trades(
    df_1h:  pd.DataFrame,
    market: str,
    config: dict,
) -> list[dict]:
    """
    Simulate FHB trades bar-by-bar with Phase 5C exit improvements:

    1. ATR stop: stop = entry +/- FHB_ATR_STOP_MULT * ATR14
       (replaces "full range opposite side" from 5B)

    2. Trail to breakeven: after partial_taken=True (50% at 1R),
       effective stop becomes entry price (not original stop).
       This makes the remaining 50% a "free trade" — worst outcome is 0.

    3. Entry:  Open of the next bar after signal (realistic)
    4. Target1: 1R (50% partial exit)
    5. Target2: 2R (remaining 50%)
    6. Time:   Exit remaining at close of bar FHB_MAX_HOLD_BARS after entry
    """
    markets_cfg  = config.get("markets", {})
    mkt_cfg      = markets_cfg.get(market, {})
    point_value  = float(mkt_cfg.get("point_value",   50.0))
    commission   = float(mkt_cfg.get("commission",     5.0))
    slippage_tks = int(  mkt_cfg.get("slippage_ticks", 1))
    tick_size    = float(mkt_cfg.get("tick_size",      0.25))
    slippage_pts = slippage_tks * tick_size

    trades = []
    bars   = df_1h.reset_index()

    for i, row in bars.iterrows():
        is_long  = bool(row.get("fhb_long_signal",  False))
        is_short = bool(row.get("fhb_short_signal", False))
        blocked  = bool(row.get("fhb_htf_blocked",  False))
        vol_blk  = bool(row.get("fhb_vol_blocked",  False))

        if not (is_long or is_short) or blocked or vol_blk:
            continue

        if i + 1 >= len(bars):
            continue

        next_bar   = bars.iloc[i + 1]
        entry_raw  = float(next_bar["Open"])
        range_high = float(row["fhb_range_high"])
        range_low  = float(row["fhb_range_low"])
        range_size = range_high - range_low
        atr_val    = float(row.get("fhb_atr", float("nan")))

        if range_size <= 0:
            continue

        # ---- ATR-based stop (Phase 5C improvement) -------------------------
        # If ATR is available and valid, use it for stop sizing.
        # Otherwise fall back to full range (old 5B behavior).
        if FHB_ATR_STOP_MULT > 0 and not np.isnan(atr_val) and atr_val > 0:
            atr_stop_dist = min(
                FHB_ATR_STOP_MULT * atr_val,
                FHB_ATR_STOP_CAP  * atr_val,  # safety cap
            )
            # Also cap at the full range (never stop beyond range)
            atr_stop_dist = min(atr_stop_dist, range_size)
        else:
            # Fallback: full range as stop
            atr_stop_dist = range_size

        if is_long:
            entry = entry_raw + slippage_pts
            # ATR stop below entry (but no lower than full range low)
            atr_stop  = entry - atr_stop_dist
            range_stop = range_low - slippage_pts
            stop_initial = max(atr_stop, range_stop)  # tighter of the two
        else:
            entry = entry_raw - slippage_pts
            atr_stop  = entry + atr_stop_dist
            range_stop = range_high + slippage_pts
            stop_initial = min(atr_stop, range_stop)

        risk_pts = abs(entry - stop_initial)
        if risk_pts <= 0:
            continue

        target1 = entry + FHB_PARTIAL_R * risk_pts * (1 if is_long else -1)
        target2 = entry + FHB_TARGET_R  * risk_pts * (1 if is_long else -1)

        # ---- Bar-by-bar simulation ------------------------------------------
        partial_taken    = False
        stop             = stop_initial       # live stop, may trail to BE
        final_exit_price = None
        exit_reason      = "time"
        exit_bar_offset  = FHB_MAX_HOLD_BARS

        for j in range(1, FHB_MAX_HOLD_BARS + 1):
            bar_idx = i + 1 + j
            if bar_idx >= len(bars):
                final_exit_price = float(bars.iloc[bar_idx - 1]["Close"])
                exit_reason      = "eod"
                exit_bar_offset  = j
                break

            bar      = bars.iloc[bar_idx]
            bar_high = float(bar["High"])
            bar_low  = float(bar["Low"])

            if is_long:
                # --- Stop check (uses live 'stop' which may be trailed to BE)
                if bar_low <= stop:
                    final_exit_price = stop
                    exit_reason = "stop_partial" if partial_taken else "stop_full"
                    exit_bar_offset  = j
                    break

                # --- 1R partial check
                if not partial_taken and bar_high >= target1:
                    partial_taken = True
                    # TRAIL TO BREAKEVEN (Phase 5C): move stop to entry
                    if FHB_TRAIL_BREAKEVEN:
                        stop = entry  # free trade from here

                # --- 2R full target
                if partial_taken and bar_high >= target2:
                    final_exit_price = target2
                    exit_reason      = "target_full"
                    exit_bar_offset  = j
                    break

            else:  # SHORT
                if bar_high >= stop:
                    final_exit_price = stop
                    exit_reason = "stop_partial" if partial_taken else "stop_full"
                    exit_bar_offset  = j
                    break

                if not partial_taken and bar_low <= target1:
                    partial_taken = True
                    if FHB_TRAIL_BREAKEVEN:
                        stop = entry  # trail to breakeven

                if partial_taken and bar_low <= target2:
                    final_exit_price = target2
                    exit_reason      = "target_full"
                    exit_bar_offset  = j
                    break

        # Time exit: close of bar FHB_MAX_HOLD_BARS
        if final_exit_price is None:
            exit_bar_final = i + 1 + FHB_MAX_HOLD_BARS
            if exit_bar_final < len(bars):
                final_exit_price = float(bars.iloc[exit_bar_final]["Close"])
            else:
                final_exit_price = float(bars.iloc[-1]["Close"])
            exit_reason = "time"

        # ---- P&L calculation -----------------------------------------------
        if partial_taken and exit_reason == "target_full":
            # Both halves hit their targets
            pnl_pts_1 = (target1 - entry) * (1 if is_long else -1) * FHB_PARTIAL_PCT
            pnl_pts_2 = (target2 - entry) * (1 if is_long else -1) * (1 - FHB_PARTIAL_PCT)
            pnl_pts   = pnl_pts_1 + pnl_pts_2
        elif partial_taken and "stop" in exit_reason:
            # Partial taken at 1R, then stopped on remainder at BE (or live stop)
            pnl_pts_1 = (target1 - entry) * (1 if is_long else -1) * FHB_PARTIAL_PCT
            pnl_pts_2 = (final_exit_price - entry) * (1 if is_long else -1) * (1 - FHB_PARTIAL_PCT)
            pnl_pts   = pnl_pts_1 + pnl_pts_2
        else:
            # Full position exit (stop or time before partial)
            pnl_pts = (final_exit_price - entry) * (1 if is_long else -1)

        pnl_gross  = pnl_pts * point_value
        pnl_net    = pnl_gross - (2 * commission)   # round-turn commission
        r_multiple = pnl_pts / risk_pts if risk_pts > 0 else 0.0
        is_win     = pnl_net > 0

        trades.append({
            "date":           pd.Timestamp(row["Timestamp"]).date(),
            "market":         market,
            "direction":      "LONG" if is_long else "SHORT",
            "entry":          round(entry, 4),
            "stop_initial":   round(stop_initial, 4),
            "stop_live":      round(stop, 4),       # final live stop (may = entry)
            "target1":        round(target1, 4),
            "target2":        round(target2, 4),
            "exit_price":     round(final_exit_price, 4),
            "exit_reason":    exit_reason,
            "atr_at_entry":   round(atr_val, 4) if not np.isnan(atr_val) else None,
            "range_size":     round(range_size, 4),
            "risk_pts":       round(risk_pts, 4),
            "pnl_pts":        round(pnl_pts, 4),
            "pnl_gross":      round(pnl_gross, 2),
            "pnl_net":        round(pnl_net, 2),
            "r_multiple":     round(r_multiple, 3),
            "partial_taken":  partial_taken,
            "exit_bars":      exit_bar_offset,
            "is_win":         is_win,
        })

    return trades


# ============================================================
# PERFORMANCE METRICS
# ============================================================

def compute_metrics(trades: list[dict], market: str) -> dict:
    """Full performance metrics from trade list."""
    if not trades:
        return {"market": market, "total_trades": 0, "error": "No trades"}

    df    = pd.DataFrame(trades)
    total = len(df)
    wins  = int(df["is_win"].sum())
    losses = total - wins
    win_rate = wins / total if total > 0 else 0.0

    gross_wins   = df.loc[df["pnl_net"] > 0,  "pnl_net"].sum()
    gross_losses = abs(df.loc[df["pnl_net"] <= 0, "pnl_net"].sum())
    pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    avg_win  = df.loc[df["is_win"],  "pnl_net"].mean() if wins > 0   else 0.0
    avg_loss = df.loc[~df["is_win"], "pnl_net"].mean() if losses > 0 else 0.0
    avg_r    = df["r_multiple"].mean()

    daily_pnl = df.groupby("date")["pnl_net"].sum()
    avg_daily = daily_pnl.mean()
    best_day  = daily_pnl.max()
    worst_day = daily_pnl.min()

    cum_equity = daily_pnl.cumsum()
    roll_max   = cum_equity.cummax()
    drawdown   = cum_equity - roll_max
    max_dd     = float(drawdown.min())

    exit_counts  = df["exit_reason"].value_counts().to_dict()
    partial_rate = df["partial_taken"].mean() * 100
    r_by_exit    = df.groupby("exit_reason")["r_multiple"].mean().round(3).to_dict()

    return {
        "market":           market,
        "total_trades":     total,
        "win_rate_pct":     round(win_rate * 100, 1),
        "profit_factor":    round(pf, 2),
        "total_net_pnl":    round(df["pnl_net"].sum(), 2),
        "avg_daily_pnl":    round(avg_daily, 2),
        "best_day":         round(best_day, 2),
        "worst_day":        round(worst_day, 2),
        "avg_win_usd":      round(avg_win, 2),
        "avg_loss_usd":     round(avg_loss, 2),
        "avg_r_multiple":   round(avg_r, 3),
        "partial_rate_pct": round(partial_rate, 1),
        "max_drawdown_usd": round(max_dd, 2),
        "exit_breakdown":   exit_counts,
        "r_by_exit_type":   r_by_exit,
        "trading_days":     int(daily_pnl.shape[0]),
    }


# ============================================================
# REPORTING
# ============================================================

def yearly_breakdown(trades: list[dict], market: str) -> None:
    if not trades:
        return
    df = pd.DataFrame(trades)
    df["year"] = pd.to_datetime(df["date"]).dt.year

    print(f"\n  {market} -- Year-by-Year Breakdown:")
    print(f"  {'Year':<6} {'Trades':<8} {'Win%':<8} {'PF':<7} {'Net P&L':>12} {'Avg/Day':>10}")
    print("  " + "-" * 57)
    for yr, grp in df.groupby("year"):
        t   = len(grp)
        w   = int(grp["is_win"].sum())
        wr  = w / t * 100 if t > 0 else 0
        gw  = grp.loc[grp["pnl_net"] > 0,  "pnl_net"].sum()
        gl  = abs(grp.loc[grp["pnl_net"] <= 0, "pnl_net"].sum())
        pf  = gw / gl if gl > 0 else float("inf")
        net = grp["pnl_net"].sum()
        days = grp.groupby("date").ngroups
        apd = net / days if days > 0 else 0
        print(f"  {yr:<6} {t:<8} {wr:<8.1f} {pf:<7.2f} ${net:>10,.0f} ${apd:>8,.0f}")


def print_comparison(baseline_metrics: dict, improved_metrics: dict) -> None:
    """Print a side-by-side before/after comparison table."""
    print("\n  BEFORE vs AFTER -- Phase 5B (range stop) vs Phase 5C (ATR stop + trail):")
    print(f"  {'Metric':<22} {'5B Baseline':>14} {'5C Improved':>14} {'Delta':>10}")
    print("  " + "-" * 62)

    keys = [
        ("win_rate_pct",     "Win Rate %",      "{:.1f}%",   "{:+.1f}pp"),
        ("profit_factor",    "Profit Factor",   "{:.2f}",    "{:+.2f}"),
        ("total_net_pnl",    "Total P&L",       "${:,.0f}",  "${:+,.0f}"),
        ("avg_daily_pnl",    "Avg Daily P&L",   "${:,.0f}",  "${:+,.0f}"),
        ("avg_r_multiple",   "Avg R/trade",     "{:.3f}R",   "{:+.3f}R"),
        ("partial_rate_pct", "Partial Rate %",  "{:.1f}%",   "{:+.1f}pp"),
        ("max_drawdown_usd", "Max Drawdown",    "${:,.0f}",  "${:+,.0f}"),
    ]
    for key, label, fmt_val, fmt_delta in keys:
        b = baseline_metrics.get(key, 0) if baseline_metrics else 0
        i = improved_metrics.get(key, 0)
        delta = i - b
        try:
            print(f"  {label:<22} {fmt_val.format(b):>14} {fmt_val.format(i):>14} "
                  f"{fmt_delta.format(delta):>10}")
        except Exception:
            print(f"  {label:<22} {'N/A':>14} {'N/A':>14} {'N/A':>10}")


def print_report(
    metrics_list:    list[dict],
    baseline_list:   list[dict],
    all_trades:      list[dict],
    trading_days:    int,
) -> None:
    print("\n" + "=" * 68)
    print("  AlgoBot FHB Backtest -- Phase 5C Exit Strategy Fix")
    print(f"  Data: Yahoo Finance 1-hour | ~730 days | ES + NQ")
    print("  Improvements: ATR stop + Trail-to-BE + Volume filter")
    print("=" * 68)

    print(f"\n  Dataset: ~{trading_days} trading days")
    print(f"  ATR stop:       {FHB_ATR_STOP_MULT}x ATR(14,1h) -- tighter than range stop")
    print(f"  Trail to BE:    {FHB_TRAIL_BREAKEVEN} -- stop moves to entry after 1R partial")
    print(f"  Volume filter:  {FHB_VOLUME_FILTER} -- min {int(FHB_VOLUME_MIN_PCT*100)}% of 20d avg volume")

    print("\n" + "-" * 68)

    baseline_by_mkt = {m.get("market"): m for m in baseline_list}
    total_pnl_all = 0.0

    for m in metrics_list:
        mkt = m.get("market", "?")
        if m.get("total_trades", 0) == 0:
            print(f"\n  {mkt}: No trades generated")
            continue

        total_pnl_all += m.get("total_net_pnl", 0)
        baseline = baseline_by_mkt.get(mkt, {})

        print(f"\n  {mkt} Performance ({m['total_trades']} trades, ~{m['trading_days']} days):")
        print(f"    Win rate            : {m['win_rate_pct']:.1f}%")
        print(f"    Profit Factor       : {m['profit_factor']:.2f}")
        print(f"    Total net P&L       : ${m['total_net_pnl']:>10,.2f}")
        print(f"    Avg daily P&L       : ${m['avg_daily_pnl']:>10,.2f}")
        print(f"    Best day            : ${m['best_day']:>10,.2f}")
        print(f"    Worst day           : ${m['worst_day']:>10,.2f}")
        print(f"    Max drawdown        : ${m['max_drawdown_usd']:>10,.2f}")
        print(f"    Avg win             : ${m['avg_win_usd']:>10,.2f}")
        print(f"    Avg loss            : ${m['avg_loss_usd']:>10,.2f}")
        print(f"    Avg R per trade     : {m['avg_r_multiple']:>10.3f}R")
        print(f"    Partial exit rate   : {m['partial_rate_pct']:.1f}%")
        print(f"    Exit breakdown      : {m['exit_breakdown']}")
        print(f"    R by exit type      : {m['r_by_exit_type']}")

        if baseline:
            print_comparison(baseline, m)

    if len(metrics_list) > 1 and total_pnl_all != 0:
        print(f"\n  COMBINED (ES + NQ):")
        print(f"    Total net P&L       : ${total_pnl_all:>10,.2f}")
        avg_days = max(m.get("trading_days", 1) for m in metrics_list)
        print(f"    Avg daily P&L       : ${total_pnl_all/avg_days:>10,.2f}")

    print("\n" + "-" * 68)
    print("  EDGE THRESHOLDS:")
    print("  PF > 2.0 AND Win% > 55% -> Strong edge, proceed to paper trade")
    print("  PF 1.5-2.0              -> Good edge, consider Phase 6")
    print("  PF 1.2-1.5              -> Marginal, tune further")
    print("  PF < 1.2                -> Structural problem, rethink")
    print("=" * 68 + "\n")


# ============================================================
# MAIN
# ============================================================

def main():
    print("\n" + "=" * 68)
    print("  AlgoBot FHB Backtest -- Phase 5C Exit Strategy Fix")
    print("  ATR stop + Trail-to-Breakeven + Volume Filter")
    print("=" * 68 + "\n")

    config      = load_config()
    fhb_markets = ["ES", "NQ"]

    # ---- Step 1: Download 1-hour data ----------------------------------------
    print("Step 1: Downloading 1-hour intraday data...")
    intraday_data: dict[str, pd.DataFrame] = {}
    for market in fhb_markets:
        df = download_1h_intraday(market)
        if not df.empty:
            intraday_data[market] = df

    if not intraday_data:
        print("\nERROR: No 1-hour data downloaded. Check internet connection.\n")
        sys.exit(1)

    total_days = max(df.index.normalize().nunique() for df in intraday_data.values())

    # ---- Step 2: Compute ATR and volume tables --------------------------------
    print("\nStep 2: Computing ATR(14) and first-hour volume tables...")
    atr_by_market: dict[str, pd.Series]     = {}
    vol_by_market: dict[str, pd.DataFrame]  = {}
    for market, df_1h in intraday_data.items():
        atr_by_market[market] = compute_1h_atr(df_1h, FHB_ATR_PERIOD)
        vol_by_market[market] = compute_first_hour_volumes(df_1h)
        vol_tbl = vol_by_market[market]
        vol_ok_pct = vol_tbl["vol_ok"].mean() * 100 if len(vol_tbl) > 0 else 100
        print(f"  {market}: ATR(14) latest={atr_by_market[market].iloc[-1]:.2f} pts | "
              f"Volume filter pass rate: {vol_ok_pct:.0f}% of days")

    # ---- Step 3: HTF bias ----------------------------------------------------
    print("\nStep 3: Computing HTF bias from daily bars...")
    htf_bias_map: dict[str, pd.Series] = {}
    for market in fhb_markets:
        if market in intraday_data:
            htf_bias_map[market] = get_htf_bias_series(market, config)

    # ---- Step 4: Compute signals (Phase 5C) ----------------------------------
    print("\nStep 4: Computing FHB signals (with volume filter)...")
    fhb_results: dict[str, pd.DataFrame] = {}
    for market in fhb_markets:
        if market not in intraday_data:
            continue
        df_1h    = intraday_data[market]
        htf_bias = htf_bias_map.get(market, pd.Series(dtype=str))
        atr_ser  = atr_by_market.get(market, pd.Series(dtype=float))
        vol_tbl  = vol_by_market.get(market, pd.DataFrame())

        df_sig = compute_fhb_signals(
            df_1h, market, htf_bias, atr_ser, vol_tbl, config
        )
        fhb_results[market] = df_sig

        longs       = int(df_sig["fhb_long_signal"].sum())
        shorts      = int(df_sig["fhb_short_signal"].sum())
        blocked_htf = int(df_sig["fhb_htf_blocked"].sum())
        blocked_vol = int(df_sig["fhb_vol_blocked"].sum())
        days        = df_sig.index.normalize().nunique()

        print(f"  {market}: {longs} longs, {shorts} shorts | "
              f"HTF blocked: {blocked_htf}, Vol blocked: {blocked_vol} | "
              f"{days} days ({(longs+shorts)/max(days,1):.2f} signals/day)")

    # ---- Step 5: Phase 5B BASELINE simulation (for comparison) ---------------
    # Run 5B simulation (range stop, no trail) on same signals for apples-to-apples
    print("\nStep 5: Simulating Phase 5B BASELINE trades (for comparison)...")
    baseline_trades_by_mkt: dict[str, list[dict]] = {}
    baseline_metrics_list: list[dict] = []

    for market, df_sig in fhb_results.items():
        # Temporarily disable improvements for baseline simulation
        global FHB_ATR_STOP_MULT, FHB_TRAIL_BREAKEVEN, FHB_VOLUME_FILTER
        saved_atr   = FHB_ATR_STOP_MULT
        saved_trail = FHB_TRAIL_BREAKEVEN
        saved_vol   = FHB_VOLUME_FILTER

        FHB_ATR_STOP_MULT   = 0       # Full range stop (5B behavior)
        FHB_TRAIL_BREAKEVEN = False   # No trail
        FHB_VOLUME_FILTER   = False   # No volume filter

        baseline_trades = simulate_fhb_trades(df_sig, market, config)
        baseline_trades_by_mkt[market] = baseline_trades
        baseline_metrics_list.append(compute_metrics(baseline_trades, market))

        FHB_ATR_STOP_MULT   = saved_atr
        FHB_TRAIL_BREAKEVEN = saved_trail
        FHB_VOLUME_FILTER   = saved_vol

        print(f"  {market} baseline: {len(baseline_trades)} trades")

    # ---- Step 6: Phase 5C IMPROVED simulation --------------------------------
    print("\nStep 6: Simulating Phase 5C IMPROVED trades...")
    all_trades:   list[dict] = []
    metrics_list: list[dict] = []

    for market, df_sig in fhb_results.items():
        trades  = simulate_fhb_trades(df_sig, market, config)
        all_trades.extend(trades)
        metrics = compute_metrics(trades, market)
        metrics_list.append(metrics)
        print(f"  {market}: {len(trades)} trades (vs {len(baseline_trades_by_mkt[market])} baseline)")

    # ---- Step 7: Full report -------------------------------------------------
    print_report(metrics_list, baseline_metrics_list, all_trades, total_days)

    # ---- Step 8: Year-by-year breakdown --------------------------------------
    for market in fhb_markets:
        mkt_trades = [t for t in all_trades if t["market"] == market]
        yearly_breakdown(mkt_trades, market)
    print()

    # ---- Step 9: Save CSV ----------------------------------------------------
    if all_trades:
        reports_dir = PROJECT_ROOT / "reports" / "backtests"
        reports_dir.mkdir(parents=True, exist_ok=True)
        ts       = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        csv_path = reports_dir / f"fhb_5c_backtest_{ts}.csv"
        pd.DataFrame(all_trades).to_csv(csv_path, index=False)
        print(f"  Trade log saved: {csv_path.name}\n")

    return metrics_list


if __name__ == "__main__":
    main()
