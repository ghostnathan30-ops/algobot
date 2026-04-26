"""
AlgoBot — VWAP Strategies: Trend Pullback + Mean Reversion
===========================================================
Module:  src/strategy/vwap_signal.py
Phase:   6 — Institutional VWAP Framework
Purpose: Two quantitative VWAP-based strategies suited to different
         market regimes and asset classes.

── WHY VWAP? ──────────────────────────────────────────────────────────────────
VWAP is the single most-watched institutional reference price intraday.
Every major buy-side desk benchmarks execution quality against VWAP.
This creates self-reinforcing behaviour: institutions push price back toward
VWAP when it deviates too far, AND add to positions when price briefly dips
to VWAP in a trending market.

Academic basis:
  - Madhavan (2000): VWAP as primary benchmark for institutional execution
  - Berkowitz, Logue & Noser (1988): Institutional VWAP anchoring documented
  - Kissell & Glantz (2003): Intraday VWAP drift and reversion patterns
  - Brownlees & Gallo (2006): Volume-weighted deviation predicts short-term MR

── STRATEGY A: VWAP TREND PULLBACK ────────────────────────────────────────────
Assets:       NQ (primary), ES (secondary)
Timeframe:    1-hour bars
Market regime: Trending (ADX ≥ 20) OR any regime in the first 2 hours of RTH
Signal window: 09:45–13:00 ET

Logic:
  1. Confirm trend: price has been ABOVE (or below) VWAP for ≥ 3 consecutive bars
  2. Pullback: price touches VWAP ±touch_threshold (default ±0.15% of VWAP)
  3. Bounce: the touch bar or next bar closes back above (below) VWAP AND
             has a bullish (bearish) bar (close > open for long)
  4. Entry:  market order at close of confirmation bar
  5. Stop:   below the pullback bar's Low − ATR × 0.3 (tight; if wrong exit fast)
  6. Target: +2.0R (good asymmetry vs tight stop); secondary target = prior session high
  7. Breakeven: move stop to entry when +1.0R is reached

Win-rate expectation: 58–65% (institutional alignment = high-probability)
Profit Factor target: 1.8–2.4 live

Assets where this WORKS:
  ✅ NQ  — highest institutional VWAP participation; strong trend-and-revert
  ✅ ES  — similar but calmer; reliable VWAP magnetism
  ⚠  GC  — works on trend days but gold has strong news sensitivity
  ❌ CL  — EIA disruption makes VWAP less reliable on range touches

── STRATEGY B: VWAP MEAN REVERSION ───────────────────────────────────────────
Assets:       GC/MGC (primary), NQ range days (secondary), CL (tertiary)
Timeframe:    1-hour bars (30m also tested)
Market regime: Ranging (ADX < 25) — do NOT fade in strong trends
Signal window: 09:45–14:30 ET

Logic:
  1. Price extends beyond VWAP ±2 SD bands (volume-weighted standard deviation)
  2. RSI(5) extreme: >75 for overextended longs (SHORT signal),
                     <25 for overextended shorts (LONG signal)
  3. Regime gate: ADX < 25 (ranging — not a trending market where extension continues)
  4. HTF fade filter: skip if weekly HTF confirms the extension direction
  5. Entry: market order at close of signal bar
  6. Stop:  beyond ±3 SD (the "no coming back" level; if price reaches ±3SD the
            mean reversion thesis is broken)
  7. Target: VWAP midline (the magnet) — typically 0.8–1.5R
  8. Max hold: 4 bars (if not at VWAP by bar 4, thesis failed)

Win-rate expectation: 45–55% (low win rate, but asymmetric payoff)
Profit Factor target: 1.5–2.0 live (driven by favorable reward when it works)

Assets where this WORKS:
  ✅ GC/MGC — gold has very strong VWAP magnetism; ~49% of breakouts revert
  ✅ NQ     — on confirmed range days (ADX<20) overnight-session overextensions revert
  ✅ ES     — lower volatility makes SD bands more reliable
  ⚠  CL    — works but requires EIA blackout (Wednesday 10:00-11:30 ET)

── COLUMN SCHEMA ──────────────────────────────────────────────────────────────
Added by compute_vwap_signals():

  # VWAP bands (daily-reset, volume-weighted SD)
  vwap             — daily VWAP (cumulative from RTH open)
  vwap_sd          — volume-weighted 1 standard deviation
  vwap_1sd_upper   — VWAP + 1×SD
  vwap_1sd_lower   — VWAP - 1×SD
  vwap_2sd_upper   — VWAP + 2×SD
  vwap_2sd_lower   — VWAP - 2×SD
  vwap_3sd_upper   — VWAP + 3×SD (stop zone for mean reversion)
  vwap_3sd_lower   — VWAP - 3×SD

  # Pullback mode (Mode A)
  vwap_pb_long       — True: confirmed VWAP pullback long signal
  vwap_pb_short      — True: confirmed VWAP pullback short signal
  vwap_pb_stop       — Calculated stop level (below pullback bar low)
  vwap_pb_target     — Calculated target (+2.0R)

  # Mean reversion mode (Mode B)
  vwap_mr_long       — True: price below -2SD + RSI<25 + ADX<25 (go long to VWAP)
  vwap_mr_short      — True: price above +2SD + RSI>75 + ADX<25 (go short to VWAP)
  vwap_mr_stop       — Calculated stop (beyond ±3SD)
  vwap_mr_target     — VWAP midline

  # Shared
  vwap_htf_blocked   — True: HTF bias gate blocked this signal
  vwap_filter_reason — Why signal was filtered (diagnostics)
  vwap_gls_score     — Green Light Score composite (0–100)
  vwap_atr           — ATR at signal bar (used for stop calc)

Usage:
    from src.strategy.vwap_signal import compute_vwap_signals, simulate_vwap_trades

    df_sig = compute_vwap_signals(df_1h, "NQ", htf_bias, regime, config, econ_cal, vix_filter)
    trades = simulate_vwap_trades(df_sig, "NQ", config)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import get_logger

log = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
_BULL    = "BULL"
_BEAR    = "BEAR"
_NEUTRAL = "NEUTRAL"

VWAP_ATR_PERIOD  = 14
RTH_START        = "09:30"
SIGNAL_START     = "09:45"
PB_SIGNAL_END    = "13:00"
MR_SIGNAL_END    = "14:30"

# Assets where each mode makes sense
PULLBACK_MARKETS   = {"NQ", "MNQ", "ES", "MES"}

# REVERSION_MARKETS: CL and MCL removed after backtest (2026-04-04).
#   CL: PF=0.74 — oil is geopolitics-driven, VWAP MR has no edge
#   MCL: PF=0.00, WR=0.7% — yfinance data quality issues + no real edge
#   NQ/MNQ kept but controlled by equity_mr_long_only config flag (bull bias)
REVERSION_MARKETS  = {"NQ", "MNQ", "ES", "MES", "GC", "MGC"}

# Equity index markets — these trend strongly; MR_SHORT disabled in bull regimes
_EQUITY_MR_MARKETS = {"NQ", "MNQ", "ES", "MES"}


# ── ATR helper (Wilder's smoothed ATR on 1H bars) ─────────────────────────────

def _compute_atr(df: pd.DataFrame, period: int = VWAP_ATR_PERIOD) -> pd.Series:
    h, lo, c = df["High"], df["Low"], df["Close"]
    prev_c   = c.shift(1)
    tr = pd.concat([h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, min_periods=period, adjust=False).mean()


# ── Core VWAP + Volume-Weighted SD bands ──────────────────────────────────────

def _add_vwap_bands(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute daily-reset VWAP with proper volume-weighted standard deviation bands.

    Formula:
        TP        = (High + Low + Close) / 3
        VWAP      = Σ(TP × V) / Σ(V)        — cumulative within each day
        Var_VWAP  = Σ(V × TP²) / Σ(V) − VWAP²
        SD_VWAP   = √Var_VWAP
        ±1SD band = VWAP ± 1 × SD_VWAP
        ±2SD band = VWAP ± 2 × SD_VWAP
        ±3SD band = VWAP ± 3 × SD_VWAP

    This is the institutionally-standard VWAP SD used by Bloomberg Terminal,
    Sierra Charts, and most professional execution desks. The standard deviation
    is volume-weighted (not time-weighted), so high-volume bars have more
    influence on the width of the bands.

    Args:
        df: 1H OHLCV DataFrame with DatetimeIndex (ET timezone)

    Returns:
        df copy with vwap, vwap_sd, vwap_1/2/3sd_upper/lower columns added.
    """
    df = df.copy()

    vol  = df["Volume"].clip(lower=1)
    tp   = (df["High"] + df["Low"] + df["Close"]) / 3.0
    tp2  = tp ** 2

    if hasattr(df.index, "normalize"):
        day_key = df.index.normalize()
    else:
        day_key = pd.Series(df.index).dt.normalize().values

    # Cumulative sums within each day
    cum_tpv  = (tp  * vol).groupby(day_key).cumsum()
    cum_tp2v = (tp2 * vol).groupby(day_key).cumsum()
    cum_vol  = vol.groupby(day_key).cumsum()

    vwap_vals   = cum_tpv / cum_vol
    var_vals    = (cum_tp2v / cum_vol) - vwap_vals ** 2
    sd_vals     = np.sqrt(var_vals.clip(lower=0))

    df["vwap"]          = vwap_vals.round(4)
    df["vwap_sd"]       = sd_vals.round(4)
    df["vwap_1sd_upper"] = (vwap_vals + 1.0 * sd_vals).round(4)
    df["vwap_1sd_lower"] = (vwap_vals - 1.0 * sd_vals).round(4)
    df["vwap_2sd_upper"] = (vwap_vals + 2.0 * sd_vals).round(4)
    df["vwap_2sd_lower"] = (vwap_vals - 2.0 * sd_vals).round(4)
    df["vwap_3sd_upper"] = (vwap_vals + 3.0 * sd_vals).round(4)
    df["vwap_3sd_lower"] = (vwap_vals - 3.0 * sd_vals).round(4)
    df["above_vwap"]     = df["Close"] > df["vwap"]

    return df


# ── RSI helper (fast, used for MR confirmation) ───────────────────────────────

def _compute_rsi(series: pd.Series, period: int = 5) -> pd.Series:
    delta  = series.diff()
    gain   = delta.clip(lower=0)
    loss   = (-delta).clip(lower=0)
    avg_g  = gain.ewm(span=period, min_periods=period, adjust=False).mean()
    avg_l  = loss.ewm(span=period, min_periods=period, adjust=False).mean()
    rs     = avg_g / avg_l.replace(0, 1e-9)
    return 100.0 - (100.0 / (1.0 + rs))


# ── GLS score helpers ──────────────────────────────────────────────────────────

def _gls_pullback(htf_bias: str, bar_delta_positive: bool,
                  above_vwap: bool, direction: str,
                  consecutive_bars: int) -> int:
    """
    Composite quality score for VWAP pullback setups (0–100).

    Scoring:
      Base score:                          60
      HTF aligned (BULL for long):        +10
      Synthetic delta confirms direction: +10
      VWAP side correct:                  +10
      Strong trend (≥5 consecutive bars): +10
    """
    score = 60
    if direction == "LONG"  and htf_bias == _BULL:  score += 10
    if direction == "SHORT" and htf_bias == _BEAR:  score += 10
    if direction == "LONG"  and above_vwap:          score += 5
    if direction == "SHORT" and not above_vwap:      score += 5
    if bar_delta_positive and direction == "LONG":   score += 10
    if not bar_delta_positive and direction == "SHORT": score += 10
    if consecutive_bars >= 5:                        score += 5
    return min(score, 95)


def _gls_reversion(htf_bias: str, rsi_val: float, sd_extension: float,
                   direction: str) -> int:
    """
    Composite quality score for VWAP mean reversion setups (0–100).

    Scoring:
      Base score:                         60
      HTF NEUTRAL (best for MR):         +10
      RSI extreme (>80 short / <20 long): +10
      Extension deep (>2.5 SD):          +10
    """
    score = 60
    if htf_bias == _NEUTRAL:             score += 10
    if direction == "LONG"  and rsi_val < 20:   score += 10
    if direction == "SHORT" and rsi_val > 80:   score += 10
    if sd_extension > 2.5:               score += 10
    return min(score, 90)


# ── MAIN: Compute VWAP signals ─────────────────────────────────────────────────

def compute_vwap_signals(
    df_1h:           pd.DataFrame,
    market:          str,
    htf_bias_series: pd.Series,
    regime_series:   pd.Series,
    config:          dict,
    econ_cal=None,
    vix_filter=None,
) -> pd.DataFrame:
    """
    Compute VWAP Pullback and VWAP Mean Reversion signals on 1-hour bars.

    Both modes are computed in a single pass; which fires on a given day
    depends on the regime and config flags.

    Args:
        df_1h:           1H OHLCV DataFrame with DatetimeIndex (ET timezone).
        market:          Market code ("NQ", "ES", "GC", "MGC", "CL", etc.).
        htf_bias_series: Daily Series of BULL/BEAR/NEUTRAL from htf_bias.py.
        regime_series:   Daily Series of regime strings from regime_classifier.py.
        config:          Full config dict. Reads vwap_pullback and vwap_reversion.
        econ_cal:        Optional EconCalendar instance.
        vix_filter:      Optional VIXFilter instance.

    Returns:
        DataFrame with vwap_* signal columns added (see module docstring).
    """
    pb_cfg  = config.get("vwap_pullback",  {})
    mr_cfg  = config.get("vwap_reversion", {})
    mkt_cfg = config.get("markets", {}).get(market, {})

    # ── Mode eligibility ───────────────────────────────────────────────────────
    mkt_upper      = market.upper()
    run_pullback   = mkt_upper in PULLBACK_MARKETS   and bool(pb_cfg.get("enabled", True))
    run_reversion  = mkt_upper in REVERSION_MARKETS  and bool(mr_cfg.get("enabled", True))

    # ── Pullback config ────────────────────────────────────────────────────────
    pb_touch_pct     = float(pb_cfg.get("touch_threshold",   0.0015))  # 0.15%
    pb_trend_bars    = int(  pb_cfg.get("trend_bars_required", 3))
    pb_stop_mult     = float(pb_cfg.get("stop_atr_mult",      0.3))
    pb_target_r      = float(pb_cfg.get("target_r",           2.0))
    pb_skip_medium   = bool( pb_cfg.get("skip_medium_impact", False))

    # ── Reversion config ───────────────────────────────────────────────────────
    mr_sd_entry         = float(mr_cfg.get("sd_entry_threshold",  2.0))
    mr_sd_stop          = float(mr_cfg.get("sd_stop_threshold",   3.0))
    mr_skip_medium      = bool( mr_cfg.get("skip_medium_impact",  True))   # GC is news-sensitive
    mr_equity_long_only = bool( mr_cfg.get("equity_mr_long_only", True))

    # Market-specific thresholds (backtest-validated 2026-04-04):
    #   GC/MGC: ADX<25, RSI 25/75 — gold bidirectional; original thresholds optimal
    #           (PF=2.37 at 25/75 + ADX<25  vs  PF=1.34 at stricter settings)
    #   Equity: ADX<20, RSI 20/80 — trending equity market; only extreme readings work
    #           Equity long-only gate also applies (no MR_Short in bull market)
    if mkt_upper in {"GC", "MGC"}:
        mr_rsi_long  = float(mr_cfg.get("rsi_long_gold",           25.0))
        mr_rsi_short = float(mr_cfg.get("rsi_short_gold",          75.0))
        mr_max_adx   = float(mr_cfg.get("max_adx_gold",            25.0))
    else:
        mr_rsi_long  = float(mr_cfg.get("rsi_long",                20.0))
        mr_rsi_short = float(mr_cfg.get("rsi_short",               80.0))
        mr_max_adx   = float(mr_cfg.get("max_adx",                 20.0))

    df = df_1h.copy()

    # ── Compute indicators ─────────────────────────────────────────────────────
    df = _add_vwap_bands(df)
    df["vwap_atr"] = _compute_atr(df)
    df["vwap_rsi"] = _compute_rsi(df["Close"])

    # Synthetic delta (buy/sell pressure from OHLC)
    try:
        from src.utils.orderflow import add_synthetic_delta
        df = add_synthetic_delta(df)
    except ImportError:
        df["bar_delta"]  = 0.0
        df["cum_delta"]  = 0.0

    # ADX for regime check in MR mode
    try:
        import ta.trend as _tat
        adx_ind     = _tat.ADXIndicator(df["High"], df["Low"], df["Close"], window=14)
        df["_adx"]  = adx_ind.adx()
    except Exception:
        df["_adx"]  = 20.0   # neutral fallback

    # ── Initialise signal columns ──────────────────────────────────────────────
    for col in ("vwap_pb_long", "vwap_pb_short",
                "vwap_mr_long", "vwap_mr_short",
                "vwap_htf_blocked"):
        df[col] = False

    for col in ("vwap_pb_stop", "vwap_pb_target",
                "vwap_mr_stop", "vwap_mr_target"):
        df[col] = float("nan")

    df["vwap_filter_reason"] = ""
    df["vwap_gls_score"]     = 0
    df["vwap_mode"]          = ""   # "PB" | "MR" | ""
    df["vwap_econ_impact"]   = "NONE"
    df["vwap_vix_regime"]    = "OPTIMAL"
    df["vwap_htf_bias"]      = _NEUTRAL

    trading_dates = df.index.normalize().unique()
    total_pb_long = total_pb_short = 0
    total_mr_long = total_mr_short = 0
    total_blocked = econ_skipped = vix_skipped = 0

    for day in trading_dates:
        day_str  = str(pd.Timestamp(day).date())
        day_mask = df.index.normalize() == day
        day_df   = df[day_mask].copy()

        if len(day_df) < 3:
            continue

        # ── Economic Calendar ──────────────────────────────────────────────────
        econ_impact = "NONE"
        if econ_cal is not None:
            econ_impact = econ_cal.get_impact_level(day_str)

        # For pullback: only skip HIGH impact (MEDIUM = half size, handled later)
        # For reversion (gold): skip HIGH + MEDIUM
        pb_skip = (econ_cal is not None and
                   econ_cal.skip_today(day_str, min_impact="HIGH" if not pb_skip_medium else "MEDIUM"))
        mr_skip = (econ_cal is not None and
                   econ_cal.skip_today(day_str, min_impact="MEDIUM" if mr_skip_medium else "HIGH"))

        # ── VIX regime ─────────────────────────────────────────────────────────
        vix_regime = "OPTIMAL"
        if vix_filter is not None:
            vix_regime = vix_filter.get_regime(day_str)
        if vix_regime in ("QUIET", "CRISIS"):
            day_df["vwap_vix_regime"]   = vix_regime
            day_df["vwap_econ_impact"]  = econ_impact
            df.update(day_df)
            vix_skipped += 1
            continue

        # ── HTF bias for this day ──────────────────────────────────────────────
        htf_bias = _NEUTRAL
        try:
            if len(htf_bias_series) > 0:
                bias_idx = htf_bias_series.copy()
                bias_idx.index = [str(pd.Timestamp(d).date()) for d in htf_bias_series.index]
                prior    = bias_idx[bias_idx.index <= day_str]
                htf_bias = str(prior.iloc[-1]) if len(prior) > 0 else _NEUTRAL
        except Exception:
            pass

        day_df["vwap_htf_bias"]    = htf_bias
        day_df["vwap_vix_regime"]  = vix_regime
        day_df["vwap_econ_impact"] = econ_impact

        # ── Tradeable windows ──────────────────────────────────────────────────
        try:
            pb_window = day_df.between_time(SIGNAL_START, PB_SIGNAL_END)
            mr_window = day_df.between_time(SIGNAL_START, MR_SIGNAL_END)
        except Exception:
            df.update(day_df)
            continue

        # Track consecutive bars above/below VWAP for pullback mode
        above_arr = day_df["above_vwap"].values.astype(bool)
        pb_fired_long  = False
        pb_fired_short = False
        mr_fired_long  = False
        mr_fired_short = False

        # ── MODE A: VWAP Trend Pullback ────────────────────────────────────────
        if run_pullback and not pb_skip:
            for idx in pb_window.index:
                if pb_fired_long and pb_fired_short:
                    break

                bar      = day_df.loc[idx]
                atr_val  = float(bar["vwap_atr"]) if not pd.isna(bar["vwap_atr"]) else 0.0
                vwap_val = float(bar["vwap"])
                close    = float(bar["Close"])
                bar_open = float(bar["Open"])
                bar_low  = float(bar["Low"])
                bar_high = float(bar["High"])

                if pd.isna(vwap_val) or atr_val == 0:
                    continue

                # How far is price from VWAP as a fraction?
                dist_pct = abs(close - vwap_val) / vwap_val if vwap_val > 0 else 1.0

                # Count consecutive bars on one side of VWAP going back from this bar
                bar_pos = list(day_df.index).index(idx)
                consec  = 0
                for k in range(bar_pos, -1, -1):
                    if above_arr[k]:
                        consec += 1
                    else:
                        break
                consec_below = 0
                for k in range(bar_pos, -1, -1):
                    if not above_arr[k]:
                        consec_below += 1
                    else:
                        break

                # ── LONG PULLBACK SETUP ────────────────────────────────────────
                # Requires: ≥pb_trend_bars bars above VWAP before, now touching it,
                #           bounce bar (close > open), close back above VWAP
                if not pb_fired_long and not pb_fired_short:
                    consec_above_before = 0
                    for k in range(bar_pos - 1, -1, -1):
                        if above_arr[k]:
                            consec_above_before += 1
                        else:
                            break

                    is_touch_long = dist_pct <= pb_touch_pct and close >= vwap_val * 0.9985
                    is_bounce_bar = close > bar_open
                    trend_ok_long = consec_above_before >= pb_trend_bars

                    if trend_ok_long and is_touch_long and is_bounce_bar:
                        # HTF check for long
                        if htf_bias == _BEAR:
                            day_df.loc[idx, "vwap_htf_blocked"]    = True
                            day_df.loc[idx, "vwap_filter_reason"]  = f"PB_LONG blocked: HTF={htf_bias}"
                            total_blocked += 1
                        else:
                            stop_level   = bar_low - pb_stop_mult * atr_val
                            risk_pts     = abs(close - stop_level)
                            target_level = close + pb_target_r * risk_pts

                            bar_delta_pos = float(bar.get("bar_delta", 0)) > 0
                            gls = _gls_pullback(htf_bias, bar_delta_pos, True,
                                                "LONG", consec_above_before)

                            day_df.loc[idx, "vwap_pb_long"]    = True
                            day_df.loc[idx, "vwap_pb_stop"]    = round(stop_level,  4)
                            day_df.loc[idx, "vwap_pb_target"]  = round(target_level, 4)
                            day_df.loc[idx, "vwap_gls_score"]  = gls
                            day_df.loc[idx, "vwap_mode"]       = "PB"
                            pb_fired_long = True
                            total_pb_long += 1

                # ── SHORT PULLBACK SETUP ───────────────────────────────────────
                if not pb_fired_short and not pb_fired_long:
                    consec_below_before = 0
                    for k in range(bar_pos - 1, -1, -1):
                        if not above_arr[k]:
                            consec_below_before += 1
                        else:
                            break

                    is_touch_short = dist_pct <= pb_touch_pct and close <= vwap_val * 1.0015
                    is_drop_bar    = close < bar_open
                    trend_ok_short = consec_below_before >= pb_trend_bars

                    if trend_ok_short and is_touch_short and is_drop_bar:
                        if htf_bias == _BULL:
                            day_df.loc[idx, "vwap_htf_blocked"]   = True
                            day_df.loc[idx, "vwap_filter_reason"] = f"PB_SHORT blocked: HTF={htf_bias}"
                            total_blocked += 1
                        else:
                            stop_level   = bar_high + pb_stop_mult * atr_val
                            risk_pts     = abs(stop_level - close)
                            target_level = close - pb_target_r * risk_pts

                            bar_delta_pos = float(bar.get("bar_delta", 0)) > 0
                            gls = _gls_pullback(htf_bias, bar_delta_pos, False,
                                                "SHORT", consec_below_before)

                            day_df.loc[idx, "vwap_pb_short"]   = True
                            day_df.loc[idx, "vwap_pb_stop"]    = round(stop_level,  4)
                            day_df.loc[idx, "vwap_pb_target"]  = round(target_level, 4)
                            day_df.loc[idx, "vwap_gls_score"]  = gls
                            day_df.loc[idx, "vwap_mode"]       = "PB"
                            pb_fired_short = True
                            total_pb_short += 1

        # ── MODE B: VWAP Mean Reversion ────────────────────────────────────────
        if run_reversion and not mr_skip:
            for idx in mr_window.index:
                if mr_fired_long and mr_fired_short:
                    break
                # Don't fire both PB and MR on the same day (they conflict)
                if pb_fired_long or pb_fired_short:
                    break

                bar       = day_df.loc[idx]
                close     = float(bar["Close"])
                bar_high  = float(bar["High"])
                bar_low   = float(bar["Low"])
                atr_val   = float(bar["vwap_atr"])   if not pd.isna(bar["vwap_atr"])   else 0.0
                rsi_val   = float(bar["vwap_rsi"])   if not pd.isna(bar["vwap_rsi"])   else 50.0
                adx_val   = float(bar["_adx"])        if not pd.isna(bar["_adx"])        else 20.0
                vwap_val  = float(bar["vwap"])        if not pd.isna(bar["vwap"])        else float("nan")
                sd_val    = float(bar["vwap_sd"])     if not pd.isna(bar["vwap_sd"])     else 0.0
                upper2    = float(bar["vwap_2sd_upper"])
                lower2    = float(bar["vwap_2sd_lower"])
                upper3    = float(bar["vwap_3sd_upper"])
                lower3    = float(bar["vwap_3sd_lower"])

                if pd.isna(vwap_val) or sd_val == 0 or atr_val == 0:
                    continue

                # Regime gate: only in ranging/non-trending markets
                if adx_val >= mr_max_adx:
                    continue

                # ── MR LONG: price below -2SD + RSI oversold → long back to VWAP ──
                if not mr_fired_long and not mr_fired_short:
                    overextended_low = close < lower2
                    rsi_oversold     = rsi_val < mr_rsi_long

                    if overextended_low and rsi_oversold:
                        if htf_bias == _BEAR:
                            day_df.loc[idx, "vwap_htf_blocked"]   = True
                            day_df.loc[idx, "vwap_filter_reason"] = f"MR_LONG blocked: HTF={htf_bias}"
                            total_blocked += 1
                        else:
                            stop_level   = lower3 - 0.5 * atr_val  # beyond ±3SD
                            risk_pts     = abs(close - stop_level)
                            target_level = vwap_val  # target = VWAP midline

                            rr = abs(target_level - close) / risk_pts if risk_pts > 0 else 0
                            if rr < 0.75:
                                day_df.loc[idx, "vwap_filter_reason"] = f"MR_LONG rr={rr:.2f}<0.75"
                                total_blocked += 1
                            else:
                                sd_ext = abs(close - vwap_val) / sd_val if sd_val > 0 else 0
                                gls    = _gls_reversion(htf_bias, rsi_val, sd_ext, "LONG")

                                day_df.loc[idx, "vwap_mr_long"]   = True
                                day_df.loc[idx, "vwap_mr_stop"]   = round(stop_level,  4)
                                day_df.loc[idx, "vwap_mr_target"] = round(target_level, 4)
                                day_df.loc[idx, "vwap_gls_score"] = gls
                                day_df.loc[idx, "vwap_mode"]      = "MR"
                                mr_fired_long = True
                                total_mr_long += 1

                # ── MR SHORT: price above +2SD + RSI overbought → short back to VWAP ─
                # Equity long-only gate: NQ/MNQ/ES/MES in a trending bull market
                # keep extending beyond +2SD.  Never fade them short.
                if mr_equity_long_only and mkt_upper in _EQUITY_MR_MARKETS:
                    continue  # skip MR_SHORT entirely for equity index markets

                if not mr_fired_short and not mr_fired_long:
                    overextended_high = close > upper2
                    rsi_overbought    = rsi_val > mr_rsi_short

                    if overextended_high and rsi_overbought:
                        if htf_bias == _BULL:
                            day_df.loc[idx, "vwap_htf_blocked"]   = True
                            day_df.loc[idx, "vwap_filter_reason"] = f"MR_SHORT blocked: HTF={htf_bias}"
                            total_blocked += 1
                        else:
                            stop_level   = upper3 + 0.5 * atr_val  # beyond ±3SD
                            risk_pts     = abs(stop_level - close)
                            target_level = vwap_val  # target = VWAP midline

                            rr = abs(close - target_level) / risk_pts if risk_pts > 0 else 0
                            if rr < 0.75:
                                day_df.loc[idx, "vwap_filter_reason"] = f"MR_SHORT rr={rr:.2f}<0.75"
                                total_blocked += 1
                            else:
                                sd_ext = abs(close - vwap_val) / sd_val if sd_val > 0 else 0
                                gls    = _gls_reversion(htf_bias, rsi_val, sd_ext, "SHORT")

                                day_df.loc[idx, "vwap_mr_short"]  = True
                                day_df.loc[idx, "vwap_mr_stop"]   = round(stop_level,  4)
                                day_df.loc[idx, "vwap_mr_target"] = round(target_level, 4)
                                day_df.loc[idx, "vwap_gls_score"] = gls
                                day_df.loc[idx, "vwap_mode"]      = "MR"
                                mr_fired_short = True
                                total_mr_short += 1

        df.update(day_df)

    # Clean up temporary column
    df.drop(columns=["_adx"], inplace=True, errors="ignore")

    log.info(
        "{market}: VWAP signals | PB_Long={pbl} PB_Short={pbs} "
        "MR_Long={mrl} MR_Short={mrs} | Blocked={b} EconSkip={e} VixSkip={v} | Days={d}",
        market=market,
        pbl=total_pb_long,  pbs=total_pb_short,
        mrl=total_mr_long,  mrs=total_mr_short,
        b=total_blocked, e=econ_skipped, v=vix_skipped,
        d=len(trading_dates),
    )
    return df


# ── Trade simulation ───────────────────────────────────────────────────────────

def simulate_vwap_trades(
    df_signals: pd.DataFrame,
    market:     str,
    config:     dict,
    label:      str = "VWAP",
) -> list[dict]:
    """
    Simulate VWAP Pullback and Mean Reversion trades from signal columns.

    Mechanics:
      Pullback (PB):
        - Max hold: 8 bars (1 trading day = ~7 RTH 1H bars)
        - Partial exit at +0.75R → trail stop to breakeven
        - No overnight carry
        - Target: vwap_pb_target (2.0R)

      Mean Reversion (MR):
        - Max hold: 4 bars (MR either works fast or fails)
        - Target: VWAP midline (vwap_mr_target)
        - Partial exit at 0.5R → trail to breakeven
        - No overnight carry

    Returns:
        List of trade dicts (same schema as gc_signal.simulate_gc_trades).
    """
    pb_cfg  = config.get("vwap_pullback",  {})
    mr_cfg  = config.get("vwap_reversion", {})
    mkt_cfg = config.get("markets", {}).get(market, {})

    point_value     = float(mkt_cfg.get("point_value",    20.0))
    commission      = float(mkt_cfg.get("commission",      2.05))
    slippage_ticks  = int(  mkt_cfg.get("slippage_ticks",  1))
    tick_size       = float(mkt_cfg.get("tick_size",       0.25))
    slippage_pts    = slippage_ticks * tick_size

    pb_max_hold  = int(pb_cfg.get("max_hold_bars",   8))
    pb_partial_r = float(pb_cfg.get("partial_exit_r", 0.75))
    mr_max_hold  = int(mr_cfg.get("max_hold_bars",   4))
    mr_partial_r = float(mr_cfg.get("partial_exit_r", 0.50))

    trades = []
    bars   = df_signals.reset_index()

    for i, row in bars.iterrows():
        is_pb_long  = bool(row.get("vwap_pb_long",  False))
        is_pb_short = bool(row.get("vwap_pb_short", False))
        is_mr_long  = bool(row.get("vwap_mr_long",  False))
        is_mr_short = bool(row.get("vwap_mr_short", False))
        htf_blocked = bool(row.get("vwap_htf_blocked", False))

        if not any([is_pb_long, is_pb_short, is_mr_long, is_mr_short]) or htf_blocked:
            continue
        if i + 1 >= len(bars):
            continue

        # Determine mode-specific parameters
        if is_pb_long or is_pb_short:
            mode        = "PB"
            is_long     = is_pb_long
            stop_col    = "vwap_pb_stop"
            target_col  = "vwap_pb_target"
            max_hold    = pb_max_hold
            partial_r   = pb_partial_r
            strategy_lbl = f"{label}_PB"
        else:
            mode        = "MR"
            is_long     = is_mr_long
            stop_col    = "vwap_mr_stop"
            target_col  = "vwap_mr_target"
            max_hold    = mr_max_hold
            partial_r   = mr_partial_r
            strategy_lbl = f"{label}_MR"

        next_bar    = bars.iloc[i + 1]
        entry_raw   = float(next_bar["Open"])
        stop_level  = float(row[stop_col])   if not pd.isna(row[stop_col])   else 0.0
        target_lvl  = float(row[target_col]) if not pd.isna(row[target_col]) else 0.0

        if stop_level == 0 or target_lvl == 0:
            continue

        entry     = entry_raw + slippage_pts if is_long else entry_raw - slippage_pts
        stop      = stop_level
        risk_pts  = abs(entry - stop)
        if risk_pts <= 0:
            continue

        target         = target_lvl
        partial_target = entry + partial_r * risk_pts * (1 if is_long else -1)
        entry_day      = str(pd.Timestamp(row["Timestamp"] if "Timestamp" in row.index
                                          else bars.index[i]).date())

        partial_taken    = False
        current_stop     = stop
        final_exit_price = None
        exit_reason      = "time"

        for j in range(1, max_hold + 1):
            bar_idx = i + 1 + j
            if bar_idx >= len(bars):
                final_exit_price = float(bars.iloc[bar_idx - 1]["Close"])
                exit_reason      = "eod"
                break

            sim_bar  = bars.iloc[bar_idx]
            bar_high = float(sim_bar["High"])
            bar_low  = float(sim_bar["Low"])
            bar_date = str(pd.Timestamp(
                sim_bar.get("Timestamp", bars.index[bar_idx])
            ).date())

            # No overnight carry
            if bar_date != entry_day:
                final_exit_price = float(bars.iloc[bar_idx - 1]["Close"])
                exit_reason      = "eod_no_carry"
                break

            if is_long:
                if bar_low <= current_stop:
                    final_exit_price = current_stop
                    exit_reason      = "stop"
                    break
                if not partial_taken and bar_high >= partial_target:
                    partial_taken = True
                    current_stop  = entry   # trail to breakeven
                if bar_high >= target:
                    final_exit_price = target
                    exit_reason      = "target"
                    break
            else:  # SHORT
                if bar_high >= current_stop:
                    final_exit_price = current_stop
                    exit_reason      = "stop"
                    break
                if not partial_taken and bar_low <= partial_target:
                    partial_taken = True
                    current_stop  = entry
                if bar_low <= target:
                    final_exit_price = target
                    exit_reason      = "target"
                    break

        if final_exit_price is None:
            last_idx         = min(i + 1 + max_hold, len(bars) - 1)
            final_exit_price = float(bars.iloc[last_idx]["Close"])
            exit_reason      = "time"

        direction  = "LONG" if is_long else "SHORT"
        gross_pts  = (final_exit_price - entry) if is_long else (entry - final_exit_price)
        gross_pnl  = gross_pts * point_value
        round_trip = 2 * commission + 2 * slippage_pts * point_value
        pnl_net    = gross_pnl - round_trip

        signal_date = entry_day
        trade = {
            "date":          signal_date,
            "strategy":      strategy_lbl,
            "market":        market,
            "direction":     direction,
            "mode":          mode,
            "entry":         round(entry,            4),
            "entry_price":   round(entry,            4),
            "stop":          round(stop,             4),
            "target":        round(target,           4),
            "exit":          round(final_exit_price, 4),
            "exit_price":    round(final_exit_price, 4),
            "exit_reason":   exit_reason,
            "pnl_net":       round(pnl_net,          2),
            "risk_pts":      round(risk_pts,         4),
            "partial_taken": partial_taken,
            "gls_score":     int(row.get("vwap_gls_score", 0)),
            "of_score":      0,
        }
        trades.append(trade)

    return trades
