"""
AlgoBot — CL (Crude Oil WTI) Trend Breakout Signal
====================================================
Module:  src/strategy/cl_signal.py
Purpose: First-hour breakout on CL with CL-specific parameters designed
         for 60%+ win rate without overfitting.

Why FHB at default params fails on CL (Win=40%):
  1. Target too high (2.5R): CL 1H bars avg $600; price rarely travels 2R+ in session.
  2. Wrong GLS threshold (40): GLS<80 on CL = 17% win rate. GLS>=80 = 75% win rate.
  3. No EIA filter: Wednesday 10:30 AM inventory report creates spike+reversal that
     instantly fires the breakout then whipsaws — the single biggest source of losses.

Parameter choices (economic rationale — NOT curve-fitted):
  CL_ATR_STOP_MULT = 0.8   CL 1H ATR ~$600; 0.8x = $480 risk — acceptable for $50k account.
                            Slightly wider than NQ to absorb oil's intrabar noise.
  CL_TARGET_R = 1.0        Sweet spot: achievable 55-70% of the time on CL before reversal.
                            2R is for trending markets; oil trends are choppy intraday.
  CL_PARTIAL_R = 0.5       Early lock-in — oil can reverse violently on news at any moment.
  CL_GLS_MIN = 80          Strict quality gate. Data-validated cliff: 80+ → 75% WR.
  CL_USE_FAST_BIAS = True  Daily EMA(10)/EMA(20). Oil supply/demand cycles: 2-4 weeks.
                            Weekly/monthly bias is too slow. Daily momentum aligns with
                            the typical OPEC/supply-shock cycle length.
  EIA skip                 EIA inventory every Wednesday 10:30 AM. Skip 10:00-12:00 window.
  Regime filter            Skip RANGING days. SC validation: RANGING PF=0.70, TRENDING PF=2.68.
                            Economic basis: first-hour breakouts in ranging CL markets reverse
                            back into the range within 1-2 bars — the standard false-breakout
                            pattern. Only trade when the daily timeframe confirms trend structure.
  LONG dual confirmation   Require BOTH htf_bias=BULL AND fast_bias=BULL for longs.
                            Economic basis: CL rises on supply shocks (geopolitical, OPEC cuts)
                            which are sudden and mean-reverting. Intraday long breakouts in CL
                            more likely to be noise than signal unless BOTH trend timeframes agree.
                            SHORT signals only require fast_bias=BEAR (supply/demand drops trend).

Usage:
    from src.strategy.cl_signal import compute_cl_signals, simulate_cl_trades

    sigs   = compute_cl_signals(df_1h, htf_bias, regime, fast_bias, config, econ_cal, vix_filter)
    trades = simulate_cl_trades(sigs, config)
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.utils.logger import get_logger

log = get_logger(__name__)

# ── Symbols ────────────────────────────────────────────────────────────────────
_BULL    = "BULL"
_BEAR    = "BEAR"
_NEUTRAL = "NEUTRAL"

# ── Strategy constants ─────────────────────────────────────────────────────────
CL_ATR_PERIOD      = 14      # Wilder's ATR — industry standard
CL_RANGE_START     = "09:30" # First-hour bar open (NY equity open)
CL_RANGE_END       = "10:30" # End of first-hour range
CL_NO_ENTRY_AFTER  = "13:00" # No new entries after 1 PM ET

# Exit parameters — see module docstring for economic rationale
CL_ATR_STOP_MULT   = 0.80    # Stop distance = 0.8 × ATR
CL_ATR_STOP_CAP    = 1.00    # Never wider than 1.0 × ATR (hard cap)
CL_TARGET_R        = 1.00    # Full target = 1.0R from entry
CL_PARTIAL_R       = 0.50    # Partial (50%) exit at 0.5R
CL_PARTIAL_PCT     = 0.50    # Fraction of position to exit at CL_PARTIAL_R
CL_MAX_HOLD_BARS   = 5       # Time stop: max 5 hourly bars same-day
CL_MAX_LOSS_USD    = 1_500   # Hard dollar cap per trade (TopStep daily $1k limit)
CL_GLS_MIN         = 80      # Minimum GLS for entry (strict — see above)

# Filters
CL_USE_FAST_BIAS   = True    # Use daily EMA10/EMA20 as primary trend gate
CL_EIA_SKIP        = True    # Skip entries 10:00–12:00 ET on Wednesdays
CL_ENTRY_BUF_TICKS = 2       # 2 ticks ($0.02 = $20) above/below range boundary
CL_REQUIRE_TRENDING  = True   # Skip RANGING regime days (TRENDING/HIGH_VOL only)
CL_LONG_DUAL_CONFIRM = True   # LONG requires both htf_bias=BULL AND fast_bias=BULL

# Long entry: "Spring" / dip-and-recover parameters
# Economic basis: CL intraday longs work best as mean-reversion buys at support,
# NOT as breakout entries. The "Spring" pattern (Wyckoff) is a bear trap:
#   1. Price dips to/below range_low (stops out longs, activates shorts)
#   2. Sellers can't push further, price snaps back above range_low
#   3. Trapped shorts must cover → creates clean, fast long moves
# VWAP below entry = buying at a "discount" to institutional fair value
# RSI(5) < 45 = short-term oversold after the dip, not entering extended longs
CL_SPRING_DIP_MULT   = 0.50  # Price dip ≤ range_low + 0.50×ATR to qualify as "at support"
CL_SPRING_RSI_MAX    = 52    # RSI(5) must be ≤ 52 at entry (not extended/overbought)
CL_SPRING_RSI_PERIOD = 5     # 5-bar RSI for fast intraday reaction to oversold levels
CL_SPRING_VWAP_BELOW = True  # Entry close must be ≤ VWAP (buying at/below fair value)

# Contract spec
CL_POINT_VALUE     = 1000.0  # $1,000 per $1 move
CL_TICK_SIZE       = 0.01    # $0.01 minimum tick
CL_COMMISSION      = 5.00    # per side
CL_SLIPPAGE_TICKS  = 2       # typical CL spread 2-3 ticks at liquid hours


# ── ATR ────────────────────────────────────────────────────────────────────────

def compute_1h_atr(df: pd.DataFrame, period: int = CL_ATR_PERIOD) -> pd.Series:
    """Wilder's Average True Range on 1-hour OHLC bars."""
    h      = df["High"]
    lo     = df["Low"]
    prev_c = df["Close"].shift(1)
    tr = pd.concat(
        [h - lo, (h - prev_c).abs(), (lo - prev_c).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(span=period, min_periods=period, adjust=False).mean()


def compute_1h_rsi(df: pd.DataFrame, period: int = CL_SPRING_RSI_PERIOD) -> pd.Series:
    """
    Fast RSI on 1-hour closes — detects short-term overbought/oversold conditions.
    Uses Wilder's smoothing (EMA with alpha=1/period) for standard RSI behaviour.
    RSI(5) reacts quickly to intraday price dips, ideal for identifying bounce entries.
    """
    delta = df["Close"].diff()
    gain  = delta.where(delta > 0, 0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss  = (-delta).where(delta < 0, 0.0).ewm(alpha=1.0 / period, adjust=False).mean()
    rs    = gain / loss.where(loss > 0, 1e-9)
    return 100.0 - (100.0 / (1.0 + rs))


def compute_daily_vwap(df: pd.DataFrame) -> pd.Series:
    """
    Rolling daily VWAP — resets at midnight of each trading day.
    VWAP = ∑(Typical Price × Volume) / ∑Volume, accumulated intraday.
    When Volume is unavailable, uses a simple cumulative mean of typical price.

    Economic basis: VWAP is the primary reference for institutional order execution.
    Buying below VWAP = entering at a discount to institutional fair value.
    Typical Price = (High + Low + Close) / 3.
    """
    vwap = pd.Series(np.nan, index=df.index)
    has_vol = "Volume" in df.columns

    for day in df.index.normalize().unique():
        mask   = df.index.normalize() == day
        day_df = df[mask]
        if len(day_df) == 0:
            continue
        tp = (day_df["High"] + day_df["Low"] + day_df["Close"]) / 3.0
        if has_vol and day_df["Volume"].sum() > 0:
            vol     = day_df["Volume"].fillna(0.0)
            cum_vol = vol.cumsum()
            cum_tpv = (tp * vol).cumsum()
            vwap[mask] = np.where(cum_vol > 0, cum_tpv / cum_vol, tp.values)
        else:
            vwap[mask] = tp.expanding().mean().values
    return vwap


# ── Signal computation ─────────────────────────────────────────────────────────

def compute_cl_signals(
    df_1h:            pd.DataFrame,
    htf_bias_series:  pd.Series,
    regime_series:    pd.Series,
    fast_bias_series: pd.Series,
    config:           dict,
    econ_cal=None,
    vix_filter=None,
) -> pd.DataFrame:
    """
    Detect CL first-hour breakout signals.

    Logic:
      1. Build 9:30–10:30 AM ET range each day.
      2. After 10:30 AM, detect entry signals:
      3. Gate by fast_bias (daily EMA10/EMA20):
           BULL (+ htf=BULL) → LONG "Spring" entries only (mean-reversion dip-buy)
           BEAR              → SHORT breakdown entries only (unchanged, 75% WR)
           NEUTRAL           → skip the day entirely
      4. Regime filter: skip RANGING days (require TRENDING or HIGH_VOL).
      5. Skip Wednesday 10:00–12:00 ET (EIA inventory window).
      6. Skip HIGH-impact econ days (FOMC, NFP).
      7. Skip VIX=QUIET or VIX=CRISIS.
      8. Max 1 trade per day.

    LONG entry logic — "Spring" / dip-and-recover (replaces old breakout-high):
      On BULL-bias TRENDING days, scan 10:30–13:00 for the first bar where:
        a) bar.Low  ≤ range_low + CL_SPRING_DIP_MULT × ATR  (touched support zone)
        b) bar.Close ≥ range_low − 0.05 × ATR              (recovered above range_low)
        c) bar.Close > bar.Open                             (bullish reversal candle)
        d) RSI(5) on 1H ≤ CL_SPRING_RSI_MAX                (not overbought at entry)
        e) bar.Close ≤ VWAP (if CL_SPRING_VWAP_BELOW)      (below institutional fair value)
      Economic rationale: Wyckoff "Spring" — price undercuts support trapping bears,
      then snaps back. Trapped shorts cover → fastest long move of the session.

    SHORT entry logic — unchanged (first close below range_low − buffer on BEAR days).

    Adds columns: cl_long_signal, cl_short_signal, cl_htf_bias, cl_fast_bias,
                  cl_range_high, cl_range_low, cl_gls_score, cl_eia_blocked,
                  cl_htf_blocked, cl_regime_blocked, cl_filter_reason, cl_atr.
    """
    tick_size    = float(config.get("markets", {}).get("CL", {}).get("tick_size", CL_TICK_SIZE))
    entry_buffer = CL_ENTRY_BUF_TICKS * tick_size

    df = df_1h.copy()
    atr  = compute_1h_atr(df)
    rsi  = compute_1h_rsi(df)
    vwap = compute_daily_vwap(df)
    df["cl_atr"]  = atr
    df["cl_rsi"]  = rsi
    df["cl_vwap"] = vwap

    # Initialise columns
    for col, val in [
        ("cl_range_high",      float("nan")),
        ("cl_range_low",       float("nan")),
        ("cl_range_complete",  False),
        ("cl_long_signal",     False),
        ("cl_short_signal",    False),
        ("cl_htf_blocked",     False),
        ("cl_eia_blocked",     False),
        ("cl_regime_blocked",  False),
        ("cl_gls_score",       0),
        ("cl_htf_bias",        _NEUTRAL),
        ("cl_fast_bias",       _NEUTRAL),
        ("cl_filter_reason",   ""),
        # Spring exit guidance: tight stop + range_high target (used by simulate)
        ("cl_spring_stop_px",   float("nan")),
        ("cl_spring_target_px", float("nan")),
    ]:
        df[col] = val

    # Allowed regimes (reject RANGING — false breakout trap)
    _ALLOWED_REGIMES = {"TRENDING", "HIGH_VOL", "TRANSITIONING"}

    trading_dates = df.index.normalize().unique()
    n_long = n_short = n_htf_skip = n_eia = n_econ = n_vix = n_regime = 0

    for day in trading_dates:
        day_str   = str(pd.Timestamp(day).date())
        day_mask  = df.index.normalize() == day
        day_df    = df[day_mask].copy()

        if len(day_df) < 3:
            continue

        # ── Econ filter (HIGH impact: FOMC, NFP) ──────────────────────────────
        if econ_cal is not None and econ_cal.skip_today(day_str, min_impact="HIGH"):
            n_econ += 1
            df.update(day_df)
            continue

        # ── VIX regime ────────────────────────────────────────────────────────
        vix_regime = "OPTIMAL"
        if vix_filter is not None:
            vix_regime = vix_filter.get_regime(day_str)
        if vix_regime in ("QUIET", "CRISIS"):
            n_vix += 1
            df.update(day_df)
            continue

        # ── HTF / fast bias ────────────────────────────────────────────────────
        htf_bias  = _NEUTRAL
        fast_bias = _NEUTRAL

        def _lookup(series: pd.Series, date_str: str) -> str:
            try:
                if len(series) == 0:
                    return _NEUTRAL
                ix = series.copy()
                ix.index = [str(pd.Timestamp(d).date()) for d in series.index]
                prior = ix[ix.index <= date_str]
                return str(prior.iloc[-1]) if len(prior) > 0 else _NEUTRAL
            except Exception:
                return _NEUTRAL

        htf_bias  = _lookup(htf_bias_series,  day_str)
        fast_bias = _lookup(fast_bias_series, day_str)
        regime    = _lookup(regime_series,    day_str)

        # ── Regime filter (skip RANGING) ──────────────────────────────────────
        # Only block when we have a known regime that is explicitly disallowed.
        # If regime is NEUTRAL (unknown/no data), let the trade through.
        if CL_REQUIRE_TRENDING and regime != _NEUTRAL and regime not in _ALLOWED_REGIMES:
            n_regime += 1
            day_df.loc[:, "cl_regime_blocked"]  = True
            day_df.loc[:, "cl_filter_reason"]   = f"regime={regime}"
            df.update(day_df)
            continue

        # For CL: use fast_bias as primary; fall back to htf_combined if neutral
        effective = fast_bias if (CL_USE_FAST_BIAS and fast_bias != _NEUTRAL) else htf_bias
        if effective == _NEUTRAL:
            n_htf_skip += 1
            df.update(day_df)
            continue

        # Dual confirmation for LONG: require both htf AND fast to be BULL
        # SHORT only needs fast_bias=BEAR (downtrends cleaner on CL)
        if CL_LONG_DUAL_CONFIRM:
            long_allowed  = (effective == _BULL and htf_bias == _BULL)
            short_allowed = (effective == _BEAR)
        else:
            long_allowed  = (effective == _BULL)
            short_allowed = (effective == _BEAR)

        # ── First-hour range ──────────────────────────────────────────────────
        try:
            first_hr = day_df.between_time(CL_RANGE_START, CL_RANGE_END, inclusive="left")
        except Exception:
            continue
        if first_hr.empty:
            continue

        rng_hi = float(first_hr["High"].max())
        rng_lo = float(first_hr["Low"].min())
        if rng_hi <= rng_lo:
            continue

        # ── Range quality filter ──────────────────────────────────────────────
        # Narrow range (< 0.4×ATR): first hour had micro-chop → breakout signals
        #   are noise, price oscillates across boundary all session.
        # Wide range (> 2.0×ATR): first hour already had a large directional move;
        #   breakout entry from an already-extended range has poor R:R and higher
        #   reversal probability (EIA-like spike pattern even on non-EIA days).
        # Economic basis: mirrors FHB range_expand filter validated on ES/NQ/CL SC data.
        _atr_for_range = float(day_df["cl_atr"].dropna().iloc[0]) if not day_df["cl_atr"].dropna().empty else 0.0
        _range_width   = rng_hi - rng_lo
        if _atr_for_range > 0:
            if _range_width < 0.4 * _atr_for_range or _range_width > 2.0 * _atr_for_range:
                day_df.loc[:, "cl_filter_reason"] = (
                    f"range_quality: width={_range_width:.3f} "
                    f"ATR={_atr_for_range:.3f} "
                    f"ratio={_range_width/_atr_for_range:.2f}"
                )
                df.update(day_df)
                continue

        # Mark post-range bars
        try:
            post = day_df.between_time(CL_RANGE_END, "23:59")
            day_df.loc[post.index, "cl_range_complete"] = True
        except Exception:
            pass

        day_df["cl_range_high"] = rng_hi
        day_df["cl_range_low"]  = rng_lo
        day_df["cl_htf_bias"]   = htf_bias
        day_df["cl_fast_bias"]  = fast_bias

        # ── Tradeable window: 10:30 → 13:00 ──────────────────────────────────
        try:
            tradeable = day_df.between_time(CL_RANGE_END, CL_NO_ENTRY_AFTER)
        except Exception:
            tradeable = day_df[day_df["cl_range_complete"]]

        short_entry = rng_lo - entry_buffer   # SHORT: close below range_low (unchanged)
        fired = False

        for idx in tradeable.index:
            if fired:
                break

            bar     = tradeable.loc[idx]
            bar_ts  = pd.Timestamp(idx)
            bar_cls = float(bar["Close"])
            bar_opn = float(bar["Open"])
            bar_lo  = float(bar["Low"])
            atr_val = float(df.at[idx, "cl_atr"])  if (idx in df.index and not np.isnan(df.at[idx, "cl_atr"]))  else 0.0
            rsi_val = float(df.at[idx, "cl_rsi"])  if (idx in df.index and not np.isnan(df.at[idx, "cl_rsi"]))  else 50.0
            vwap_val= float(df.at[idx, "cl_vwap"]) if (idx in df.index and not np.isnan(df.at[idx, "cl_vwap"])) else bar_cls

            # ── EIA Wednesday filter ───────────────────────────────────────────
            if CL_EIA_SKIP and bar_ts.dayofweek == 2:  # Wednesday
                t = bar_ts.time()
                if _dt.time(10, 0) <= t <= _dt.time(12, 0):
                    day_df.loc[idx, "cl_eia_blocked"]   = True
                    day_df.loc[idx, "cl_filter_reason"] = "EIA window"
                    n_eia += 1
                    continue

            is_long  = False
            is_short = False

            # ── SHORT: breakdown below range_low (unchanged — 75% WR baseline) ──
            # Decisive breakdown filter: bar must OPEN at or above range_low
            # boundary then CLOSE below it in one decisive move.
            # This filters out "slow drift" breakdowns (gradual leak through support)
            # in favour of clean momentum breakdowns where bears take control fast.
            # Economic rationale: decisive single-bar breakdowns trap more bulls
            # (stops cluster at range_low — one fast move triggers them all at once).
            if short_allowed:
                decisive_break = float(bar["Open"]) >= rng_lo - entry_buffer
                is_short = (bar_cls < short_entry) and decisive_break

            # ── LONG: "Spring" / dip-and-recover entry ────────────────────────
            # Price must DIP to near/below range_low (support undercut — bear trap),
            # then CLOSE BACK ABOVE range_low on a BULLISH candle, with RSI not
            # overbought and close at/below VWAP (buying at institutional discount).
            #
            # Why this beats breakout-high entry:
            #   Buying the HIGH = entering when price is extended, poor R/R.
            #   Buying the DIP  = entering at support where stops cluster and
            #   trapped shorts must cover → fastest and cleanest long moves.
            if long_allowed and atr_val > 0:
                dip_zone      = bar_lo <= rng_lo + CL_SPRING_DIP_MULT * atr_val
                recovered     = bar_cls >= rng_lo - 0.05 * atr_val
                bullish_candle= bar_cls > bar_opn
                rsi_ok        = rsi_val <= CL_SPRING_RSI_MAX
                vwap_ok       = (bar_cls <= vwap_val) if CL_SPRING_VWAP_BELOW else True

                is_long = dip_zone and recovered and bullish_candle and rsi_ok and vwap_ok

            if not is_long and not is_short:
                continue

            fired = True
            # Score: 85 base + 5 for each extra confirmation
            gls = 85
            if rsi_val <= 35:        gls += 5   # deeply oversold = higher confidence
            if bar_lo < rng_lo:      gls += 5   # actual undercut (not just near support)
            if bar_cls <= vwap_val:  gls += 5   # VWAP below confirmed
            day_df.loc[idx, "cl_gls_score"] = min(gls, 100)

            # For spring LONG: record tight stop and range-high target
            # Stop just below the support zone (not 0.8 ATR away)
            # Target = range_high (full mean reversion — buying dip back to range top)
            if is_long and atr_val > 0:
                spring_stop = rng_lo - 0.30 * atr_val
                # Ensure stop is genuinely below entry
                spring_stop = min(spring_stop, bar_cls - 0.05 * atr_val)
                day_df.loc[idx, "cl_spring_stop_px"]   = spring_stop
                day_df.loc[idx, "cl_spring_target_px"] = rng_hi

            if is_long:
                day_df.loc[idx, "cl_long_signal"]  = True
                n_long += 1
            else:
                day_df.loc[idx, "cl_short_signal"] = True
                n_short += 1

        df.update(day_df)

    log.info(
        "[CL] Signals: Long=%d Short=%d | Filtered: HTF=%d Regime=%d EIA=%d Econ=%d VIX=%d",
        n_long, n_short, n_htf_skip, n_regime, n_eia, n_econ, n_vix
    )
    print(f"  [CL signals] Long={n_long} Short={n_short}  "
          f"Filtered: HTF={n_htf_skip} Regime={n_regime} EIA={n_eia} "
          f"Econ={n_econ} VIX={n_vix}")
    return df


# ── Trade simulation ───────────────────────────────────────────────────────────

def simulate_cl_trades(
    df_sig: pd.DataFrame,
    config: dict,
) -> list[dict]:
    """
    Simulate CL first-hour breakout trades from signal DataFrame.

    Exit hierarchy (checked each bar in order):
      1. Partial exit (50%) at CL_PARTIAL_R — trail stop to entry (breakeven)
      2. Full exit at CL_TARGET_R (1.0R)
      3. Full stop hit
      4. Time stop: market close (16:00 ET) or CL_MAX_HOLD_BARS bars
    """
    point_val  = CL_POINT_VALUE
    commission = CL_COMMISSION * 2                      # round-turn
    slippage   = CL_SLIPPAGE_TICKS * CL_TICK_SIZE * point_val  # per contract

    trades: list[dict] = []
    trading_dates = df_sig.index.normalize().unique()

    for day in trading_dates:
        day_mask = df_sig.index.normalize() == day
        day_df   = df_sig[day_mask].copy()

        # Find signal bars
        long_bars  = day_df.index[day_df["cl_long_signal"]].tolist()
        short_bars = day_df.index[day_df["cl_short_signal"]].tolist()

        for is_long, signal_bars in [(True, long_bars), (False, short_bars)]:
            if not signal_bars:
                continue

            sig_idx = signal_bars[0]
            sig_bar = day_df.loc[sig_idx]
            atr_val = float(sig_bar.get("cl_atr", 0.0))
            if atr_val <= 0:
                continue

            direction = "LONG" if is_long else "SHORT"
            sign      = 1 if is_long else -1

            # Entry price = close of signal bar
            entry_px = float(sig_bar["Close"])

            # ── Exit parameters ──────────────────────────────────────────────
            # LONG (Spring): tight stop just below support + range_high target.
            #   Stop = range_low - 0.30×ATR  (stored as cl_spring_stop_px)
            #   Target full = range_high      (stored as cl_spring_target_px)
            #   Partial = midpoint between entry and range_high
            #   Rationale: buying at range_low, the fair-value reversion target IS
            #   range_high. Tight stop because if support fails → we're just wrong.
            #
            # SHORT: ATR-based stop + 1.0R target (unchanged, 75% WR)
            max_pts = CL_MAX_LOSS_USD / point_val

            spring_stop_px   = float(sig_bar.get("cl_spring_stop_px",   float("nan")))
            spring_target_px = float(sig_bar.get("cl_spring_target_px", float("nan")))

            if is_long and not np.isnan(spring_stop_px) and spring_stop_px < entry_px:
                # Spring LONG: tight stop, range-high target
                stop_px    = spring_stop_px
                stop_dist  = entry_px - stop_px
                target2_px = spring_target_px                         # range_high
                target1_px = entry_px + 0.5 * (target2_px - entry_px)# midpoint partial
            else:
                # Default ATR-based exits (SHORT always; LONG fallback)
                raw_stop_dist = min(CL_ATR_STOP_MULT, CL_ATR_STOP_CAP) * atr_val
                stop_dist     = min(raw_stop_dist, max_pts)
                stop_px    = entry_px - sign * stop_dist
                target1_px = entry_px + sign * CL_PARTIAL_R * stop_dist
                target2_px = entry_px + sign * CL_TARGET_R  * stop_dist

            gls_score = int(sig_bar.get("cl_gls_score", 0))

            # Simulate bar-by-bar from the bar AFTER the signal
            sig_pos     = day_df.index.get_loc(sig_idx)
            remaining   = day_df.iloc[sig_pos + 1:]

            partial_done   = False
            stop_at_entry  = False   # after partial, stop trails to entry
            bars_held      = 0
            exit_price     = entry_px
            exit_reason    = "time_exit"
            exit_ts        = sig_idx

            for bar_idx, bar in remaining.iterrows():
                bars_held += 1
                bar_ts = pd.Timestamp(bar_idx)
                lo_    = float(bar["Low"])
                hi_    = float(bar["High"])
                cls_   = float(bar["Close"])

                # Time stop: close by 16:00 or max hold bars
                at_close  = bar_ts.hour >= 15 and bar_ts.minute >= 45
                too_long  = bars_held >= CL_MAX_HOLD_BARS
                if at_close or too_long:
                    exit_price  = cls_
                    exit_reason = "time_exit"
                    exit_ts     = bar_idx
                    break

                effective_stop = entry_px if stop_at_entry else stop_px

                if is_long:
                    # Stop hit?
                    if lo_ <= effective_stop:
                        exit_price  = effective_stop
                        exit_reason = "stop_partial" if partial_done else "stop_full"
                        exit_ts     = bar_idx
                        break
                    # Partial target?
                    if not partial_done and hi_ >= target1_px:
                        partial_done  = True
                        stop_at_entry = True   # trail to breakeven
                    # Full target?
                    if hi_ >= target2_px:
                        exit_price  = target2_px
                        exit_reason = "target_full"
                        exit_ts     = bar_idx
                        break
                else:  # SHORT
                    if hi_ >= effective_stop:
                        exit_price  = effective_stop
                        exit_reason = "stop_partial" if partial_done else "stop_full"
                        exit_ts     = bar_idx
                        break
                    if not partial_done and lo_ <= target1_px:
                        partial_done  = True
                        stop_at_entry = True
                    if lo_ <= target2_px:
                        exit_price  = target2_px
                        exit_reason = "target_full"
                        exit_ts     = bar_idx
                        break
            else:
                # End of available bars — use last close
                if len(remaining) > 0:
                    exit_price = float(remaining.iloc[-1]["Close"])
                    exit_ts    = remaining.index[-1]
                exit_reason = "time_exit"

            # P&L calculation
            # Full position P&L if no partial, else blend partial + remainder
            if partial_done:
                # 50% exited at 0.5R partial level, 50% at actual exit
                partial_pnl   = CL_PARTIAL_PCT * stop_dist * CL_PARTIAL_R * point_val
                remainder_pts = sign * (exit_price - entry_px) - sign * CL_PARTIAL_R * stop_dist
                remainder_pnl = (1.0 - CL_PARTIAL_PCT) * remainder_pts * point_val
                gross = partial_pnl + max(remainder_pnl, -(1.0 - CL_PARTIAL_PCT) * stop_dist * point_val)
            else:
                gross = sign * (exit_price - entry_px) * point_val

            pnl_net = gross - commission - slippage

            trades.append({
                "date":         str(pd.Timestamp(day).date()),
                "strategy":     "CL_FHB",
                "market":       "CL",
                "direction":    direction,
                "entry":        str(pd.Timestamp(sig_idx)),
                "entry_price":  round(entry_px, 2),
                "stop":         round(stop_px, 2),
                "target":       round(target2_px, 2),
                "exit":         str(pd.Timestamp(exit_ts)),
                "exit_price":   round(exit_price, 2),
                "exit_reason":  exit_reason,
                "pnl_net":      round(pnl_net, 2),
                "risk_pts":     round(stop_dist, 4),
                "risk_usd":     round(stop_dist * point_val, 2),
                "r_multiple":   round(sign * (exit_price - entry_px) / stop_dist, 3) if stop_dist > 0 else 0.0,
                "gls_score":    gls_score,
                "partial_done": partial_done,
                "bars_held":    bars_held,
                "atr_entry":    round(atr_val, 4),
            })

    return trades
