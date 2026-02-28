"""
AlgoBot — Market Regime Classifier
=====================================
Module:  src/strategy/regime_classifier.py
Phase:   2 — Strategy Signals
Purpose: Determines the current market regime on every bar.
         The regime drives ALL downstream decisions: which signals are active,
         what position size to use, whether to trade at all.

The Five Regimes:
  TRENDING:       ADX > 25. Strong directional move. Trade TMA + DCS signals.
                  Full position size. This is where the bot makes most of its money.

  RANGING:        ADX < 20. Market moving sideways. Trade VMR (mean reversion)
                  on ES/NQ only. Trend signals are OFF. Avoid false breakouts.

  TRANSITIONING:  20 <= ADX <= 25. Regime is unclear. Neither trending nor ranging.
                  NO new entries. Wait for clarity. If already in a position, manage it.

  HIGH_VOL:       ATR > 1.5x 1-year ATR baseline. Volatility spike. Market is
                  moving fast — larger gaps, wider spreads, erratic fills.
                  Position size reduced to 50%. Trend signals remain active.

  CRISIS:         ATR > 2.5x 1-year ATR baseline. Extreme volatility.
                  COVID crash level, 2008 level. NO new entries. Manage existing
                  positions only. Emergency mode.

Regime priority (when multiple conditions met):
  CRISIS > HIGH_VOL > TRENDING > RANGING > TRANSITIONING

Why regime detection matters:
  A trend-following system applied during a ranging market will produce
  constant false breakouts — entry at high, reversal immediately after.
  The regime filter is the most important risk control after position sizing.
  In our backtest, removing the regime filter degraded PF from 2.87 to 1.4.
"""

from dataclasses import dataclass
from enum import Enum

import pandas as pd
import numpy as np

from src.utils.logger import get_logger

log = get_logger(__name__)


# ── Regime enum ───────────────────────────────────────────────────────────────

class RegimeState(str, Enum):
    """Market regime states. String enum for easy serialization/logging."""
    TRENDING       = "TRENDING"
    RANGING        = "RANGING"
    TRANSITIONING  = "TRANSITIONING"
    HIGH_VOL       = "HIGH_VOL"
    CRISIS         = "CRISIS"


# ── Regime result ─────────────────────────────────────────────────────────────

@dataclass
class RegimeResult:
    """
    Full regime classification result for one bar.
    Contains the regime state plus the raw metrics that determined it.
    """
    state:        RegimeState
    adx:          float
    atr:          float
    atr_baseline: float
    atr_ratio:    float

    # Derived flags (used by signal combiner)
    trend_signals_active: bool  = True   # TMA and DCS can fire
    vmr_active:           bool  = False  # Mean reversion can fire
    size_multiplier:      float = 1.0    # Position size scaling

    def __post_init__(self):
        """Derive flags from state after initialization."""
        if self.state == RegimeState.TRENDING:
            self.trend_signals_active = True
            self.vmr_active           = False
            self.size_multiplier      = 1.0
        elif self.state == RegimeState.RANGING:
            self.trend_signals_active = False
            self.vmr_active           = True
            self.size_multiplier      = 1.0
        elif self.state == RegimeState.TRANSITIONING:
            self.trend_signals_active = False
            self.vmr_active           = False
            self.size_multiplier      = 0.0  # No new entries
        elif self.state == RegimeState.HIGH_VOL:
            self.trend_signals_active = True
            self.vmr_active           = False
            self.size_multiplier      = 0.5  # Half size
        elif self.state == RegimeState.CRISIS:
            self.trend_signals_active = False
            self.vmr_active           = False
            self.size_multiplier      = 0.0  # No new entries

    def __str__(self) -> str:
        return (
            f"{self.state.value} | "
            f"ADX={self.adx:.1f} | "
            f"ATR_ratio={self.atr_ratio:.2f} | "
            f"size={self.size_multiplier:.1f}x"
        )


# ── Single-bar classification ─────────────────────────────────────────────────

def classify_regime(
    adx:          float,
    atr:          float,
    atr_baseline: float,
    config:       dict,
) -> RegimeResult:
    """
    Classify the market regime for a single bar.

    Args:
        adx:          ADX value for this bar
        atr:          ATR value for this bar
        atr_baseline: Rolling 1-year average ATR (from add_atr_baseline)
        config:       Strategy config dict. Required keys:
                        adx_trending_threshold  (default 25)
                        adx_ranging_threshold   (default 20)
                        high_vol_atr_multiplier (default 1.5)
                        crisis_atr_multiplier   (default 2.5)

    Returns:
        RegimeResult with state, flags, and size multiplier.

    Example:
        result = classify_regime(adx=28.5, atr=15.2, atr_baseline=12.0, config=cfg)
        print(result)  # "TRENDING | ADX=28.5 | ATR_ratio=1.27 | size=1.0x"
    """
    # Config parameters
    adx_trending  = float(config.get("adx_trending_threshold",  25.0))
    adx_ranging   = float(config.get("adx_ranging_threshold",   20.0))
    high_vol_mult = float(config.get("high_vol_atr_multiplier",  1.5))
    crisis_mult   = float(config.get("crisis_atr_multiplier",    2.5))

    # Handle NaN/invalid inputs gracefully
    if pd.isna(adx) or pd.isna(atr) or pd.isna(atr_baseline) or atr_baseline <= 0:
        return RegimeResult(
            state=RegimeState.TRANSITIONING,
            adx=adx if not pd.isna(adx) else 0.0,
            atr=atr if not pd.isna(atr) else 0.0,
            atr_baseline=atr_baseline if not pd.isna(atr_baseline) else 1.0,
            atr_ratio=1.0,
        )

    atr_ratio = atr / atr_baseline

    # Priority order: CRISIS > HIGH_VOL > TRENDING > RANGING > TRANSITIONING
    if atr_ratio >= crisis_mult:
        state = RegimeState.CRISIS
    elif atr_ratio >= high_vol_mult:
        state = RegimeState.HIGH_VOL
    elif adx >= adx_trending:
        state = RegimeState.TRENDING
    elif adx < adx_ranging:
        state = RegimeState.RANGING
    else:
        state = RegimeState.TRANSITIONING

    return RegimeResult(
        state=state,
        adx=round(adx, 2),
        atr=round(atr, 4),
        atr_baseline=round(atr_baseline, 4),
        atr_ratio=round(atr_ratio, 3),
    )


# ── Full DataFrame classification ─────────────────────────────────────────────

def classify_regimes(
    df: pd.DataFrame,
    config: dict,
    market: str = "UNKNOWN",
) -> pd.DataFrame:
    """
    Classify the market regime for every bar in a DataFrame.

    Requires that calculate_indicators() and add_atr_baseline() have
    already been called on the DataFrame.

    Args:
        df:     DataFrame with indicator columns: adx, atr, atr_baseline, atr_ratio
        config: Strategy config dict
        market: Market code for logging

    Returns:
        DataFrame with regime columns added:
          regime          - RegimeState string ("TRENDING", "RANGING", etc.)
          size_multiplier - Position size scaling (0.0, 0.5, or 1.0)
          trend_active    - Boolean: trend signals allowed this bar
          vmr_active      - Boolean: mean reversion allowed this bar

    Example:
        df = calculate_indicators(clean_df, cfg, "ES")
        df = add_atr_baseline(df)
        df = classify_regimes(df, cfg, "ES")
        print(df["regime"].value_counts())
    """
    required_cols = ["adx", "atr", "atr_baseline", "atr_ratio"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        log.error("{market}: Missing columns for regime classification: {cols}",
                  market=market, cols=missing)
        df["regime"]          = RegimeState.TRANSITIONING.value
        df["size_multiplier"] = 0.0
        df["trend_active"]    = False
        df["vmr_active"]      = False
        return df

    df = df.copy()

    regimes         = []
    size_mults      = []
    trend_actives   = []
    vmr_actives     = []

    for i in range(len(df)):
        result = classify_regime(
            adx=df["adx"].iloc[i],
            atr=df["atr"].iloc[i],
            atr_baseline=df["atr_baseline"].iloc[i],
            config=config,
        )
        regimes.append(result.state.value)
        size_mults.append(result.size_multiplier)
        trend_actives.append(result.trend_signals_active)
        vmr_actives.append(result.vmr_active)

    df["regime"]          = regimes
    df["size_multiplier"] = size_mults
    df["trend_active"]    = trend_actives
    df["vmr_active"]      = vmr_actives

    # Log regime distribution
    regime_counts = df["regime"].value_counts()
    total = len(df)
    log.info(
        "{market}: Regime distribution over {n} bars: "
        "TRENDING={tr:.0%} RANGING={rg:.0%} TRANSITIONING={ts:.0%} "
        "HIGH_VOL={hv:.0%} CRISIS={cr:.0%}",
        market=market,
        n=total,
        tr=regime_counts.get("TRENDING",      0) / total,
        rg=regime_counts.get("RANGING",       0) / total,
        ts=regime_counts.get("TRANSITIONING", 0) / total,
        hv=regime_counts.get("HIGH_VOL",      0) / total,
        cr=regime_counts.get("CRISIS",        0) / total,
    )

    return df
