"""
AlgoBot — Signal Combiner, Agreement Filter & HTF Bias Gate
=============================================================
Module:  src/strategy/signal_combiner.py
Phase:   2 (original) / 5 (updated with MTF bias gate)
Purpose: Combines TMA, DCS, and VMR signals using the Signal Agreement Filter.
         Phase 5 adds the Higher-Timeframe Bias Gate as an outer filter:
         only signals aligned with the weekly/monthly trend are passed through.

PHASE 5 CHANGES:
  1. HTF Bias Gate added: all signals must align with htf_combined_bias.
       AGREE_LONG  blocked when htf_combined_bias == BEAR
       AGREE_SHORT blocked when htf_combined_bias == BULL
       VMR_LONG    blocked when htf_weekly_bias   == BEAR (weekly is enough)
  2. VMR SHORT disabled: controlled by config.strategy.vmr.vmr_short_enabled.
       Set to false in Phase 5 because IS backtest showed VMR SHORT PF=0.76.
       VMR LONG (oversold bounces) is retained — it has positive expectancy.
  3. combine_signals() now accepts optional config parameter.
       If config=None, HTF bias gate is skipped (backward-compatible).

The Signal Agreement Filter (the core edge):
  A trend trade is ONLY executed when BOTH TMA AND DCS agree.
  VMR trades execute independently (a separate bet on range reversion).

  Individual systems (estimated):
    TMA alone:  Win rate ~45%, avg win/loss = 3.0 -> PF ~ 2.45
    DCS alone:  Win rate ~43%, avg win/loss = 3.5 -> PF ~ 2.64
    VMR alone:  Win rate ~55%, avg win/loss = 1.5 -> PF ~ 1.83

  Agreement filter (both must agree):
    Win rate ~58%, avg win/loss = 4.2 -> PF ~ 5.80 in isolation
    Blended PF target with VMR: 2.5-3.0

  HTF Bias Gate (Phase 5 addition):
    Removes counter-trend entries and VMR SHORT into bull markets.
    Estimated additional PF improvement: +0.3 to +0.6 over Phase 4.

Trade types:
  AGREE_LONG:  TMA=+1 AND DCS=+1 AND trend_active AND htf != BEAR
  AGREE_SHORT: TMA=-1 AND DCS=-1 AND trend_active AND htf != BULL
  VMR_LONG:    VMR=+1 AND vmr_active AND market ES/NQ AND weekly != BEAR
  VMR_SHORT:   DISABLED (vmr_short_enabled=false in config)
  NO_TRADE:    None of the above
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import pandas as pd
import numpy as np

from src.utils.logger import get_logger
from src.strategy.vmr_signal import VMR_MARKETS

# Phase 5D -- lazy imports so existing tests that don't use these never fail
try:
    from src.utils.econ_calendar import EconCalendar as _EconCalendar
    from src.utils.vix_filter import VIXFilter as _VIXFilter
    _FILTERS_AVAILABLE = True
except ImportError:
    _FILTERS_AVAILABLE = False

log = get_logger(__name__)

# HTF bias constant values (must match htf_bias.py)
_BULL    = "BULL"
_BEAR    = "BEAR"
_NEUTRAL = "NEUTRAL"


# ── Signal direction enum ─────────────────────────────────────────────────────

class SignalDirection(str, Enum):
    """Final combined signal direction for position management."""
    AGREE_LONG   = "AGREE_LONG"    # TMA+DCS both long — trend breakout trade
    AGREE_SHORT  = "AGREE_SHORT"   # TMA+DCS both short — trend breakout trade
    PB_LONG      = "PB_LONG"       # EMA pullback long — higher win-rate trend entry
    PB_SHORT     = "PB_SHORT"      # EMA pullback short — higher win-rate trend entry
    VMR_LONG     = "VMR_LONG"      # Mean reversion long (ES/NQ only)
    VMR_SHORT    = "VMR_SHORT"     # Mean reversion short — DISABLED in Phase 5
    NO_TRADE     = "NO_TRADE"      # No valid signal this bar


# ── Combined signal result ────────────────────────────────────────────────────

@dataclass
class CombinedSignal:
    """
    Result of signal combination for a single bar.
    Passed to position_sizer and then to the backtesting engine.
    """
    direction:         SignalDirection
    tma_signal:        int    = 0      # Raw TMA: +1, 0, -1
    dcs_signal:        int    = 0      # Raw DCS: +1, 0, -1
    vmr_signal:        int    = 0      # Raw VMR: +1, 0, -1
    regime:            str    = ""     # Regime state string
    size_multiplier:   float  = 0.0    # From regime (1.0, 0.5, or 0.0)
    is_new_entry:      bool   = False  # True only on first bar of new signal
    is_trend:          bool   = False  # True for AGREE_LONG/AGREE_SHORT
    is_mean_reversion: bool   = False  # True for VMR_LONG/VMR_SHORT
    htf_blocked:       bool   = False  # True if HTF bias gate blocked this bar

    def __post_init__(self):
        self.is_trend          = self.direction in (
            SignalDirection.AGREE_LONG, SignalDirection.AGREE_SHORT,
            SignalDirection.PB_LONG,   SignalDirection.PB_SHORT,
        )
        self.is_mean_reversion = self.direction in (SignalDirection.VMR_LONG,
                                                    SignalDirection.VMR_SHORT)

    def __str__(self) -> str:
        htf_str = " [HTF-BLOCKED]" if self.htf_blocked else ""
        return (
            f"{self.direction.value}{htf_str} | "
            f"TMA={self.tma_signal:+d} DCS={self.dcs_signal:+d} VMR={self.vmr_signal:+d} | "
            f"Regime={self.regime} | size={self.size_multiplier:.1f}x | "
            f"{'NEW' if self.is_new_entry else 'cont'}"
        )


# ── Full DataFrame combination ────────────────────────────────────────────────

def combine_signals(
    df: pd.DataFrame,
    market: str = "UNKNOWN",
    config: Optional[dict] = None,
    vix_filter=None,
    econ_cal=None,
) -> pd.DataFrame:
    """
    Apply the Signal Agreement Filter + HTF Bias Gate + Phase 5D filters.

    Call order (all must run before this):
      calculate_indicators() -> add_atr_baseline() -> classify_regimes()
      -> tma_signal() -> dcs_signal() -> vmr_signal()
      -> add_htf_bias()   [optional, Phase 5 -- skipped if columns absent]

    Args:
        df:         DataFrame with all signal and regime columns.
        market:     Market code for logging ("ES", "NQ", etc.).
        config:     Full config dict. If None, HTF gate is skipped (backward
                    compatible with Phase 2-4 tests that don't pass config).
        vix_filter: Optional VIXFilter instance (Phase 5D). If provided:
                    - CRISIS regime (VIX>35): all entries blocked
                    - ELEVATED regime (VIX 28-35): size_mult *= 0.5
                    - QUIET regime (VIX<13): all entries blocked
                    Backward compatible: None = no VIX gating.
        econ_cal:   Optional EconCalendar instance (Phase 5D). If provided:
                    - HIGH impact days (NFP, FOMC): all new entries blocked
                    - MEDIUM impact days: size_mult *= 0.5
                    Backward compatible: None = no econ gating.

    Returns:
        DataFrame with combined signal columns added:
          combined_signal      - SignalDirection string value
          combined_new_entry   - bool: new entry signal this bar
          combined_is_trend    - bool: trend trade (AGREE_LONG/SHORT)
          combined_is_vmr      - bool: mean reversion trade
          combined_size_mult   - float: position size multiplier
          combined_htf_blocked - bool: True when HTF gate suppressed a signal

    Example:
        df = add_htf_bias(df, config, "ES")
        df = combine_signals(df, "ES", config)
        entries = df[df["combined_new_entry"]]
        print(entries["combined_signal"].value_counts())
    """
    required_cols = [
        "tma_signal", "dcs_signal", "vmr_signal",
        "trend_active", "vmr_active", "regime", "size_multiplier",
    ]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        log.error("{market}: combine_signals missing columns: {cols}",
                  market=market, cols=missing)
        df["combined_signal"]      = SignalDirection.NO_TRADE.value
        df["combined_new_entry"]   = False
        df["combined_is_trend"]    = False
        df["combined_is_vmr"]      = False
        df["combined_size_mult"]   = 0.0
        df["combined_htf_blocked"] = False
        return df

    df = df.copy()
    vmr_allowed = market.upper() in VMR_MARKETS

    # ── Read config flags ──────────────────────────────────────────────────────
    vmr_short_enabled = True   # default (backward compat)
    use_htf_gate      = False  # only enabled when config provided AND columns present

    if config is not None:
        vmr_cfg = config.get("strategy", {}).get("vmr", {})
        vmr_short_enabled = bool(vmr_cfg.get("vmr_short_enabled", True))

        # Enable HTF gate only if bias columns are present in the DataFrame
        htf_cols_present = (
            "htf_combined_bias" in df.columns and
            "htf_weekly_bias"   in df.columns
        )
        use_htf_gate = htf_cols_present

        if not htf_cols_present and config.get("htf_bias") is not None:
            log.warning(
                "{market}: HTF bias config present but htf_bias columns not found "
                "in DataFrame. Run add_htf_bias() before combine_signals(). "
                "HTF gate will be skipped.",
                market=market,
            )

    # ── Bar-by-bar combination ─────────────────────────────────────────────────
    combined_signals  = []
    htf_blocked_flags = []
    vix_blocked_count  = 0
    econ_blocked_count = 0

    # Pre-check: does df have a datetime index for VIX/econ lookups?
    _has_date_index = hasattr(df.index, "date") or (
        len(df) > 0 and hasattr(df.index[0], "date")
    )

    for i in range(len(df)):
        row       = df.iloc[i]
        tma_raw   = int(row["tma_signal"])
        dcs_raw   = int(row["dcs_signal"])
        vmr_raw   = int(row["vmr_signal"])
        trend_ok  = bool(row["trend_active"])
        vmr_ok    = bool(row["vmr_active"]) and vmr_allowed
        size_mult = float(row["size_multiplier"])

        # Read HTF bias for this bar (default NEUTRAL if gate disabled)
        if use_htf_gate:
            htf_combined = str(row.get("htf_combined_bias", _NEUTRAL))
            htf_weekly   = str(row.get("htf_weekly_bias",   _NEUTRAL))
        else:
            htf_combined = _NEUTRAL
            htf_weekly   = _NEUTRAL

        htf_blocked  = False
        direction    = SignalDirection.NO_TRADE

        if size_mult <= 0:
            # Crisis regime -- no entries regardless of signals
            combined_signals.append(direction.value)
            htf_blocked_flags.append(False)
            continue

        # ── Phase 5D: VIX regime gate ─────────────────────────────────────────
        if vix_filter is not None and _has_date_index:
            try:
                bar_date = df.index[i]
                vix_regime = vix_filter.get_regime(bar_date)
                if vix_regime in ("CRISIS", "QUIET"):
                    # VIX too extreme -- skip all new entries
                    combined_signals.append(SignalDirection.NO_TRADE.value)
                    htf_blocked_flags.append(False)
                    vix_blocked_count += 1
                    continue
                elif vix_regime == "ELEVATED":
                    # Half size on elevated VIX (multiplier applied to size_mult)
                    size_mult = size_mult * 0.5
            except Exception:
                pass   # VIX lookup failure -- don't block, continue

        # ── Phase 5D: Economic calendar gate ─────────────────────────────────
        if econ_cal is not None and _has_date_index:
            try:
                bar_date   = df.index[i]
                econ_level = econ_cal.get_impact_level(bar_date)
                if econ_level == "HIGH":
                    # High-impact news today -- skip all new entries
                    combined_signals.append(SignalDirection.NO_TRADE.value)
                    htf_blocked_flags.append(False)
                    econ_blocked_count += 1
                    continue
                elif econ_level == "MEDIUM":
                    size_mult = size_mult * 0.5
            except Exception:
                pass   # Calendar lookup failure -- don't block, continue

        # ── Trend: TMA + DCS agreement filter ────────────────────────────────
        if trend_ok:
            if tma_raw == 1 and dcs_raw == 1:
                # Potential AGREE_LONG -- check HTF gate
                if use_htf_gate and htf_combined == _BEAR:
                    htf_blocked = True
                    direction   = SignalDirection.NO_TRADE
                else:
                    direction = SignalDirection.AGREE_LONG

            elif tma_raw == -1 and dcs_raw == -1:
                # Potential AGREE_SHORT -- check HTF gate
                if use_htf_gate and htf_combined == _BULL:
                    htf_blocked = True
                    direction   = SignalDirection.NO_TRADE
                else:
                    direction = SignalDirection.AGREE_SHORT

        # ── Pullback entries: EMA retest within confirmed trend ───────────────
        # Fires when AGREE_LONG/SHORT is NOT active (don't double-enter same bar)
        # but a confirmed trend pullback setup exists.
        if direction == SignalDirection.NO_TRADE and trend_ok:
            pb_new_long  = bool(row.get("pb_new_long",  False))
            pb_new_short = bool(row.get("pb_new_short", False))

            if pb_new_long:
                if use_htf_gate and htf_combined == _BEAR:
                    htf_blocked = True
                else:
                    direction = SignalDirection.PB_LONG

            elif pb_new_short:
                if use_htf_gate and htf_combined == _BULL:
                    htf_blocked = True
                else:
                    direction = SignalDirection.PB_SHORT

        # ── VMR: Mean Reversion (ranging regime) ──────────────────────────────
        if direction == SignalDirection.NO_TRADE and vmr_ok:
            if vmr_raw == 1:
                # VMR LONG -- only block in explicit bear weekly trend
                if use_htf_gate and htf_weekly == _BEAR:
                    htf_blocked = True
                else:
                    direction = SignalDirection.VMR_LONG

            elif vmr_raw == -1:
                # VMR SHORT -- check if enabled
                if not vmr_short_enabled:
                    pass  # disabled globally -- silent skip
                elif use_htf_gate and htf_weekly == _BULL:
                    htf_blocked = True
                else:
                    direction = SignalDirection.VMR_SHORT

        combined_signals.append(direction.value)
        htf_blocked_flags.append(htf_blocked)

    df["combined_signal"]      = combined_signals
    df["combined_htf_blocked"] = htf_blocked_flags

    # ── New entry detection ────────────────────────────────────────────────────
    prev_signal = df["combined_signal"].shift(1).fillna(SignalDirection.NO_TRADE.value)
    df["combined_new_entry"] = (
        (df["combined_signal"] != SignalDirection.NO_TRADE.value) &
        (df["combined_signal"] != prev_signal)
    )

    # ── Derived boolean flags ──────────────────────────────────────────────────
    df["combined_is_trend"] = df["combined_signal"].isin([
        SignalDirection.AGREE_LONG.value,
        SignalDirection.AGREE_SHORT.value,
        SignalDirection.PB_LONG.value,
        SignalDirection.PB_SHORT.value,
    ])
    df["combined_is_vmr"]   = df["combined_signal"].isin([
        SignalDirection.VMR_LONG.value,
        SignalDirection.VMR_SHORT.value,
    ])
    df["combined_size_mult"] = df["size_multiplier"]

    # ── Logging ────────────────────────────────────────────────────────────────
    signal_counts = df["combined_signal"].value_counts()

    agree_long  = signal_counts.get(SignalDirection.AGREE_LONG.value,  0)
    agree_short = signal_counts.get(SignalDirection.AGREE_SHORT.value, 0)
    pb_long     = signal_counts.get(SignalDirection.PB_LONG.value,     0)
    pb_short    = signal_counts.get(SignalDirection.PB_SHORT.value,    0)
    vmr_long    = signal_counts.get(SignalDirection.VMR_LONG.value,    0)
    vmr_short   = signal_counts.get(SignalDirection.VMR_SHORT.value,   0)
    no_trade    = signal_counts.get(SignalDirection.NO_TRADE.value,    0)
    htf_blocks  = int(df["combined_htf_blocked"].sum())

    log.info(
        "{market}: Combined signals {n} bars | "
        "AGREE_LONG={al} AGREE_SHORT={as_} PB_LONG={pbl} PB_SHORT={pbs} "
        "VMR_LONG={vl} VMR_SHORT={vs} NO_TRADE={nt} | "
        "HTF_BLOCKED={hb} VIX_BLOCKED={vb} ECON_BLOCKED={eb} | new_entries={ne}",
        market=market,
        n=len(df),
        al=agree_long, as_=agree_short,
        pbl=pb_long, pbs=pb_short,
        vl=vmr_long, vs=vmr_short,
        nt=no_trade, hb=htf_blocks,
        vb=vix_blocked_count, eb=econ_blocked_count,
        ne=int(df["combined_new_entry"].sum()),
    )

    return df


# ── Exit signal logic ─────────────────────────────────────────────────────────

def get_exit_signal(
    df: pd.DataFrame,
    position_type: str,
    entry_bar: int,
) -> bool:
    """
    Determine whether to exit an open position at bar `entry_bar`.

    Exit rules depend on position type:
      Trend (AGREE_LONG/SHORT): Exit when DCS exit fires OR TMA flips
      VMR:                      Exit when RSI returns to neutral (40-60)

    Args:
        df:            Full indicator DataFrame
        position_type: "AGREE_LONG", "AGREE_SHORT", "VMR_LONG", "VMR_SHORT"
        entry_bar:     Integer index of the current bar being evaluated

    Returns:
        True if position should be closed, False to hold.
    """
    if entry_bar >= len(df):
        return True  # Safety: always exit at end of data

    row = df.iloc[entry_bar]

    if position_type in ("AGREE_LONG", "PB_LONG"):
        return bool(row.get("dcs_exit_long", False)) or (int(row.get("tma_signal", 0)) == -1)

    elif position_type in ("AGREE_SHORT", "PB_SHORT"):
        return bool(row.get("dcs_exit_short", False)) or (int(row.get("tma_signal", 0)) == 1)

    elif position_type == "VMR_LONG":
        return bool(row.get("vmr_exit_long", False))

    elif position_type == "VMR_SHORT":
        return bool(row.get("vmr_exit_short", False))

    return False
