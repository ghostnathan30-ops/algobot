"""
AlgoBot — GC (Gold) Mean Reversion Sub-Bot
============================================
Module:  src/strategy/gc_signal.py
Phase:   Sub-Bot A — GC Mean Reversion
Purpose: Generate mean-reversion entry signals for Gold futures by inverting
         the First Hour Breakout (FHB) signal direction.

Why GC fails breakout, wins reversal:
  - GC FHB PF = 1.07 → ~49% of GC range breakouts fail and reverse
  - Gold has strong VWAP magnetism (macro anchoring, round-number gravity)
  - Gold breakouts are primarily news-driven spikes that revert within 1-3 bars
  - Strategy: use FHB signal infrastructure as-is, but invert the direction

Strategy Logic:
  - Data:    1-hour bars, GC=F
  - Range:   9:30–10:30 AM ET (identical to FHB — reuses compute_fhb_signals)
  - Signal:  FHB signals LONG → GC goes SHORT (fade); FHB signals SHORT → GC goes LONG
  - Entry:   1 tick beyond range boundary (same as FHB, same bar)
  - Stop:    range_boundary + 1.0× ATR14 above the breakout extreme
  - Target:  VWAP (dynamic) or range midpoint (static fallback) — typically 0.5–1.0R
  - Max hold: 3 bars (3 hours) — gold reverts fast or not at all
  - HTF filter:
      FHB_LONG signal + HTF=BEAR or NEUTRAL → GC SHORT (fade) ✓
      FHB_LONG signal + HTF=BULL → SKIP (dangerous to fade a confirmed bull breakout)
  - Calendar: Skip HIGH + MEDIUM impact (CPI/PPI/PCE all move gold aggressively)
  - VIX: Same gating as main bot

Trade dict schema (same as FHB):
  date, direction, entry, stop, target, exit, exit_reason,
  pnl_net, risk_pts, gls_score, of_score, strategy="GC_REV"

Usage:
    from src.strategy.gc_signal import compute_gc_signals, simulate_gc_trades

    df_sig = compute_gc_signals(df_1h, "GC", htf_bias, regime, config, econ_cal, vix_filter)
    trades = simulate_gc_trades(df_sig, "GC", config)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Allow importing from project root when run directly
_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import get_logger

log = get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
_BULL    = "BULL"
_BEAR    = "BEAR"
_NEUTRAL = "NEUTRAL"

GC_ATR_PERIOD    = 14    # Wilder ATR on 1-hour bars
GC_RANGE_START   = "09:30"
GC_RANGE_END     = "10:30"
GC_NO_ENTRY_AFTER = "13:00"


# ── ATR helper (same as FHB) ───────────────────────────────────────────────────

def compute_1h_atr(df: pd.DataFrame, period: int = GC_ATR_PERIOD) -> pd.Series:
    """Wilder's ATR on 1-hour bars."""
    h      = df["High"]
    lo     = df["Low"]
    prev_c = df["Close"].shift(1)
    tr = pd.concat(
        [h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(span=period, min_periods=period, adjust=False).mean()


# ── Signal computation ─────────────────────────────────────────────────────────

def compute_gc_signals(
    df_1h:           pd.DataFrame,
    market:          str,
    htf_bias_series: pd.Series,
    regime_series:   pd.Series,
    config:          dict,
    econ_cal=None,
    vix_filter=None,
) -> pd.DataFrame:
    """
    Compute GC mean-reversion signals by inverting FHB direction.

    The logic:
      1. Detect the 9:30–10:30 AM ET first-hour range (same as FHB).
      2. After 10:30 AM, detect the first bar that breaks out of the range.
      3. INVERT: if price breaks UP → signal is SHORT (fade the breakout).
                 if price breaks DOWN → signal is LONG (fade the breakout).
      4. Apply HTF fade filter:
           Skip SHORT fade if HTF=BULL (confirming the upside breakout — don't fade it)
           Skip LONG  fade if HTF=BEAR (confirming the downside breakout — don't fade it)
      5. Apply econ_cal skip (HIGH + MEDIUM impact for GC — gold is very news-sensitive)
      6. Apply VIX skip (QUIET <13 or CRISIS >35)

    Added columns:
      gc_range_high      — High of the first-hour range
      gc_range_low       — Low of the first-hour range
      gc_range_complete  — True from 10:30 AM onward
      gc_long_signal     — True: fade a downside breakout (go LONG)
      gc_short_signal    — True: fade an upside breakout (go SHORT)
      gc_htf_blocked     — True if HTF bias blocked the fade
      gc_stop            — Stop loss level (ATR-based, above the breakout extreme)
      gc_target          — Target (VWAP if available, else range midpoint)
      gc_filter_reason   — Why a signal was filtered (for diagnostics)
      gc_atr             — ATR at signal bar

    Args:
        df_1h:           1-hour OHLCV DataFrame (DatetimeIndex in ET).
        market:          Market code, typically "GC".
        htf_bias_series: Series of BULL/BEAR/NEUTRAL indexed by date.
        regime_series:   Series of regime strings indexed by date.
        config:          Full config dict (reads gc_reversion and markets sections).
        econ_cal:        EconCalendar instance (optional).
        vix_filter:      VIXFilter instance (optional).

    Returns:
        DataFrame with gc_* signal columns added.
    """
    gc_cfg       = config.get("gc_reversion", {})
    markets_cfg  = config.get("markets", {})
    tick_size    = float(markets_cfg.get(market, {}).get("tick_size", 0.10))
    entry_buffer = tick_size   # 1 tick buffer

    atr_stop_mult      = float(gc_cfg.get("atr_stop_mult",       1.0))
    skip_medium_impact = bool( gc_cfg.get("skip_medium_impact",  True))
    skip_gap_days      = bool( gc_cfg.get("skip_gap_days",       True))
    gap_threshold      = float(gc_cfg.get("gap_threshold",       0.005))

    df = df_1h.copy()

    # Compute ATR on full dataset
    atr_series = compute_1h_atr(df)
    df["gc_atr"] = atr_series

    # Add VWAP (daily reset) if not already present
    try:
        from src.utils.orderflow import add_daily_vwap
        df = add_daily_vwap(df)
    except ImportError:
        df["vwap"] = float("nan")

    # Initialize signal columns
    df["gc_range_high"]     = float("nan")
    df["gc_range_low"]      = float("nan")
    df["gc_range_complete"] = False
    df["gc_long_signal"]    = False
    df["gc_short_signal"]   = False
    df["gc_htf_blocked"]    = False
    df["gc_stop"]           = float("nan")
    df["gc_target"]         = float("nan")
    df["gc_filter_reason"]  = ""
    df["gc_gls_score"]      = 0
    df["gc_econ_impact"]    = "NONE"
    df["gc_vix_regime"]     = "OPTIMAL"
    df["gc_htf_bias"]       = _NEUTRAL

    trading_dates  = df.index.normalize().unique()
    total_long     = total_short = total_blocked = 0
    econ_skipped   = vix_skipped = 0
    prev_close     = None

    for day in trading_dates:
        day_str  = str(pd.Timestamp(day).date())
        day_mask = df.index.normalize() == day
        day_df   = df[day_mask].copy()

        today_last_close = float(day_df.iloc[-1]["Close"]) if not day_df.empty else None

        if len(day_df) < 3:
            prev_close = today_last_close
            continue

        # ── Economic Calendar check ────────────────────────────────────────────
        econ_impact = "NONE"
        if econ_cal is not None:
            econ_impact = econ_cal.get_impact_level(day_str)

        skip_min_impact = "MEDIUM" if skip_medium_impact else "HIGH"
        if econ_cal is not None and econ_cal.skip_today(day_str, min_impact=skip_min_impact):
            day_df["gc_econ_impact"] = econ_impact
            df.update(day_df)
            prev_close = today_last_close
            econ_skipped += 1
            continue

        # ── VIX regime check ──────────────────────────────────────────────────
        vix_regime = "OPTIMAL"
        if vix_filter is not None:
            vix_regime = vix_filter.get_regime(day_str)
        if vix_regime in ("QUIET", "CRISIS"):
            day_df["gc_vix_regime"] = vix_regime
            df.update(day_df)
            prev_close = today_last_close
            vix_skipped += 1
            continue

        # ── Gap-open filter ────────────────────────────────────────────────────
        if skip_gap_days and prev_close is not None and not day_df.empty:
            today_open = float(day_df.iloc[0]["Open"])
            if today_open > 0 and prev_close > 0:
                gap_pct = abs(today_open - prev_close) / prev_close
                if gap_pct > gap_threshold:
                    prev_close = today_last_close
                    continue

        # ── HTF bias ──────────────────────────────────────────────────────────
        htf_bias = _NEUTRAL
        try:
            if len(htf_bias_series) > 0:
                bias_idx = htf_bias_series.copy()
                bias_idx.index = [str(pd.Timestamp(d).date()) for d in htf_bias_series.index]
                prior    = bias_idx[bias_idx.index <= day_str]
                htf_bias = str(prior.iloc[-1]) if len(prior) > 0 else _NEUTRAL
        except Exception:
            pass

        # ── First-hour range ──────────────────────────────────────────────────
        try:
            first_hour = day_df.between_time(GC_RANGE_START, GC_RANGE_END, inclusive="left")
        except Exception:
            continue
        if first_hour.empty:
            continue

        range_high = float(first_hour["High"].max())
        range_low  = float(first_hour["Low"].min())
        if range_high <= range_low:
            continue

        range_mid = (range_high + range_low) / 2.0

        # Mark range complete from 10:30 onwards
        try:
            post_range = day_df.between_time(GC_RANGE_END, "23:59")
            day_df.loc[post_range.index, "gc_range_complete"] = True
        except Exception:
            pass

        day_df["gc_range_high"]  = range_high
        day_df["gc_range_low"]   = range_low
        day_df["gc_econ_impact"] = econ_impact
        day_df["gc_vix_regime"]  = vix_regime
        day_df["gc_htf_bias"]    = htf_bias

        # ── Tradeable window: 10:30 to 1:00 PM ────────────────────────────────
        try:
            tradeable = day_df.between_time(GC_RANGE_END, GC_NO_ENTRY_AFTER)
        except Exception:
            tradeable = day_df[day_df["gc_range_complete"]]

        long_entry_level  = range_high + entry_buffer   # breakout UP triggers GC SHORT fade
        short_entry_level = range_low  - entry_buffer   # breakout DOWN triggers GC LONG fade
        long_fired  = False
        short_fired = False

        for idx in tradeable.index:
            bar     = tradeable.loc[idx]
            atr_val = float(day_df.loc[idx, "gc_atr"]) if not np.isnan(day_df.loc[idx, "gc_atr"]) else 0.0

            # Detect breakouts → invert them as fade signals
            # Breakout UP (close > range_high + buffer) → GC SHORT signal
            # Breakout DOWN (close < range_low - buffer) → GC LONG signal
            breakout_up   = (not long_fired  and not short_fired and
                             float(bar["Close"]) > long_entry_level)
            breakout_down = (not short_fired and not long_fired  and
                             float(bar["Close"]) < short_entry_level)

            for breakout_type, fired_flag in [
                ("UP",   breakout_up),
                ("DOWN", breakout_down),
            ]:
                if not fired_flag:
                    continue

                if breakout_type == "UP":
                    long_fired = True
                    # Fade direction: SHORT
                    fade_direction = "SHORT"
                    # Block if HTF confirms the breakout direction
                    htf_blocked = (htf_bias == _BULL)
                    if htf_blocked:
                        day_df.loc[idx, "gc_htf_blocked"]    = True
                        day_df.loc[idx, "gc_filter_reason"]  = f"HTF={htf_bias} confirms UP breakout"
                        total_blocked += 1
                        continue
                    # Entry approximation (actual entry = next bar open, but use close for sizing)
                    _gc_entry_approx = float(bar["Close"])
                    # Stop: above the breakout extreme AND above the entry bar close.
                    # Bug guard: if a big-breakout bar closes far above range_high, the
                    # range-anchored stop could be BELOW the close, putting it on the
                    # wrong side for a SHORT (stop below entry = triggers on profit moves).
                    # Fix: take the max so stop is always above both the range boundary
                    # and the entry bar close.
                    _atr_stop_dist = atr_stop_mult * atr_val if atr_val > 0 else entry_buffer
                    stop_level = max(
                        range_high + _atr_stop_dist,
                        _gc_entry_approx + _atr_stop_dist,
                    )
                    # Target: VWAP if available, else range midpoint
                    vwap_val    = float(bar.get("vwap", float("nan"))) if hasattr(bar, "get") else float("nan")
                    target_level = vwap_val if not np.isnan(vwap_val) else range_mid
                    # R:R filter: skip if reward/risk < 0.75. If VWAP or range_mid is
                    # too close to entry, the expected value doesn't justify the trade.
                    # Economic basis: GC mean-reversion has mean R:R ≈ 0.8; trades with
                    # R:R < 0.75 are below-average expected value and drag down PF.
                    _gc_risk   = abs(_gc_entry_approx - stop_level)
                    _gc_reward = abs(_gc_entry_approx - target_level)
                    if _gc_risk > 0 and (_gc_reward / _gc_risk) < 0.75:
                        day_df.loc[idx, "gc_filter_reason"] = (
                            f"rr={_gc_reward/_gc_risk:.2f}<0.75 (reward={_gc_reward:.2f} risk={_gc_risk:.2f})"
                        )
                        total_blocked += 1
                        continue

                    # GLS score: composite fade quality
                    # +10 if HTF is NEUTRAL (no trend to fight the fade)
                    # +10 if the breakout bar has a rejection wick in the fade direction
                    # +10 if breakout is well-extended from range boundary (>1.0×ATR)
                    _gc_gls = 65
                    if htf_bias == _NEUTRAL:            _gc_gls += 10
                    bar_wick_up = float(bar["High"]) - float(bar["Close"])  # upper wick = SHORT rejection
                    if atr_val > 0 and bar_wick_up >= 0.25 * atr_val:      _gc_gls += 10
                    _gc_ext = abs(float(bar["Close"]) - range_high)
                    if atr_val > 0 and _gc_ext >= 1.0 * atr_val:           _gc_gls += 10
                    _gc_gls = min(_gc_gls, 95)
                    day_df.loc[idx, "gc_short_signal"] = True
                    day_df.loc[idx, "gc_gls_score"]    = _gc_gls
                    total_short += 1

                else:  # DOWN
                    short_fired = True
                    # Fade direction: LONG
                    fade_direction = "LONG"
                    # Block if HTF confirms the breakout direction
                    htf_blocked = (htf_bias == _BEAR)
                    if htf_blocked:
                        day_df.loc[idx, "gc_htf_blocked"]    = True
                        day_df.loc[idx, "gc_filter_reason"]  = f"HTF={htf_bias} confirms DOWN breakout"
                        total_blocked += 1
                        continue
                    # Entry approximation
                    _gc_entry_approx = float(bar["Close"])
                    # Stop: below the breakout extreme AND below the entry bar close.
                    # Mirror fix of the SHORT case: if a big-breakout bar closes far
                    # below range_low, the range-anchored stop could be ABOVE the close,
                    # putting it on the wrong side for a LONG.
                    _atr_stop_dist = atr_stop_mult * atr_val if atr_val > 0 else entry_buffer
                    stop_level = min(
                        range_low - _atr_stop_dist,
                        _gc_entry_approx - _atr_stop_dist,
                    )
                    # Target: VWAP if available, else range midpoint
                    vwap_val    = float(bar.get("vwap", float("nan"))) if hasattr(bar, "get") else float("nan")
                    target_level = vwap_val if not np.isnan(vwap_val) else range_mid
                    # R:R filter (same rationale as SHORT fade above)
                    _gc_risk   = abs(_gc_entry_approx - stop_level)
                    _gc_reward = abs(_gc_entry_approx - target_level)
                    if _gc_risk > 0 and (_gc_reward / _gc_risk) < 0.75:
                        day_df.loc[idx, "gc_filter_reason"] = (
                            f"rr={_gc_reward/_gc_risk:.2f}<0.75 (reward={_gc_reward:.2f} risk={_gc_risk:.2f})"
                        )
                        total_blocked += 1
                        continue

                    # GLS score for LONG fade
                    _gc_gls = 65
                    if htf_bias == _NEUTRAL:            _gc_gls += 10
                    bar_wick_down = float(bar["Close"]) - float(bar["Low"])  # lower wick = LONG rejection
                    if atr_val > 0 and bar_wick_down >= 0.25 * atr_val:     _gc_gls += 10
                    _gc_ext = abs(float(bar["Close"]) - range_low)
                    if atr_val > 0 and _gc_ext >= 1.0 * atr_val:            _gc_gls += 10
                    _gc_gls = min(_gc_gls, 95)
                    day_df.loc[idx, "gc_long_signal"] = True
                    day_df.loc[idx, "gc_gls_score"]   = _gc_gls
                    total_long += 1

                day_df.loc[idx, "gc_stop"]   = stop_level
                day_df.loc[idx, "gc_target"] = target_level

            if long_fired and short_fired:
                break

        df.update(day_df)
        prev_close = today_last_close

    log.info(
        "{market}: GC signals | Long(fade)={l} Short(fade)={s} HTF_blocked={b} | "
        "EconSkipped={e} VixSkipped={v} | Days={d}",
        market=market, l=total_long, s=total_short, b=total_blocked,
        e=econ_skipped, v=vix_skipped, d=len(trading_dates),
    )
    return df


# ── Trade simulation ───────────────────────────────────────────────────────────

def simulate_gc_trades(
    df_signals: pd.DataFrame,
    market:     str,
    config:     dict,
    label:      str = "GC_REV",
) -> list[dict]:
    """
    Simulate GC mean-reversion trades from gc_long_signal / gc_short_signal.

    Mechanics (different from FHB):
      - Max hold = 3 bars (GC reverts fast or not at all)
      - Target = gc_target column (VWAP or range midpoint, not fixed-R)
      - Partial exit at 0.5R (50%) → trail stop to breakeven
      - No overnight carry (GC overnight gap risk too high)
      - Stop = gc_stop column (ATR-based, placed beyond the breakout extreme)

    Returns list of trade dicts with same schema as FHB:
      date, direction, entry, exit, exit_reason, pnl_net, risk_pts,
      entry_price, exit_price, strategy, market, gls_score, of_score
    """
    gc_cfg       = config.get("gc_reversion", {})
    markets_cfg  = config.get("markets", {})
    mkt_cfg      = markets_cfg.get(market, {})
    point_value  = float(mkt_cfg.get("point_value",   100.0))
    commission   = float(mkt_cfg.get("commission",     5.0))
    slippage_tks = int(  mkt_cfg.get("slippage_ticks", 1))
    tick_size    = float(mkt_cfg.get("tick_size",      0.10))
    slippage_pts = slippage_tks * tick_size

    target_r         = float(gc_cfg.get("target_r",         1.0))
    max_hold_bars    = int(  gc_cfg.get("max_hold_bars",     3))
    partial_exit_r   = float(gc_cfg.get("partial_exit_r",   0.5))
    partial_exit_pct = float(gc_cfg.get("partial_exit_pct", 0.50))

    trades = []
    bars   = df_signals.reset_index()

    for i, row in bars.iterrows():
        is_long  = bool(row.get("gc_long_signal",  False))
        is_short = bool(row.get("gc_short_signal", False))
        blocked  = bool(row.get("gc_htf_blocked",  False))

        if not (is_long or is_short) or blocked:
            continue
        if i + 1 >= len(bars):
            continue

        next_bar    = bars.iloc[i + 1]
        entry_raw   = float(next_bar["Open"])
        stop_level  = float(row["gc_stop"])   if not np.isnan(row["gc_stop"])   else 0.0
        target_vwap = float(row["gc_target"]) if not np.isnan(row["gc_target"]) else 0.0

        if is_long:
            entry    = entry_raw + slippage_pts
            stop     = stop_level
        else:
            entry    = entry_raw - slippage_pts
            stop     = stop_level

        risk_pts = abs(entry - stop)
        if risk_pts <= 0:
            continue

        # Use VWAP target, with R-based fallback
        if target_vwap > 0:
            target = target_vwap
        else:
            target = entry + target_r * risk_pts * (1 if is_long else -1)

        # Partial target (0.5R default)
        partial_target = entry + partial_exit_r * risk_pts * (1 if is_long else -1)

        signal_date = str(pd.Timestamp(row["Timestamp"]).date())
        entry_day   = signal_date

        # ── Bar-by-bar simulation ──────────────────────────────────────────────
        partial_taken     = False
        current_stop      = stop
        final_exit_price  = None
        exit_reason       = "time"
        size_remaining    = 1.0   # fraction of position remaining

        for j in range(1, max_hold_bars + 1):
            bar_idx = i + 1 + j
            if bar_idx >= len(bars):
                final_exit_price = float(bars.iloc[bar_idx - 1]["Close"])
                exit_reason      = "eod"
                break

            sim_bar  = bars.iloc[bar_idx]
            bar_high = float(sim_bar["High"])
            bar_low  = float(sim_bar["Low"])
            bar_date = str(pd.Timestamp(sim_bar["Timestamp"]).date())

            # No overnight carry — exit at end of entry day
            if bar_date != entry_day:
                final_exit_price = float(bars.iloc[bar_idx - 1]["Close"])
                exit_reason      = "eod_no_carry"
                break

            if is_long:
                # Stop hit
                if bar_low <= current_stop:
                    if not partial_taken:
                        # Priority 1B: partial stop at halfway
                        half_stop = entry - 0.5 * risk_pts
                        if bar_low <= half_stop:
                            final_exit_price = (half_stop + current_stop) / 2.0
                        else:
                            final_exit_price = current_stop
                    else:
                        final_exit_price = current_stop
                    exit_reason = "stop"
                    break
                # Partial take at partial_target
                if not partial_taken and bar_high >= partial_target:
                    partial_taken = True
                    current_stop  = entry  # trail to breakeven after partial
                # Full target
                if bar_high >= target:
                    final_exit_price = target
                    exit_reason      = "target"
                    break
            else:  # SHORT
                # Stop hit
                if bar_high >= current_stop:
                    if not partial_taken:
                        half_stop = entry + 0.5 * risk_pts
                        if bar_high >= half_stop:
                            final_exit_price = (half_stop + current_stop) / 2.0
                        else:
                            final_exit_price = current_stop
                    else:
                        final_exit_price = current_stop
                    exit_reason = "stop"
                    break
                # Partial take at partial_target
                if not partial_taken and bar_low <= partial_target:
                    partial_taken = True
                    current_stop  = entry
                # Full target
                if bar_low <= target:
                    final_exit_price = target
                    exit_reason      = "target"
                    break

        if final_exit_price is None:
            # Time exit — exit at close of last bar
            last_bar_idx = min(i + 1 + max_hold_bars, len(bars) - 1)
            final_exit_price = float(bars.iloc[last_bar_idx]["Close"])
            exit_reason = "time"

        # ── P&L calculation ────────────────────────────────────────────────────
        exit_price  = final_exit_price
        direction   = "LONG" if is_long else "SHORT"
        gross_pts   = (exit_price - entry) if is_long else (entry - exit_price)
        gross_pnl   = gross_pts * point_value
        round_trip  = 2 * commission + 2 * slippage_pts * point_value
        pnl_net     = gross_pnl - round_trip

        trade = {
            "date":         signal_date,
            "strategy":     "GC_REV",
            "market":       market,
            "direction":    direction,
            "entry":        round(entry, 4),
            "entry_price":  round(entry, 4),
            "stop":         round(stop,  4),
            "target":       round(target, 4),
            "exit":         round(exit_price, 4),
            "exit_price":   round(exit_price, 4),
            "exit_reason":  exit_reason,
            "pnl_net":      round(pnl_net, 2),
            "risk_pts":     round(risk_pts, 4),
            "partial_taken": partial_taken,
            "gls_score":    int(row.get("gc_gls_score", 0)),
            "of_score":     0,
        }
        trades.append(trade)

    return trades
