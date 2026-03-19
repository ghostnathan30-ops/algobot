"""
AlgoBot -- First Hour Breakout (FHB) Backtest Script
======================================================
Script:  scripts/run_fhb_backtest.py
Phase:   5D -- Institutional Filters + Overnight Carry
Purpose: Full FHB backtest with all quality filters applied.

PHASE 5C IMPROVEMENTS (exit strategy):
  1. ATR-based stop (0.75 * ATR14 on 1h bars) -- tighter risk
  2. Trail stop to breakeven after 1R partial exit -- free trade on remainder

PHASE 5D IMPROVEMENTS (entry quality + holding):
  3. Economic Calendar filter -- skip HIGH_IMPACT news days (NFP, FOMC)
     Academic basis: Andersen (2003), Lucca (2015) -- news releases cause
     spike-and-reversal in first hour that stops out breakout strategies.
  4. VIX Regime filter -- skip QUIET (<13) and CRISIS (>35) days
     QUIET: ranges too tight (false breakouts); CRISIS: gap risk, halts.
     Half size on ELEVATED (28-35) days.
  5. Green Light Score (0-100) -- composite quality gate combining regime,
     HTF bias alignment, VIX, news, and time of day into a single score.
     Score < 40 -> skip. Score 40-59 -> half size. Score >= 60 -> full size.
  6. Overnight carry -- if a trade is +0.5R or better at market close,
     no high-impact news tomorrow, and VIX is OPTIMAL/ELEVATED, carry the
     position overnight and manage it next day. Stop is already at breakeven.
     Maximum hold: FHB_OVERNIGHT_MAX_BARS additional bars (2 trading days).

MULTI-DAY / MULTI-WEEK TRADING:
  FHB overnight carry enables 2-5 day holds on strong trending trades.
  Swing strategy (BacktestEngine) already holds positions for weeks/months.
  Combined: FHB handles daily breakouts, swing handles major trend rides.

Architecture:
  - Signal detection: compute_fhb_signals() -- SAME logic, adds GLS metadata
  - Trade simulation: simulate_fhb_trades() -- all Phase 5C+5D improvements
  - Database logging: TradeDB (SQLite) -- persistent record for Phase 6

Run:
    cd AlgoBot
    conda run -n algobot_env python scripts/run_fhb_backtest.py
"""

from __future__ import annotations

import collections
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
from src.utils.econ_calendar import EconCalendar
from src.utils.vix_filter import VIXFilter
from src.utils.trade_readiness import GreenLightScore
from src.utils.trade_db import TradeDB
from src.utils.orderflow import add_daily_vwap, add_synthetic_delta, vwap_signal_aligned, delta_signal_aligned
from src.utils.logger import get_logger

log = get_logger(__name__)

# ============================================================
# STRATEGY PARAMETERS
# ============================================================

FHB_RANGE_START     = "09:30"   # First hour bar open
FHB_RANGE_END       = "10:30"   # End of first-hour range window
FHB_NO_ENTRY_AFTER  = "13:00"   # No new entries after 1 PM ET
FHB_MAX_HOLD_BARS   = 5         # Max same-day hold: 5 hourly bars from entry
FHB_PARTIAL_R       = 0.7       # Partial exit at 0.7R (50% of position) -- P2: faster BE trail
FHB_TARGET_R        = 2.5       # Full target at 2.5R (raised from 2.0 — free trade after partial)
FHB_PARTIAL_PCT     = 0.50      # 50% partial exit fraction
FHB_ENTRY_BUF_TICKS = 1         # 1 tick buffer above/below range boundary

# Phase 5C -- exit improvements
FHB_ATR_PERIOD      = 14        # ATR period (Wilder's) on 1-hour bars
FHB_ATR_STOP_MULT   = 0.75      # Stop = entry +/- 0.75 * ATR14
FHB_ATR_STOP_CAP    = 1.0       # Cap stop at 1x ATR (never wider)
FHB_TRAIL_BREAKEVEN = True      # Move stop to entry after 1R partial fires

# Phase 5D -- entry quality filters
FHB_SKIP_HIGH_IMPACT = True     # Skip NFP, FOMC days (EconCalendar)
FHB_SKIP_MEDIUM_IMPACT = False  # Don't skip CPI/GDP (too many days)
FHB_VIX_FILTER       = True     # Apply VIX regime gating
FHB_GLS_MIN_SCORE    = 40       # Skip if GreenLightScore < 40
FHB_GLS_HALF_SCORE   = 65       # Half size if GreenLightScore < 65

# Phase 5D -- overnight carry
FHB_OVERNIGHT_CARRY     = True  # Allow carrying profitable trades overnight
FHB_OVERNIGHT_MIN_R     = 0.4   # Must be at least +0.4R to carry overnight (lowered to give more trades time to reach 2.5R)
FHB_OVERNIGHT_MAX_BARS  = 10    # Max additional bars if carrying overnight (~2 days)

# Phase 5E -- order flow filters (VWAP + synthetic delta)
FHB_VWAP_FILTER         = True  # Require price on correct side of daily VWAP at signal
FHB_DELTA_FILTER        = True  # Require cumulative delta to confirm direction
FHB_DELTA_STRICT        = False # If True, BOTH cum_delta AND bar_delta must confirm

# ── Priority 1: Hard loss cap + partial stop ──────────────────────────────────
# 1A: If ATR stop implies dollar risk > FHB_MAX_LOSS_USD, tighten stop to cap.
#     Protects Topstep account -- single trade can never bust the daily limit.
#     Not curve-fitted: $2,000 is derived from Topstep rules ($3,000/day limit,
#     $1,000 buffer), NOT from inspecting which trades were most profitable.
# 1B: For trades that hit the full stop (stop_full), 50% exits at -HALF_STOP_R
#     and 50% exits at -1R.  With HALF_STOP_R=0.30: avg full-stop loss = -0.65R
#     (was -0.75R at 0.50R).  Reduces loss on every stop_full by ~0.10R.
# P2: FHB_PARTIAL_R lowered from 1.0 to 0.7 -- trail stop to breakeven at 0.7R
#     instead of 1.0R.  Converts some stop_full into stop_partial (profitable).
FHB_MAX_LOSS_USD = 2_000   # Hard cap: max dollar risk per trade
FHB_HALF_STOP_R  = 0.20    # Partial stop fraction (R) for stop_full improvement -- P2

# Overhaul: Opening Range Expansion filter + Fast Bear detection
FHB_RANGE_EXPAND_MULT = 1.5   # Skip if 1h range > 1.5x 20-day rolling avg (chaotic open days)
FHB_RANGE_LOOKBACK    = 20    # Rolling window for avg range computation
FHB_FAST_BIAS         = True  # Enable medium-term bear detection (daily EMA10 vs EMA20)

# Gap-open filter: skip FHB if today's open gaps >0.5% from yesterday's close.
# Headline-driven gaps (tariffs, Fed surprises) create chaotic first-hour action
# that produces false breakouts after an already-extended open.
FHB_GAP_FILTER    = True    # Enable gap-open skip
FHB_GAP_THRESHOLD = 0.005   # 0.5% gap threshold

# RTY quality gate: RTY only has edge in TRENDING and TRANSITIONING regimes.
# In RANGING regime (48% of RTY days), PF=1.16 — barely positive.
FHB_RTY_TREND_ONLY = True   # Skip RTY trades when regime=RANGING

# Selective scaling: NQ gets 1.5x size when HTF=BULL AND fast=BULL AND direction=LONG.
# All three alignment layers confirm: weekly trend, daily momentum, signal direction.
# In live trading this maps to 1→2 contracts at peak conviction.
FHB_NQ_BULL_BOOST = True    # Enable NQ conviction scaling
FHB_NQ_BULL_MULT  = 1.5     # Multiplier (1.5x models trading 2 instead of 1 contract)


# ============================================================
# DATA LOADING
# ============================================================

def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def download_1h_intraday(market: str) -> pd.DataFrame:
    """Download 730 days of 1-hour RTH data from Yahoo Finance."""
    import time as _time
    import yfinance as yf

    yf_tickers = {
        "ES":  "ES=F",
        "NQ":  "NQ=F",
        "GC":  "GC=F",
        "CL":  "CL=F",
        "ZB":  "ZB=F",
        "6E":  "6E=F",
        "RTY": "RTY=F",
        "YM":  "YM=F",
    }
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

    print(f"  {market}: Downloading 730 days of 1-hour data...")
    try:
        raw = yf.download(ticker, period="730d", interval="1h",
                          auto_adjust=True, progress=False, timeout=30)
        if raw is None or raw.empty:
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
        print(f"  {market}: {len(df)} bars | "
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


def get_htf_data(market: str, config: dict) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute HTF combined bias, daily regime, and fast daily bias from ~7 years of daily data.

    Returns:
        (htf_bias_series, regime_series, fast_bias_series) -- all indexed by date (normalized)
    """
    print(f"  HTF data for {market}...")
    raw = download_market(market, "2019-01-01", "2025-12-31")
    if raw is None or raw.empty:
        return pd.Series(dtype=str), pd.Series(dtype=str), pd.Series(dtype=str)

    strat_cfg  = config.get("strategy", config)
    regime_cfg = config.get("regime", strat_cfg)
    df = calculate_indicators(raw, strat_cfg, market)
    df = add_atr_baseline(df)
    df = classify_regimes(df, regime_cfg, market)
    df = add_htf_bias(df, config, market)

    bias = df["htf_combined_bias"].copy()
    bias.index = pd.to_datetime(bias.index).normalize()

    regime = df["regime"].copy()
    regime.index = pd.to_datetime(regime.index).normalize()

    fast_bias = df["fast_bias"].copy() if "fast_bias" in df.columns else pd.Series(dtype=str)
    fast_bias.index = pd.to_datetime(fast_bias.index).normalize()

    return bias, regime, fast_bias


# ============================================================
# ATR COMPUTATION
# ============================================================

def compute_1h_atr(df: pd.DataFrame, period: int = FHB_ATR_PERIOD) -> pd.Series:
    """Wilder's ATR on 1-hour bars. Returns Series aligned to df.index."""
    h      = df["High"]
    lo     = df["Low"]
    prev_c = df["Close"].shift(1)
    tr = pd.concat(
        [h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(span=period, min_periods=period, adjust=False).mean()


# ============================================================
# SIGNAL COMPUTATION (core logic identical to Phase 5B/5C)
# ============================================================

def compute_fhb_signals(
    df_1h:            pd.DataFrame,
    market:           str,
    htf_bias_series:  pd.Series,
    regime_series:    pd.Series,
    config:           dict,
    econ_cal:         EconCalendar    = None,
    vix_filter:       VIXFilter       = None,
    gls_engine:       GreenLightScore = None,
    fast_bias_series: pd.Series       = None,
) -> pd.DataFrame:
    """
    Compute First Hour Breakout signals on 1-hour DataFrame.

    Phase 5D adds per-signal metadata columns (GLS score, filter reasons)
    but does NOT change the raw signal detection logic. The simulation layer
    uses these metadata columns to gate entries and size positions.

    Added columns vs Phase 5B:
      fhb_econ_impact   - 'NONE', 'MEDIUM', 'HIGH'
      fhb_vix_regime    - 'QUIET', 'OPTIMAL', 'ELEVATED', 'CRISIS'
      fhb_daily_regime  - Daily regime from HTF daily data
      fhb_gls_score     - Green Light Score (0-100) for signal bar
      fhb_gls_action    - 'FULL_SIZE', 'HALF_SIZE', 'SKIP'
      fhb_filter_reason - Human-readable reason if signal is filtered
    """
    markets_cfg  = config.get("markets", {})
    tick_size    = float(markets_cfg.get(market, {}).get("tick_size", 0.25))
    entry_buffer = FHB_ENTRY_BUF_TICKS * tick_size

    df = df_1h.copy()
    # Phase 5E: Add VWAP and synthetic delta to the full dataframe first
    df = add_daily_vwap(df)
    df = add_synthetic_delta(df, reset_daily=True)

    df["fhb_range_high"]     = float("nan")
    df["fhb_range_low"]      = float("nan")
    df["fhb_range_complete"] = False
    df["fhb_long_signal"]    = False
    df["fhb_short_signal"]   = False
    df["fhb_htf_blocked"]    = False
    # Phase 5D metadata
    df["fhb_econ_impact"]    = "NONE"
    df["fhb_vix_regime"]     = "OPTIMAL"
    df["fhb_daily_regime"]   = "TRENDING"
    df["fhb_gls_score"]      = 0
    df["fhb_gls_action"]     = "SKIP"
    df["fhb_filter_reason"]  = ""
    # Phase 5E metadata
    df["fhb_vwap"]           = float("nan")
    df["fhb_vwap_aligned"]   = True
    df["fhb_delta_aligned"]  = True
    df["fhb_of_score"]       = 0    # 0=no confirm, 1=partial, 2=both confirm
    # Overhaul metadata
    df["fhb_range_filtered"] = False
    df["fhb_gap_filtered"]   = False
    df["fhb_htf_bias"]       = "NEUTRAL"
    df["fhb_fast_bias"]      = "NEUTRAL"

    trading_dates = df.index.normalize().unique()
    total_long = total_short = total_blocked = 0
    econ_skipped = vix_skipped = gls_skipped = 0
    range_deque = collections.deque(maxlen=FHB_RANGE_LOOKBACK)
    prev_close  = None   # tracks yesterday's close for gap-open filter

    for day in trading_dates:
        day_str  = str(pd.Timestamp(day).date())
        day_mask = df.index.normalize() == day
        day_df   = df[day_mask].copy()

        # Compute today's close early (used by gap filter and to update prev_close)
        today_last_close = float(day_df.iloc[-1]["Close"]) if not day_df.empty else None

        if len(day_df) < 3:
            prev_close = today_last_close
            continue

        # ── Phase 5D: Economic Calendar check ────────────────────────────────
        econ_impact = "NONE"
        if econ_cal is not None:
            econ_impact = econ_cal.get_impact_level(day_str)

        # ── Phase 5D: VIX regime for this day ────────────────────────────────
        vix_regime = "OPTIMAL"
        if vix_filter is not None:
            vix_regime = vix_filter.get_regime(day_str)

        # ── Phase 5D: Daily regime from HTF data ──────────────────────────────
        daily_regime = "TRENDING"
        if len(regime_series) > 0:
            try:
                reg_idx = regime_series.copy()
                reg_idx.index = pd.to_datetime(reg_idx.index).normalize()
                prior_reg = reg_idx[reg_idx.index <= pd.Timestamp(day_str)]
                if len(prior_reg) > 0:
                    daily_regime = str(prior_reg.iloc[-1])
            except Exception:
                pass

        # ── HTF bias ──────────────────────────────────────────────────────────
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

        # ── Gap-open filter ───────────────────────────────────────────────────
        # Skip if today's open is >0.5% from yesterday's close. These headline-
        # driven gaps (tariff/Fed surprises) produce false first-hour breakouts
        # because the market is already displaced before the range even forms.
        if FHB_GAP_FILTER and prev_close is not None and not day_df.empty:
            today_open = float(day_df.iloc[0]["Open"])
            if today_open > 0 and prev_close > 0:
                gap_pct = abs(today_open - prev_close) / prev_close
                if gap_pct > FHB_GAP_THRESHOLD:
                    day_df["fhb_gap_filtered"] = True
                    df.update(day_df)
                    prev_close = today_last_close
                    continue

        # ── Fast daily bias (EMA10 vs EMA20) ─────────────────────────────────
        fast_bias_val = "NEUTRAL"
        if FHB_FAST_BIAS and fast_bias_series is not None and len(fast_bias_series) > 0:
            try:
                fb_idx = fast_bias_series.copy()
                fb_idx.index = [str(pd.Timestamp(d).date()) for d in fast_bias_series.index]
                prior_fb = fb_idx[fb_idx.index <= day_str]
                fast_bias_val = str(prior_fb.iloc[-1]) if len(prior_fb) > 0 else "NEUTRAL"
            except Exception:
                pass

        # ── First-hour bar ────────────────────────────────────────────────────
        # Yahoo Finance 1h bars are clock-aligned: 10:00, 11:00, 12:00...
        # The 10:00 bar = opening period (9:30-10:30 ET).
        # Use between_time("09:30","10:30", inclusive="left") to capture it.
        try:
            first_hour = day_df.between_time(FHB_RANGE_START, FHB_RANGE_END, inclusive="left")
        except Exception:
            continue
        if first_hour.empty:
            continue

        range_high = float(first_hour["High"].max())
        range_low  = float(first_hour["Low"].min())
        if range_high <= range_low:
            continue

        range_size = range_high - range_low

        # ── Range expansion filter ────────────────────────────────────────────
        # Skip chaotic open days where 1h range > 1.5x 20-day rolling average.
        # Hypothesis: news-driven opens (tariff headlines, FOMC) expand the
        # first-hour range and produce false breakouts.
        range_filtered_day = False
        if FHB_RANGE_EXPAND_MULT > 0 and len(range_deque) >= FHB_RANGE_LOOKBACK:
            avg_range = sum(range_deque) / len(range_deque)
            if avg_range > 0 and range_size > FHB_RANGE_EXPAND_MULT * avg_range:
                range_filtered_day = True
        range_deque.append(range_size)
        if range_filtered_day:
            day_df["fhb_range_filtered"] = True
            df.update(day_df)
            prev_close = today_last_close
            continue

        # Mark range complete from 10:30 onwards
        try:
            post_range = day_df.between_time(FHB_RANGE_END, "23:59")
            day_df.loc[post_range.index, "fhb_range_complete"] = True
        except Exception:
            pass

        day_df["fhb_range_high"]   = range_high
        day_df["fhb_range_low"]    = range_low
        day_df["fhb_econ_impact"]  = econ_impact
        day_df["fhb_vix_regime"]   = vix_regime
        day_df["fhb_daily_regime"] = daily_regime
        day_df["fhb_htf_bias"]     = htf_bias
        day_df["fhb_fast_bias"]    = fast_bias_val

        # ── Tradeable window: 10:30 to 1:00 PM ────────────────────────────────
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
            bar_hour   = int(pd.Timestamp(idx).hour)
            bar_minute = int(pd.Timestamp(idx).minute)

            for direction, is_triggered in [
                ("LONG",  (not long_fired  and bar["Close"] > long_entry_level)),
                ("SHORT", (not short_fired and bar["Close"] < short_entry_level)),
            ]:
                if not is_triggered:
                    continue

                if direction == "LONG":
                    long_fired = True
                    allowed    = long_allowed
                else:
                    short_fired = True
                    allowed     = short_allowed

                if not allowed:
                    day_df.loc[idx, "fhb_htf_blocked"] = True
                    total_blocked += 1
                    continue

                # ── Phase 5D: GreenLightScore ──────────────────────────────
                gls_score  = 0
                gls_action = "FULL_SIZE"
                filter_reason = ""

                if gls_engine is not None:
                    gls_result = gls_engine.compute(
                        regime=daily_regime,
                        htf_bias=htf_bias,
                        signal_direction=direction,
                        vix_regime=vix_regime,
                        econ_impact=econ_impact,
                        bar_hour=bar_hour,
                        bar_minute=bar_minute,
                    )
                    gls_score  = gls_result.score
                    gls_action = gls_result.action
                    if not gls_result.should_trade:
                        filter_reason = gls_result.flags.get("override", f"GLS={gls_score}")

                day_df.loc[idx, "fhb_gls_score"]     = gls_score
                day_df.loc[idx, "fhb_gls_action"]    = gls_action
                day_df.loc[idx, "fhb_filter_reason"] = filter_reason

                # ── Phase 5E: Order Flow alignment (VWAP + synthetic delta) ──
                vwap_aligned  = True
                delta_aligned = True
                of_score      = 2   # assume full confirm until checked

                if FHB_VWAP_FILTER and "vwap" in day_df.columns:
                    vwap_aligned = vwap_signal_aligned(bar, direction)

                if FHB_DELTA_FILTER and "cum_delta" in day_df.columns:
                    delta_aligned = delta_signal_aligned(
                        bar, direction, require_both=FHB_DELTA_STRICT
                    )

                of_score = int(vwap_aligned) + int(delta_aligned)
                day_df.loc[idx, "fhb_vwap"]          = bar.get("vwap", float("nan"))
                day_df.loc[idx, "fhb_vwap_aligned"]  = vwap_aligned
                day_df.loc[idx, "fhb_delta_aligned"] = delta_aligned
                day_df.loc[idx, "fhb_of_score"]      = of_score

                if direction == "LONG":
                    day_df.loc[idx, "fhb_long_signal"] = True
                    total_long += 1
                else:
                    day_df.loc[idx, "fhb_short_signal"] = True
                    total_short += 1

            if long_fired and short_fired:
                break

        df.update(day_df)
        prev_close = today_last_close

    log.info(
        "{market}: FHB signals | Long={l} Short={s} HTF_blocked={b} | "
        "EconSkipped={e} VixSkipped={v} GLSSkipped={g} | Days={d}",
        market=market, l=total_long, s=total_short, b=total_blocked,
        e=econ_skipped, v=vix_skipped, g=gls_skipped, d=len(trading_dates),
    )
    return df


# ============================================================
# TRADE SIMULATION (Phase 5C + 5D)
# ============================================================

def simulate_fhb_trades(
    df_1h:          pd.DataFrame,
    atr_series:     pd.Series,
    market:         str,
    config:         dict,
    use_atr_stop:   bool  = True,
    trail_be:       bool  = True,
    overnight_carry: bool = True,
    label:          str   = "5D",
    db:             TradeDB = None,
) -> list[dict]:
    """
    Simulate FHB trades with Phase 5C + 5D improvements.

    Phase 5C:  ATR-based stop, trail to breakeven
    Phase 5D:  GreenLightScore gating, VIX sizing, econ filter, overnight carry

    Multi-day holding:
      If overnight_carry=True and a trade is +FHB_OVERNIGHT_MIN_R at close:
        - No high-impact news tomorrow
        - VIX not CRISIS
        - Stop already at or above breakeven
        -> Carry up to FHB_OVERNIGHT_MAX_BARS additional bars (next 1-2 days)
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
    atr_arr = atr_series.reindex(df_1h.index).values

    # Pre-build a date -> next_date lookup for overnight carry check
    trading_dates = sorted(df_1h.index.normalize().unique())
    next_date_map = {}
    for k in range(len(trading_dates) - 1):
        next_date_map[str(trading_dates[k].date())] = str(trading_dates[k + 1].date())

    for i, row in bars.iterrows():
        is_long  = bool(row.get("fhb_long_signal",  False))
        is_short = bool(row.get("fhb_short_signal", False))
        blocked  = bool(row.get("fhb_htf_blocked",  False))

        if not (is_long or is_short) or blocked:
            continue
        if i + 1 >= len(bars):
            continue

        # ── Phase 5D: GLS gate ────────────────────────────────────────────────
        gls_score  = int(row.get("fhb_gls_score",  100))
        gls_action = str(row.get("fhb_gls_action", "FULL_SIZE"))
        econ_impact = str(row.get("fhb_econ_impact", "NONE"))

        # Hard skip if filtered
        if gls_action == "SKIP":
            continue

        # Size multiplier from GLS
        if gls_action == "HALF_SIZE":
            size_mult = 0.5
        else:
            size_mult = 1.0

        # Additional hard override: skip HIGH_IMPACT news days
        if FHB_SKIP_HIGH_IMPACT and econ_impact == "HIGH":
            continue

        # Hard skip: HIGH_VOL regime kills FHB edge (NQ PF=0.49 empirically)
        daily_regime = str(row.get("fhb_daily_regime", "TRENDING"))
        if daily_regime == "HIGH_VOL":
            continue

        # RTY regime quality gate: RTY only has edge in TRENDING/TRANSITIONING.
        # In RANGING (48% of RTY days), PF=1.16 — below threshold for live trading.
        if FHB_RTY_TREND_ONLY and market == "RTY" and daily_regime == "RANGING":
            continue

        # ── Phase 5E: Order flow gate (VWAP + synthetic delta) ───────────────
        direction_str = "LONG" if is_long else "SHORT"
        vwap_aligned  = bool(row.get("fhb_vwap_aligned",  True))
        delta_aligned = bool(row.get("fhb_delta_aligned", True))
        of_score      = int(row.get("fhb_of_score", 2))

        if FHB_VWAP_FILTER and not vwap_aligned:
            continue   # Price on wrong side of VWAP -- skip this signal

        if FHB_DELTA_FILTER and not delta_aligned:
            # Reduce to half size rather than full skip for delta misalignment
            size_mult = size_mult * 0.5

        # ── Fast bias size adjustment ─────────────────────────────────────────
        # Applied on top of GLS/OF sizing. Never overrides the HTF hard block
        # (that already prevented the signal from firing). Only modulates size.
        htf_b  = str(row.get("fhb_htf_bias",  "NEUTRAL"))
        fast_b = str(row.get("fhb_fast_bias", "NEUTRAL"))
        if FHB_FAST_BIAS:
            if htf_b == "BULL" and fast_b == "BEAR" and direction_str == "LONG":
                size_mult *= 0.75   # Early caution: weekly still BULL but daily turning
            elif htf_b == "NEUTRAL" and fast_b == "BEAR" and direction_str == "LONG":
                size_mult *= 0.5    # Soft warning: neutral weekly, bear daily — fade longs
            elif htf_b == "NEUTRAL" and fast_b == "BULL" and direction_str == "SHORT":
                size_mult *= 0.5    # Soft warning: neutral weekly, bull daily — fade shorts

        # ── NQ selective scaling: full BULL conviction ────────────────────────
        # When HTF=BULL (weekly), fast=BULL (daily), and signal is LONG, all
        # three layers agree. Scale up to 1.5x — models going 1→2 contracts at
        # peak conviction. Cap at FHB_NQ_BULL_MULT to prevent over-sizing.
        if FHB_NQ_BULL_BOOST and market == "NQ" and direction_str == "LONG":
            if htf_b == "BULL" and fast_b == "BULL":
                size_mult = min(size_mult * FHB_NQ_BULL_MULT, FHB_NQ_BULL_MULT)

        next_bar   = bars.iloc[i + 1]
        entry_raw  = float(next_bar["Open"])
        range_high = float(row["fhb_range_high"])
        range_low  = float(row["fhb_range_low"])
        range_size = range_high - range_low
        atr_val    = float(atr_arr[i]) if i < len(atr_arr) and not np.isnan(atr_arr[i]) else 0.0

        if range_size <= 0:
            continue

        # ── Stop sizing ────────────────────────────────────────────────────────
        if use_atr_stop and atr_val > 0:
            atr_stop_dist = min(
                FHB_ATR_STOP_MULT * atr_val,
                FHB_ATR_STOP_CAP  * atr_val,
                range_size,
            )
        else:
            atr_stop_dist = range_size

        if is_long:
            entry        = entry_raw + slippage_pts
            stop_initial = entry - atr_stop_dist
            stop_initial = max(stop_initial, range_low - slippage_pts)
        else:
            entry        = entry_raw - slippage_pts
            stop_initial = entry + atr_stop_dist
            stop_initial = min(stop_initial, range_high + slippage_pts)

        risk_pts = abs(entry - stop_initial)
        if risk_pts <= 0:
            continue

        # Priority 1A: Hard dollar loss cap -- tighten stop if ATR stop is too wide
        max_risk_pts = FHB_MAX_LOSS_USD / point_value
        if risk_pts > max_risk_pts:
            risk_pts = max_risk_pts
            stop_initial = (entry - risk_pts) if is_long else (entry + risk_pts)

        target1 = entry + FHB_PARTIAL_R * risk_pts * (1 if is_long else -1)
        target2 = entry + FHB_TARGET_R  * risk_pts * (1 if is_long else -1)

        # ── Signal metadata for DB logging ────────────────────────────────────
        signal_date     = str(pd.Timestamp(row["Timestamp"]).date())
        signal_id       = f"FHB_{market}_{label}_{signal_date}_{i}"
        vix_regime      = str(row.get("fhb_vix_regime",    "OPTIMAL"))
        daily_regime    = str(row.get("fhb_daily_regime",  "TRENDING"))
        htf_bias_val    = str(row.get("fhb_daily_regime",  "NEUTRAL"))

        # ── Bar-by-bar simulation (max hold = same-day + optional overnight) ──
        partial_taken    = False
        stop             = stop_initial
        final_exit_price = None
        exit_reason      = "time"
        exit_bar_offset  = FHB_MAX_HOLD_BARS
        is_overnight     = False
        is_multiday      = False
        carrying_overnight = False

        # Total bars to scan = same-day + overnight extension (if enabled)
        max_bars = FHB_MAX_HOLD_BARS + (FHB_OVERNIGHT_MAX_BARS if overnight_carry else 0)

        for j in range(1, max_bars + 1):
            bar_idx = i + 1 + j
            if bar_idx >= len(bars):
                final_exit_price = float(bars.iloc[bar_idx - 1]["Close"])
                exit_reason      = "eod"
                exit_bar_offset  = j
                break

            bar      = bars.iloc[bar_idx]
            bar_high = float(bar["High"])
            bar_low  = float(bar["Low"])
            bar_date = str(pd.Timestamp(bar["Timestamp"]).date())

            # Track if we've crossed into a new trading day
            if bar_date != signal_date:
                is_overnight = True
            if j > FHB_MAX_HOLD_BARS + 7:   # more than ~1.5 trading days out
                is_multiday = True

            if is_long:
                # Stop check
                if bar_low <= stop:
                    final_exit_price = stop
                    exit_reason      = "stop_partial" if partial_taken else "stop_full"
                    exit_bar_offset  = j
                    break
                # 1R partial
                if not partial_taken and bar_high >= target1:
                    partial_taken = True
                    if trail_be:
                        stop = entry   # trail to breakeven -- now a free trade
                # 2R full target
                if partial_taken and bar_high >= target2:
                    final_exit_price = target2
                    exit_reason      = "target_full"
                    exit_bar_offset  = j
                    break

                # Same-day time exit: check if we should carry overnight
                if j == FHB_MAX_HOLD_BARS and overnight_carry and final_exit_price is None:
                    current_pnl_r = (bar["Close"] - entry) / risk_pts if risk_pts > 0 else 0
                    tomorrow_str  = next_date_map.get(signal_date, "")
                    tomorrow_news = "NONE"
                    # Check conditions for overnight carry
                    can_carry = (
                        current_pnl_r >= FHB_OVERNIGHT_MIN_R
                        and partial_taken            # stop is at breakeven
                        and vix_regime in ("OPTIMAL", "ELEVATED")
                        and tomorrow_news != "HIGH"
                    )
                    if not can_carry:
                        # Forced time exit at end of day
                        final_exit_price = float(bar["Close"])
                        exit_reason      = "time"
                        exit_bar_offset  = j
                        break
                    else:
                        carrying_overnight = True   # let loop continue past max_bars

            else:  # SHORT
                if bar_high >= stop:
                    final_exit_price = stop
                    exit_reason      = "stop_partial" if partial_taken else "stop_full"
                    exit_bar_offset  = j
                    break
                if not partial_taken and bar_low <= target1:
                    partial_taken = True
                    if trail_be:
                        stop = entry
                if partial_taken and bar_low <= target2:
                    final_exit_price = target2
                    exit_reason      = "target_full"
                    exit_bar_offset  = j
                    break

                if j == FHB_MAX_HOLD_BARS and overnight_carry and final_exit_price is None:
                    current_pnl_r = (entry - bar["Close"]) / risk_pts if risk_pts > 0 else 0
                    can_carry = (
                        current_pnl_r >= FHB_OVERNIGHT_MIN_R
                        and partial_taken
                        and vix_regime in ("OPTIMAL", "ELEVATED")
                    )
                    if not can_carry:
                        final_exit_price = float(bar["Close"])
                        exit_reason      = "time"
                        exit_bar_offset  = j
                        break
                    else:
                        carrying_overnight = True

        # Fallback time exit
        if final_exit_price is None:
            exit_bar_final = i + 1 + FHB_MAX_HOLD_BARS
            if exit_bar_final < len(bars):
                final_exit_price = float(bars.iloc[exit_bar_final]["Close"])
            else:
                final_exit_price = float(bars.iloc[-1]["Close"])
            exit_reason = "overnight_time" if carrying_overnight else "time"

        # ── P&L (accounts for partial exit at 1R and priority-1 half-stop) ───────
        if partial_taken and exit_reason == "target_full":
            # 50% at 1R, 50% at 2R
            pnl_pts_1 = (target1 - entry) * (1 if is_long else -1) * FHB_PARTIAL_PCT
            pnl_pts_2 = (target2 - entry) * (1 if is_long else -1) * (1 - FHB_PARTIAL_PCT)
            pnl_pts   = pnl_pts_1 + pnl_pts_2
        elif partial_taken and "stop" in exit_reason:
            # 50% at 1R, remaining 50% stopped (at breakeven after trail)
            pnl_pts_1 = (target1 - entry) * (1 if is_long else -1) * FHB_PARTIAL_PCT
            pnl_pts_2 = (final_exit_price - entry) * (1 if is_long else -1) * (1 - FHB_PARTIAL_PCT)
            pnl_pts   = pnl_pts_1 + pnl_pts_2
        elif exit_reason == "stop_full":
            # Priority 1B: 50% exits at -0.5R (half stop), 50% at full stop (-1R)
            # Reduces average full-stop loss from -1.0R to -0.75R.
            sign    = 1 if is_long else -1
            half_px = entry - FHB_HALF_STOP_R * risk_pts * sign  # price at -0.5R
            pnl_pts = (
                (half_px - entry) * sign * 0.5
                + (final_exit_price - entry) * sign * 0.5
            )
        else:
            # Time exit, eod, overnight -- full position at final price
            pnl_pts = (final_exit_price - entry) * (1 if is_long else -1)

        pnl_gross  = pnl_pts * point_value * size_mult
        pnl_net    = pnl_gross - (2 * commission)
        r_multiple = pnl_pts / risk_pts if risk_pts > 0 else 0.0
        is_win     = pnl_net > 0

        trade_rec = {
            "version":          label,
            "date":             pd.Timestamp(row["Timestamp"]).date(),
            "market":           market,
            "direction":        "LONG" if is_long else "SHORT",
            "entry":            round(entry, 4),
            "stop_initial":     round(stop_initial, 4),
            "stop_live":        round(stop, 4),
            "target1":          round(target1, 4),
            "target2":          round(target2, 4),
            "exit_price":       round(final_exit_price, 4),
            "exit_reason":      exit_reason,
            "atr_at_entry":     round(atr_val, 4) if atr_val > 0 else None,
            "range_size":       round(range_size, 4),
            "risk_pts":         round(risk_pts, 4),
            "pnl_pts":          round(pnl_pts, 4),
            "pnl_gross":        round(pnl_gross, 2),
            "pnl_net":          round(pnl_net, 2),
            "r_multiple":       round(r_multiple, 3),
            "partial_taken":    partial_taken,
            "exit_bars":        exit_bar_offset,
            "is_win":           is_win,
            "gls_score":        gls_score,
            "gls_action":       gls_action,
            "econ_impact":      econ_impact,
            "vix_regime":       vix_regime,
            "daily_regime":     daily_regime,
            "size_mult":        size_mult,
            "is_overnight":     is_overnight,
            "is_multiday":      is_multiday,
            "vwap_aligned":     vwap_aligned,
            "delta_aligned":    delta_aligned,
            "of_score":         of_score,
            "htf_bias":         str(row.get("fhb_htf_bias",  "NEUTRAL")),
            "fast_bias":        str(row.get("fhb_fast_bias", "NEUTRAL")),
            "range_filtered":   False,  # always False for executed trades
        }
        trades.append(trade_rec)

        # ── Database logging ──────────────────────────────────────────────────
        if db is not None:
            try:
                db.log_signal(
                    signal_id=signal_id,
                    trade_date=signal_date,
                    market=market,
                    strategy="FHB",
                    direction="LONG" if is_long else "SHORT",
                    gls_score=gls_score,
                    gls_action=gls_action,
                    filtered=False,
                    regime=daily_regime,
                    vix_regime=vix_regime,
                    econ_impact=econ_impact,
                )
                db.log_trade_entry(
                    signal_id=signal_id,
                    entry_time=str(pd.Timestamp(next_bar["Timestamp"])),
                    entry_price=entry,
                    stop_price=stop_initial,
                    target_price=target2,
                    risk_usd=risk_pts * point_value * size_mult,
                    size_mult=size_mult,
                )
                db.log_trade_exit(
                    signal_id=signal_id,
                    exit_time=signal_date,
                    exit_price=final_exit_price,
                    exit_reason=exit_reason,
                    pnl_gross=pnl_gross,
                    pnl_net=pnl_net,
                    pnl_r=r_multiple,
                    commission=2 * commission,
                    bars_held=exit_bar_offset,
                    is_overnight=is_overnight,
                    is_multiday=is_multiday,
                )
            except Exception:
                pass   # DB errors never crash the backtest

    return trades


# ============================================================
# METRICS
# ============================================================

def compute_metrics(trades: list[dict], market: str) -> dict:
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

    avg_win  = df.loc[df["is_win"],  "pnl_net"].mean() if wins   > 0 else 0.0
    avg_loss = df.loc[~df["is_win"], "pnl_net"].mean() if losses > 0 else 0.0
    avg_r    = df["r_multiple"].mean()

    daily_pnl = df.groupby("date")["pnl_net"].sum()
    avg_daily = daily_pnl.mean()
    best_day  = daily_pnl.max()
    worst_day = daily_pnl.min()

    cum_eq   = daily_pnl.cumsum()
    max_dd   = float((cum_eq - cum_eq.cummax()).min())

    exit_counts  = df["exit_reason"].value_counts().to_dict()
    partial_rate = df["partial_taken"].mean() * 100
    r_by_exit    = df.groupby("exit_reason")["r_multiple"].mean().round(3).to_dict()

    # Phase 5D stats
    overnight_pct = df.get("is_overnight", pd.Series([False]*total)).mean() * 100
    regime_pf = {}
    if "daily_regime" in df.columns:
        for reg, grp in df.groupby("daily_regime"):
            gw = grp.loc[grp["pnl_net"] > 0,  "pnl_net"].sum()
            gl = abs(grp.loc[grp["pnl_net"] <= 0, "pnl_net"].sum())
            regime_pf[reg] = round(gw / gl if gl > 0 else float("inf"), 2)

    gls_buckets = {}
    if "gls_score" in df.columns:
        for label_b, (lo, hi) in [("0-39", (0,39)), ("40-59", (40,59)),
                                    ("60-79", (60,79)), ("80-100", (80,100))]:
            grp = df[(df["gls_score"] >= lo) & (df["gls_score"] <= hi)]
            if len(grp) > 0:
                gw = grp.loc[grp["pnl_net"] > 0,  "pnl_net"].sum()
                gl = abs(grp.loc[grp["pnl_net"] <= 0, "pnl_net"].sum())
                gls_buckets[label_b] = {
                    "trades": len(grp),
                    "win_pct": round(grp["is_win"].mean() * 100, 1),
                    "pf": round(gw / gl if gl > 0 else float("inf"), 2),
                }

    # Phase 5E: PF by order flow score (0=no confirm, 1=partial, 2=full)
    of_buckets = {}
    if "of_score" in df.columns:
        for score_val in [0, 1, 2]:
            grp = df[df["of_score"] == score_val]
            if len(grp) > 0:
                gw = grp.loc[grp["pnl_net"] > 0,  "pnl_net"].sum()
                gl = abs(grp.loc[grp["pnl_net"] <= 0, "pnl_net"].sum())
                lbl = {0: "no_confirm", 1: "partial", 2: "full_confirm"}[score_val]
                of_buckets[lbl] = {
                    "trades":  len(grp),
                    "win_pct": round(grp["is_win"].mean() * 100, 1),
                    "pf":      round(gw / gl if gl > 0 else float("inf"), 2),
                }

    return {
        "market":             market,
        "total_trades":       total,
        "win_rate_pct":       round(win_rate * 100, 1),
        "profit_factor":      round(pf, 2),
        "total_net_pnl":      round(df["pnl_net"].sum(), 2),
        "avg_daily_pnl":      round(avg_daily, 2),
        "best_day":           round(best_day, 2),
        "worst_day":          round(worst_day, 2),
        "avg_win_usd":        round(avg_win, 2),
        "avg_loss_usd":       round(avg_loss, 2),
        "avg_r_multiple":     round(avg_r, 3),
        "partial_rate_pct":   round(partial_rate, 1),
        "max_drawdown_usd":   round(max_dd, 2),
        "exit_breakdown":     exit_counts,
        "r_by_exit_type":     r_by_exit,
        "trading_days":       int(daily_pnl.shape[0]),
        "overnight_pct":      round(overnight_pct, 1),
        "pf_by_regime":       regime_pf,
        "pf_by_gls_bucket":   gls_buckets,
        "pf_by_of_score":     of_buckets,
    }


# ============================================================
# REPORTING
# ============================================================

def yearly_breakdown(trades: list[dict], market: str) -> None:
    if not trades:
        return
    df = pd.DataFrame(trades)
    df["year"] = pd.to_datetime(df["date"]).dt.year
    print(f"\n  {market} -- Year-by-Year:")
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


def print_comparison(b: dict, im: dict, label: str = "") -> None:
    if label:
        print(f"\n  {label} -- Phase 5C (baseline) vs Phase 5D (all filters):")
    print(f"  {'Metric':<24} {'5C Baseline':>14} {'5D Improved':>14} {'Delta':>10}")
    print("  " + "-" * 64)
    rows = [
        ("win_rate_pct",     "Win Rate %",     "{:.1f}%",  "{:+.1f}pp"),
        ("profit_factor",    "Profit Factor",  "{:.2f}",   "{:+.2f}"),
        ("total_net_pnl",    "Total P&L",      "${:,.0f}", "${:+,.0f}"),
        ("avg_daily_pnl",    "Avg Daily P&L",  "${:,.0f}", "${:+,.0f}"),
        ("avg_r_multiple",   "Avg R/trade",    "{:.3f}R",  "{:+.3f}R"),
        ("partial_rate_pct", "Partial %",      "{:.1f}%",  "{:+.1f}pp"),
        ("max_drawdown_usd", "Max Drawdown",   "${:,.0f}", "${:+,.0f}"),
    ]
    for key, lbl, fmt_val, fmt_delta in rows:
        bv    = b.get(key, 0) or 0
        iv    = im.get(key, 0) or 0
        delta = iv - bv
        try:
            print(f"  {lbl:<24} {fmt_val.format(bv):>14} {fmt_val.format(iv):>14} "
                  f"{fmt_delta.format(delta):>10}")
        except Exception:
            pass


def print_report(
    baseline_map:  dict,
    improved_map:  dict,
    all_trades_5d: list[dict],
    total_days:    int,
    days_skipped:  dict,
) -> None:
    mkt_list = " + ".join(baseline_map.keys())
    print("\n" + "=" * 68)
    print("  AlgoBot FHB Backtest -- Phase 5D Institutional Filters")
    print(f"  Data: Yahoo Finance 1h | ~730 days | {mkt_list} | ~{total_days} trading days")
    print("=" * 68)
    print(f"\n  Phase 5D improvements applied:")
    print(f"    Economic Calendar: Skip HIGH_IMPACT days (NFP, FOMC) = {FHB_SKIP_HIGH_IMPACT}")
    print(f"    VIX Regime Gate:   Skip QUIET + CRISIS days = {FHB_VIX_FILTER}")
    print(f"    Green Light Score: Skip if score < {FHB_GLS_MIN_SCORE}, half-size < {FHB_GLS_HALF_SCORE}")
    print(f"    Overnight Carry:   Hold profitable trades overnight = {FHB_OVERNIGHT_CARRY}")
    for market, n in days_skipped.items():
        print(f"    {market}: {n} signals filtered by Phase 5D gates")

    print("\n" + "-" * 68)
    combined_5c = combined_5d = 0.0
    for market in baseline_map:
        b  = baseline_map[market]
        im = improved_map[market]
        combined_5c += b.get("total_net_pnl", 0) or 0
        combined_5d += im.get("total_net_pnl", 0) or 0

        print(f"\n  {market} ({im.get('total_trades',0)} trades, "
              f"~{im.get('trading_days',0)} days, "
              f"{im.get('overnight_pct',0):.1f}% overnight):")
        print(f"    Win rate          : {im['win_rate_pct']:.1f}%")
        print(f"    Profit Factor     : {im['profit_factor']:.2f}")
        print(f"    Total net P&L     : ${im['total_net_pnl']:>10,.2f}")
        print(f"    Avg daily P&L     : ${im['avg_daily_pnl']:>10,.2f}")
        print(f"    Best day          : ${im['best_day']:>10,.2f}")
        print(f"    Worst day         : ${im['worst_day']:>10,.2f}")
        print(f"    Max drawdown      : ${im['max_drawdown_usd']:>10,.2f}")
        print(f"    Avg R per trade   : {im['avg_r_multiple']:>10.3f}R")
        print(f"    Partial exit rate : {im['partial_rate_pct']:.1f}%")
        print(f"    Exit breakdown    : {im['exit_breakdown']}")
        if im.get("pf_by_regime"):
            print(f"    PF by regime      : {im['pf_by_regime']}")
        if im.get("pf_by_gls_bucket"):
            print(f"    PF by GLS bucket  : {im['pf_by_gls_bucket']}")
        if im.get("pf_by_of_score"):
            print(f"    PF by OF score    : {im['pf_by_of_score']}")
        print_comparison(b, im, label=market)

    avg_days = max(m.get("trading_days", 1) for m in improved_map.values())
    n_markets = len(baseline_map)
    print(f"\n  COMBINED ({n_markets} markets):")
    print(f"    5C baseline total P&L : ${combined_5c:>10,.2f}  "
          f"(${combined_5c/avg_days:>8,.2f}/day)")
    print(f"    5D improved total P&L : ${combined_5d:>10,.2f}  "
          f"(${combined_5d/avg_days:>8,.2f}/day)")
    print(f"    Improvement           : ${combined_5d-combined_5c:>10,.2f}  "
          f"({(combined_5d-combined_5c)/max(abs(combined_5c),1)*100:+.1f}%)")

    print("\n" + "-" * 68)
    print("  MULTI-DAY HOLDING STATUS:")
    print("    Swing strategy (daily bars): ALREADY multi-day/week/month via trailing stops")
    print("    FHB intraday: Overnight carry enabled for +0.5R profitable trades")
    print("    Max FHB hold: same day (5h) + overnight extension (2 days max)")
    print("=" * 68 + "\n")


# ============================================================
# MAIN
# ============================================================

def main():
    print("\n" + "=" * 68)
    print("  AlgoBot FHB Backtest -- Phase 5D | 8 Markets")
    print("=" * 68 + "\n")

    config      = load_config()
    # Markets retained after overhaul screening:
    #   ES  PF=2.08  KEEP  | NQ  PF=2.00  KEEP
    #   GC  PF=1.09  DROP  (overall PF 1.09, 2026 PF 0.51, DD -$12k — London dynamics, not US open)
    #   RTY PF=1.09  KEEP (HTF bias now working with IWM proxy)
    #   CL  PF=0.85  DROP  | ZB  PF=0.76  DROP
    #   6E  PF=0.91  DROP  | YM  PF=0.89  DROP
    fhb_markets = ["ES", "NQ"]

    # ── Step 1: Instantiate Phase 5D utilities ────────────────────────────────
    print("Step 1: Initialising Phase 5D filters...")
    econ_cal   = EconCalendar()
    cal_stats  = econ_cal.total_events()
    print(f"  EconCalendar: {cal_stats['high']} HIGH-impact + "
          f"{cal_stats['medium']} MEDIUM-impact dates loaded")

    vix_filter = VIXFilter.from_yahoo(start="2023-01-01", end="2026-12-31")
    gls_engine = GreenLightScore(full_size_threshold=FHB_GLS_HALF_SCORE,
                                  half_size_threshold=FHB_GLS_MIN_SCORE)

    db_path = PROJECT_ROOT / "data" / "trades.db"
    db = TradeDB(str(db_path))
    print(f"  SQLite trade database: {db_path}")

    # ── Step 2: Download 1-hour data ──────────────────────────────────────────
    print("\nStep 2: Downloading 1-hour intraday data...")
    intraday_data: dict = {}
    for market in fhb_markets:
        df = download_1h_intraday(market)
        if not df.empty:
            intraday_data[market] = df
    if not intraday_data:
        print("\nERROR: No data downloaded.\n")
        sys.exit(1)

    total_days = max(df.index.normalize().nunique() for df in intraday_data.values())

    # ── Step 3: ATR ───────────────────────────────────────────────────────────
    print("\nStep 3: Computing ATR(14) on 1-hour bars...")
    atr_by_market: dict = {}
    for market, df_1h in intraday_data.items():
        atr_ser = compute_1h_atr(df_1h, FHB_ATR_PERIOD)
        atr_by_market[market] = atr_ser
        recent = atr_ser.dropna().iloc[-1] if len(atr_ser.dropna()) > 0 else 0
        print(f"  {market}: ATR(14,1h)={recent:.2f} pts | "
              f"ATR stop={FHB_ATR_STOP_MULT}x={recent*FHB_ATR_STOP_MULT:.2f} pts")

    # ── Step 4: HTF data (bias + regime + fast_bias) ─────────────────────────
    print("\nStep 4: Computing HTF bias + daily regime + fast daily bias...")
    htf_bias_map:   dict = {}
    regime_map:     dict = {}
    fast_bias_map:  dict = {}
    for market in fhb_markets:
        if market in intraday_data:
            bias, regime, fast_bias = get_htf_data(market, config)
            htf_bias_map[market]  = bias
            regime_map[market]    = regime
            fast_bias_map[market] = fast_bias

    # ── Step 5: Signal computation (with Phase 5D metadata) ──────────────────
    print("\nStep 5: Computing FHB entry signals (with Phase 5D metadata)...")
    fhb_results: dict = {}
    for market in fhb_markets:
        if market not in intraday_data:
            continue
        df_1h    = intraday_data[market]
        htf_bias = htf_bias_map.get(market, pd.Series(dtype=str))
        regime   = regime_map.get(market, pd.Series(dtype=str))
        df_sig   = compute_fhb_signals(
            df_1h, market, htf_bias, regime, config,
            econ_cal=econ_cal,
            vix_filter=vix_filter,
            gls_engine=gls_engine,
            fast_bias_series=fast_bias_map.get(market, pd.Series(dtype=str)),
        )
        fhb_results[market] = df_sig
        longs   = int(df_sig["fhb_long_signal"].sum())
        shorts  = int(df_sig["fhb_short_signal"].sum())
        blocked = int(df_sig["fhb_htf_blocked"].sum())
        print(f"  {market}: {longs} longs, {shorts} shorts, {blocked} HTF blocked")

    # ── Step 6: Phase 5C BASELINE (for comparison) ────────────────────────────
    print("\nStep 6: Simulating Phase 5C BASELINE (ATR stop + trail, NO new filters)...")
    baseline_map:  dict = {}
    all_trades_5c: list = []
    for market, df_sig in fhb_results.items():
        atr_ser   = atr_by_market.get(market, pd.Series(dtype=float))
        trades_5c = simulate_fhb_trades(
            df_sig, atr_ser, market, config,
            use_atr_stop=True, trail_be=True,
            overnight_carry=False, label="5C",
            db=None,   # don't log baseline to DB
        )
        all_trades_5c.extend(trades_5c)
        baseline_map[market] = compute_metrics(trades_5c, market)
        m = baseline_map[market]
        print(f"  {market}: {m['total_trades']} trades | "
              f"PF={m['profit_factor']:.2f} | Win={m['win_rate_pct']:.1f}% | "
              f"P&L=${m['total_net_pnl']:,.0f}")

    # ── Step 7: Phase 5D IMPROVED (all filters + overnight carry) ─────────────
    print("\nStep 7: Simulating Phase 5D IMPROVED (all filters + overnight carry)...")
    improved_map:  dict = {}
    all_trades_5d: list = []
    days_skipped:  dict = {}
    for market, df_sig in fhb_results.items():
        atr_ser   = atr_by_market.get(market, pd.Series(dtype=float))
        trades_5d = simulate_fhb_trades(
            df_sig, atr_ser, market, config,
            use_atr_stop=True, trail_be=True,
            overnight_carry=FHB_OVERNIGHT_CARRY, label="5D",
            db=db,
        )
        all_trades_5d.extend(trades_5d)
        improved_map[market] = compute_metrics(trades_5d, market)
        m = improved_map[market]
        skipped = baseline_map[market]["total_trades"] - m["total_trades"]
        days_skipped[market] = skipped
        print(f"  {market}: {m['total_trades']} trades ({skipped} filtered) | "
              f"PF={m['profit_factor']:.2f} | Win={m['win_rate_pct']:.1f}% | "
              f"P&L=${m['total_net_pnl']:,.0f}")

    # ── Step 8: Full report ────────────────────────────────────────────────────
    print_report(baseline_map, improved_map, all_trades_5d, total_days, days_skipped)

    # ── Step 9: Year-by-year breakdown ────────────────────────────────────────
    print("  Year-by-year breakdown (Phase 5D):")
    for market in fhb_markets:
        mkt_trades = [t for t in all_trades_5d if t["market"] == market]
        yearly_breakdown(mkt_trades, market)
    print()

    # ── Step 10: Conditional expectancy from database ─────────────────────────
    print("  Conditional Expectancy (from SQLite -- Phase 5D trades):")
    for market in fhb_markets:
        for min_gls in [0, 60, 80]:
            stats = db.conditional_stats(strategy="FHB", market=market, min_gls=min_gls)
            if stats["n_trades"] > 0:
                print(f"  {market} | GLS>={min_gls}: "
                      f"{stats['n_trades']} trades | "
                      f"Win={stats['win_rate_pct']:.1f}% | "
                      f"Avg P&L=${stats['avg_pnl_net']:,.0f} | "
                      f"Expectancy/trade=${stats['avg_pnl_net']:,.0f}")

    # ── Step 11: Save trade CSV ────────────────────────────────────────────────
    reports_dir = PROJECT_ROOT / "reports" / "backtests"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    if all_trades_5d:
        p = reports_dir / f"fhb_5d_improved_{ts}.csv"
        pd.DataFrame(all_trades_5d).to_csv(p, index=False)
        print(f"\n  5D trade log saved : {p.name}")
        print(f"  SQLite database    : {db_path}")

    db.close()
    print()
    return improved_map


if __name__ == "__main__":
    main()
