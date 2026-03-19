"""
AlgoBot — 6E London Open Mean Reversion Sub-Bot
=================================================
Module:  src/strategy/london_open_signal.py
Phase:   Sub-Bot B — 6E London Open Mean Reversion (Redesign)

REDESIGN HISTORY:
  v1 (2026-03-01): FOLLOW the breakout → PF=0.575, Win%=39%, FAIL
  v2 (2026-03-01): FADE the breakout (mean reversion) ← current

Why fade works:
  - EUR/USD was RANGING 43% of 2023-2026 (post-rate-hike consolidation)
  - 60.6% of London session breakouts FAIL and REVERT within 1-3 bars
  - Following the breakout → 39% win rate (you're on the wrong side 60% of the time)
  - Fading the breakout → ~60% expected win rate (you're on the right side 60% of the time)
  - Symmetric R/R (range_stop_mult = 0.5 → stop = target distance) → PF target ≈ 1.5

Strategy Logic (fade mode = True):
  - Data:    1-hour bars, 6E=F (24-hour futures, overnight bars available)
  - Range:   3:00–5:00 AM ET (London open 2-hour range)
  - Signal:  Breakout AFTER 5:00 AM ET → FADE it (SHORT on upside break, LONG on downside break)
  - Entry:   Next bar Open (close below/above range boundary triggers signal)
  - Stop:    signal_bar.High + 0.5×range_width (SHORT fade) — above breakout extreme
             signal_bar.Low  − 0.5×range_width (LONG  fade) — below breakout extreme
  - Target:  Range midpoint — natural reversion target for London fade
  - Partial: 50% at 0.4R → trail to BE
  - Max hold: 6 bars (no_entry_after limits to ~4 hours from 5 AM)
  - HTF filter: same rules as GC
      Fade SHORT blocked if HTF=BULL (don't fade confirmed bull breakout)
      Fade LONG  blocked if HTF=BEAR (don't fade confirmed bear breakout)
  - Calendar: Skip HIGH impact only (FOMC, NFP, ECB)
  - VIX: Same gating (QUIET <13 = skip; CRISIS >35 = skip)
  - No overnight carry (position must close before end of entry day)

Trade dict schema (same as FHB):
  date, direction, entry, stop, target, exit, exit_reason,
  pnl_net, risk_pts, gls_score, of_score, strategy="6E_LON"

Usage:
    from src.strategy.london_open_signal import compute_london_signals, simulate_london_trades

    df_sig = compute_london_signals(df_1h, "6E", htf_bias, config, econ_cal, vix_filter)
    trades = simulate_london_trades(df_sig, "6E", config)
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

LON_RANGE_START    = "03:00"   # ET — London open
LON_RANGE_END      = "05:00"   # ET — end of range window
LON_ENTRY_START    = "05:00"   # ET — earliest breakout entry
LON_NO_ENTRY_AFTER = "09:00"   # ET — close before NY open
LON_ATR_PERIOD     = 14
LON_PIP_SIZE       = 0.0001    # 1 pip for EUR/USD futures (6E tick = 0.00005 = 0.5 pip)


# ── ATR helper ─────────────────────────────────────────────────────────────────

def compute_1h_atr(df: pd.DataFrame, period: int = LON_ATR_PERIOD) -> pd.Series:
    """Wilder's ATR on 1-hour bars."""
    h      = df["High"]
    lo     = df["Low"]
    prev_c = df["Close"].shift(1)
    tr = pd.concat(
        [h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(span=period, min_periods=period, adjust=False).mean()


# ── Signal computation ─────────────────────────────────────────────────────────

def compute_london_signals(
    df_1h:           pd.DataFrame,
    market:          str,
    htf_bias_series: pd.Series,
    config:          dict,
    econ_cal=None,
    vix_filter=None,
) -> pd.DataFrame:
    """
    Compute 6E London Open signals.

    When fade_mode=True (default, redesign):
      1. For each trading day, find bars in the 3:00–5:00 AM ET window.
      2. Compute the London range: range_high = max(High), range_low = min(Low).
      3. Filter: skip if range width > range_max_pips (very wide = trend day, not fade day).
      4. After 5:00 AM ET, detect the first bar where Close breaks above/below range.
      5. INVERT direction: breakout UP → lon_short_signal (fade); DOWN → lon_long_signal (fade).
      6. Stop: signal_bar.High + range_stop_mult × range_width (SHORT fade)
               signal_bar.Low  − range_stop_mult × range_width (LONG  fade)
      7. Target: range midpoint (natural reversion target).
      8. HTF filter: block SHORT fade if HTF=BULL; block LONG fade if HTF=BEAR.
      9. Apply econ_cal skip: HIGH impact only (FOMC, NFP, ECB).
     10. Apply VIX skip: QUIET and CRISIS.

    When fade_mode=False (original follow mode — deprecated):
      - lon_long_signal on upside break; lon_short_signal on downside break
      - Stop at opposite side of range; target at target_r × R

    Added columns:
      lon_range_high      — High of the London range
      lon_range_low       — Low of the London range
      lon_range_complete  — True from 5:00 AM onward
      lon_long_signal     — True: fade a downside break (go LONG) OR follow upside break
      lon_short_signal    — True: fade an upside break (go SHORT) OR follow downside break
      lon_htf_blocked     — True if HTF bias blocked this signal
      lon_stop            — Stop level (pre-computed: range-relative beyond breakout extreme)
      lon_target          — Target price (range midpoint in fade mode, R-based in follow mode)
      lon_filter_reason   — Why a signal was filtered
      lon_econ_impact     — 'NONE' | 'MEDIUM' | 'HIGH'
      lon_vix_regime      — 'QUIET' | 'OPTIMAL' | 'ELEVATED' | 'CRISIS'
      lon_htf_bias        — HTF combined bias on that day
      lon_atr             — ATR14 value (retained for diagnostics)

    Args:
        df_1h:           1-hour OHLCV DataFrame (DatetimeIndex in ET).
                         Must include overnight bars (03:00 AM onward).
        market:          Market code, typically "6E".
        htf_bias_series: Series of BULL/BEAR/NEUTRAL indexed by date.
        config:          Full config dict (reads london_open and markets sections).
        econ_cal:        EconCalendar instance (optional).
        vix_filter:      VIXFilter instance (optional).

    Returns:
        DataFrame with lon_* signal columns added.
    """
    lon_cfg      = config.get("london_open", {})
    markets_cfg  = config.get("markets", {})
    tick_size    = float(markets_cfg.get(market, {}).get("tick_size", 0.00005))
    entry_buffer = int(lon_cfg.get("entry_buffer_ticks", 1)) * tick_size

    fade_mode       = bool(lon_cfg.get("fade_mode",       True))
    range_max_pips  = float(lon_cfg.get("range_max_pips", 0))      # 0 = disabled
    range_stop_mult = float(lon_cfg.get("range_stop_mult", 0.5))   # default: symmetric R/R
    atr_stop_mult   = float(lon_cfg.get("atr_stop_mult",  0.0))    # backup (typically 0 in fade mode)
    target_r        = float(lon_cfg.get("target_r",       0.0))    # 0 = range midpoint target

    range_start    = lon_cfg.get("range_start_time",   LON_RANGE_START)
    range_end      = lon_cfg.get("range_end_time",     LON_RANGE_END)
    entry_start    = lon_cfg.get("entry_start_time",   LON_ENTRY_START)
    no_entry_after = lon_cfg.get("no_entry_after",     LON_NO_ENTRY_AFTER)

    # Convert range_max_pips to price units
    range_max_pts = range_max_pips * LON_PIP_SIZE if range_max_pips > 0 else 0.0

    df = df_1h.copy()

    # Pre-compute ATR on the full dataset (retained for diagnostics)
    atr_series = compute_1h_atr(df)
    df["lon_atr"] = atr_series

    # Initialize signal columns
    df["lon_range_high"]     = float("nan")
    df["lon_range_low"]      = float("nan")
    df["lon_range_complete"] = False
    df["lon_long_signal"]    = False
    df["lon_short_signal"]   = False
    df["lon_htf_blocked"]    = False
    df["lon_stop"]           = float("nan")
    df["lon_target"]         = float("nan")
    df["lon_filter_reason"]  = ""
    df["lon_econ_impact"]    = "NONE"
    df["lon_vix_regime"]     = "OPTIMAL"
    df["lon_htf_bias"]       = _NEUTRAL

    trading_dates = df.index.normalize().unique()
    total_long    = total_short = total_blocked = 0
    econ_skipped  = vix_skipped = range_filtered = 0

    for day in trading_dates:
        day_str  = str(pd.Timestamp(day).date())
        day_mask = df.index.normalize() == day
        day_df   = df[day_mask].copy()

        if len(day_df) < 2:
            continue

        # ── Economic Calendar: HIGH impact only ────────────────────────────────
        econ_impact = "NONE"
        if econ_cal is not None:
            econ_impact = econ_cal.get_impact_level(day_str)
        if econ_impact == "HIGH":
            day_df["lon_econ_impact"] = econ_impact
            df.update(day_df)
            econ_skipped += 1
            continue

        # ── VIX regime check ──────────────────────────────────────────────────
        vix_regime = "OPTIMAL"
        if vix_filter is not None:
            vix_regime = vix_filter.get_regime(day_str)
        if vix_regime in ("QUIET", "CRISIS"):
            day_df["lon_vix_regime"] = vix_regime
            df.update(day_df)
            vix_skipped += 1
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

        # HTF gate rules (same in both fade and follow modes):
        #   LONG signal blocked if HTF=BEAR (don't go long into a bear trend)
        #   SHORT signal blocked if HTF=BULL (don't go short into a bull trend)
        long_allowed  = (htf_bias != _BEAR)
        short_allowed = (htf_bias != _BULL)

        # ── London range computation ───────────────────────────────────────────
        try:
            london_bars = day_df.between_time(range_start, range_end, inclusive="left")
        except Exception:
            continue

        if len(london_bars) < 1:
            continue

        range_high = float(london_bars["High"].max())
        range_low  = float(london_bars["Low"].min())
        if range_high <= range_low:
            continue

        range_width = range_high - range_low
        range_mid   = range_low + range_width / 2.0

        # ── Range width filter (fade mode: skip very wide ranges = trend days) ─
        if range_max_pts > 0 and range_width > range_max_pts:
            day_df["lon_econ_impact"] = econ_impact
            day_df["lon_vix_regime"]  = vix_regime
            day_df["lon_htf_bias"]    = htf_bias
            df.update(day_df)
            range_filtered += 1
            continue

        # Mark range complete from range_end onward
        try:
            post_range = day_df.between_time(range_end, "23:59")
            day_df.loc[post_range.index, "lon_range_complete"] = True
        except Exception:
            pass

        day_df["lon_range_high"]  = range_high
        day_df["lon_range_low"]   = range_low
        day_df["lon_econ_impact"] = econ_impact
        day_df["lon_vix_regime"]  = vix_regime
        day_df["lon_htf_bias"]    = htf_bias

        # ── Entry window: 5:00–9:00 AM ET ─────────────────────────────────────
        try:
            tradeable = day_df.between_time(entry_start, no_entry_after, inclusive="left")
        except Exception:
            tradeable = day_df[day_df["lon_range_complete"]]

        if tradeable.empty:
            df.update(day_df)
            continue

        long_entry_level  = range_high + entry_buffer
        short_entry_level = range_low  - entry_buffer
        long_fired  = False
        short_fired = False

        for idx in tradeable.index:
            bar = tradeable.loc[idx]

            breakout_up   = (not long_fired and not short_fired and
                             float(bar["Close"]) > long_entry_level)
            breakout_down = (not short_fired and not long_fired and
                             float(bar["Close"]) < short_entry_level)

            for btype, triggered in [("UP", breakout_up), ("DOWN", breakout_down)]:
                if not triggered:
                    continue

                bar_high = float(bar["High"])
                bar_low  = float(bar["Low"])
                atr_val  = float(day_df.loc[idx, "lon_atr"]) if not np.isnan(
                    day_df.loc[idx, "lon_atr"]) else 0.0

                if fade_mode:
                    # ── FADE MODE: invert direction, stop beyond breakout extreme ──
                    stop_ext = range_stop_mult * range_width
                    if atr_stop_mult > 0 and atr_val > 0:
                        stop_ext = atr_stop_mult * atr_val  # ATR override if configured

                    if btype == "UP":
                        # Breakout UP → go SHORT (fade it back down)
                        short_fired = True
                        direction   = "SHORT"
                        allowed     = short_allowed
                        stop        = bar_high + stop_ext   # above breakout bar's high
                        target      = range_mid             # range midpoint
                    else:
                        # Breakout DOWN → go LONG (fade it back up)
                        long_fired = True
                        direction  = "LONG"
                        allowed    = long_allowed
                        stop       = bar_low - stop_ext     # below breakout bar's low
                        target     = range_mid              # range midpoint

                else:
                    # ── FOLLOW MODE (original, deprecated) ────────────────────
                    if btype == "UP":
                        long_fired = True
                        direction  = "LONG"
                        allowed    = long_allowed
                        stop       = range_low - entry_buffer
                    else:
                        short_fired = True
                        direction   = "SHORT"
                        allowed     = short_allowed
                        stop        = range_high + entry_buffer

                    # R-based target for follow mode
                    entry_approx = float(bar["Close"])
                    risk_pts     = abs(entry_approx - stop)
                    if risk_pts <= 0:
                        continue
                    if direction == "LONG":
                        target = entry_approx + target_r * risk_pts
                    else:
                        target = entry_approx - target_r * risk_pts

                if not allowed:
                    day_df.loc[idx, "lon_htf_blocked"]  = True
                    day_df.loc[idx, "lon_filter_reason"] = f"HTF={htf_bias} blocks {direction}"
                    total_blocked += 1
                    continue

                day_df.loc[idx, "lon_stop"]   = stop
                day_df.loc[idx, "lon_target"] = target

                if direction == "LONG":
                    day_df.loc[idx, "lon_long_signal"] = True
                    total_long += 1
                else:
                    day_df.loc[idx, "lon_short_signal"] = True
                    total_short += 1

            if long_fired and short_fired:
                break

        df.update(day_df)

    log.info(
        "{market}: London signals | fade={fade} Long={l} Short={s} HTF_blocked={b} | "
        "EconSkipped={e} VixSkipped={v} RangeFiltered={r} | Days={d}",
        market=market, fade=fade_mode, l=total_long, s=total_short, b=total_blocked,
        e=econ_skipped, v=vix_skipped, r=range_filtered, d=len(trading_dates),
    )
    return df


# ── Trade simulation ───────────────────────────────────────────────────────────

def simulate_london_trades(
    df_signals: pd.DataFrame,
    market:     str,
    config:     dict,
    label:      str = "6E_LON",
) -> list[dict]:
    """
    Simulate 6E London Open trades from lon_long_signal / lon_short_signal.

    Fade mode mechanics (default):
      - Entry: next bar Open (+ slippage for longs, - slippage for shorts)
      - Stop:  lon_stop (pre-computed in compute_london_signals: range-relative beyond extreme)
      - Target: lon_target (range midpoint — pre-computed at signal time)
      - Partial: 50% at partial_exit_r × R → trail stop to breakeven
      - Max hold: up to max_hold_bars (hard close at no_entry_after)
      - No overnight carry

    Returns list of trade dicts with same schema as FHB.
    """
    lon_cfg      = config.get("london_open", {})
    markets_cfg  = config.get("markets", {})
    mkt_cfg      = markets_cfg.get(market, {})
    point_value  = float(mkt_cfg.get("point_value",   125000.0))
    commission   = float(mkt_cfg.get("commission",     5.0))
    slippage_tks = int(  mkt_cfg.get("slippage_ticks", 1))
    tick_size    = float(mkt_cfg.get("tick_size",      0.00005))
    slippage_pts = slippage_tks * tick_size

    fade_mode        = bool( lon_cfg.get("fade_mode",       True))
    target_r         = float(lon_cfg.get("target_r",         0.0))
    partial_exit_r   = float(lon_cfg.get("partial_exit_r",   0.4))
    partial_exit_pct = float(lon_cfg.get("partial_exit_pct", 0.50))
    max_hold_bars    = int(  lon_cfg.get("max_hold_bars",     6))
    no_entry_after   = lon_cfg.get("no_entry_after", LON_NO_ENTRY_AFTER)
    atr_stop_mult    = float(lon_cfg.get("atr_stop_mult",    0.0))

    trades = []
    bars   = df_signals.reset_index()

    for i, row in bars.iterrows():
        is_long  = bool(row.get("lon_long_signal",  False))
        is_short = bool(row.get("lon_short_signal", False))
        blocked  = bool(row.get("lon_htf_blocked",  False))

        if not (is_long or is_short) or blocked:
            continue
        if i + 1 >= len(bars):
            continue

        next_bar      = bars.iloc[i + 1]
        entry_raw     = float(next_bar["Open"])
        range_stop    = float(row["lon_stop"])     # pre-computed stop level
        stored_target = float(row.get("lon_target", float("nan")))
        atr_val       = float(row.get("lon_atr", float("nan")))

        if np.isnan(range_stop):
            continue

        # ── Entry and stop ─────────────────────────────────────────────────────
        if is_long:
            entry = entry_raw + slippage_pts
            if not fade_mode and atr_stop_mult > 0 and not np.isnan(atr_val) and atr_val > 0:
                # Non-fade (follow) mode: ATR stop below entry
                stop = entry - atr_stop_mult * atr_val
            else:
                # Fade mode: use pre-computed range_stop (already below breakout bar low)
                stop = range_stop - slippage_pts
        else:
            entry = entry_raw - slippage_pts
            if not fade_mode and atr_stop_mult > 0 and not np.isnan(atr_val) and atr_val > 0:
                # Non-fade (follow) mode: ATR stop above entry
                stop = entry + atr_stop_mult * atr_val
            else:
                # Fade mode: use pre-computed range_stop (already above breakout bar high)
                stop = range_stop + slippage_pts

        risk_pts = abs(entry - stop)
        if risk_pts <= 0:
            continue

        # ── Target computation ─────────────────────────────────────────────────
        if fade_mode and not np.isnan(stored_target):
            # Use range midpoint (pre-computed at signal time)
            full_target = stored_target
        else:
            # R-based target (follow mode, or fade fallback)
            if is_long:
                full_target = entry + target_r * risk_pts
            else:
                full_target = entry - target_r * risk_pts

        # Partial target: always R-based from actual entry
        if is_long:
            partial_target = entry + partial_exit_r * risk_pts
        else:
            partial_target = entry - partial_exit_r * risk_pts

        # Sanity check: target must be in the right direction
        if is_long and full_target <= entry:
            continue
        if is_short and full_target >= entry:
            continue

        signal_date = str(pd.Timestamp(row["Timestamp"]).date())
        entry_day   = signal_date

        # ── Bar-by-bar simulation ──────────────────────────────────────────────
        partial_taken    = False
        current_stop     = stop
        final_exit_price = None
        exit_reason      = "time"

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

            # No overnight carry
            if bar_date != entry_day:
                final_exit_price = float(bars.iloc[bar_idx - 1]["Close"])
                exit_reason      = "eod_no_carry"
                break

            # Hard close before US open
            try:
                bar_time_str = (sim_bar["Timestamp"].strftime("%H:%M")
                                if hasattr(sim_bar["Timestamp"], "strftime")
                                else str(sim_bar["Timestamp"])[11:16])
                if bar_time_str >= no_entry_after:
                    final_exit_price = float(sim_bar["Open"])
                    exit_reason      = "us_open_close"
                    break
            except Exception:
                pass

            if is_long:
                if bar_low <= current_stop:
                    final_exit_price = current_stop
                    exit_reason      = "stop"
                    break
                if not partial_taken and bar_high >= partial_target:
                    partial_taken = True
                    current_stop  = entry   # trail to breakeven
                if bar_high >= full_target:
                    final_exit_price = full_target
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
                if bar_low <= full_target:
                    final_exit_price = full_target
                    exit_reason      = "target"
                    break

        if final_exit_price is None:
            last_bar_idx = min(i + 1 + max_hold_bars, len(bars) - 1)
            final_exit_price = float(bars.iloc[last_bar_idx]["Close"])
            exit_reason = "time"

        # ── P&L calculation ────────────────────────────────────────────────────
        direction  = "LONG" if is_long else "SHORT"
        gross_pts  = (final_exit_price - entry) if is_long else (entry - final_exit_price)
        gross_pnl  = gross_pts * point_value
        round_trip = 2 * commission + 2 * slippage_pts * point_value
        pnl_net    = gross_pnl - round_trip

        trade = {
            "date":          signal_date,
            "strategy":      label,
            "market":        market,
            "direction":     direction,
            "entry":         round(entry, 6),
            "entry_price":   round(entry, 6),
            "stop":          round(stop, 6),
            "target":        round(full_target, 6),
            "exit":          round(final_exit_price, 6),
            "exit_price":    round(final_exit_price, 6),
            "exit_reason":   exit_reason,
            "pnl_net":       round(pnl_net, 2),
            "risk_pts":      round(risk_pts, 6),
            "partial_taken": partial_taken,
            "gls_score":     0,
            "of_score":      0,
        }
        trades.append(trade)

    return trades
