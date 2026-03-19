"""
AlgoBot -- Green Light Score (Trade Readiness)
================================================
Module:  src/utils/trade_readiness.py
Phase:   5D -- Institutional Filters
Purpose: Compute a composite 0-100 readiness score before each trade.
         Combines regime, HTF bias, VIX, news calendar, and time-of-day
         into a single actionable number that gates entry size and direction.

Score interpretation:
  >= 80  Full size (1.0x)   -- optimal conditions on all dimensions
  60-79  Full size (1.0x)   -- good conditions, minor caution
  40-59  Half size (0.5x)   -- suboptimal, enter smaller if at all
  < 40   Skip (0.0x)        -- too many red flags, sit out

Score components and weights:
  Regime      25pts  TRENDING=25 | TRANSITIONING=15 | RANGING=10 | HIGH_VOL=5 | CRISIS=0
  HTF Bias    20pts  Aligned=20 | Neutral=10 | Counter=0 | (no bias data -> 10)
  VIX Regime  20pts  OPTIMAL=20 | ELEVATED=10 | QUIET=5 | CRISIS=0
  News Day    20pts  NONE=20 | MEDIUM=10 | HIGH=0
  Time of Day 15pts  10:30-12:00=15 | 12:00-13:00=10 | 13:00-14:00=5 | other=0

Usage:
    from src.utils.trade_readiness import GreenLightScore

    gls = GreenLightScore()
    result = gls.compute(
        regime="TRENDING",
        htf_bias="BULL",
        signal_direction="LONG",
        vix_regime="OPTIMAL",
        econ_impact="NONE",
        bar_hour=10,
    )
    print(result["score"])          # 100
    print(result["size_mult"])      # 1.0
    print(result["breakdown"])      # {'regime': 25, 'htf': 20, ...}
    print(result["summary"])        # human-readable description
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class GreenLightResult:
    """Full output of GreenLightScore.compute()."""
    score:       int                      # 0-100
    size_mult:   float                    # 0.0, 0.5, or 1.0
    action:      str                      # 'FULL_SIZE', 'HALF_SIZE', 'SKIP'
    breakdown:   Dict[str, int] = field(default_factory=dict)   # component scores
    flags:       Dict[str, str] = field(default_factory=dict)   # reasons for each score
    summary:     str = ""

    @property
    def should_trade(self) -> bool:
        return self.size_mult > 0.0

    @property
    def is_full_size(self) -> bool:
        return self.size_mult >= 1.0


class GreenLightScore:
    """
    Composite trade readiness score.

    All inputs are strings so callers don't need to import anything special.
    Missing / unknown values are handled gracefully (conservative fallback).

    Args:
        full_size_threshold: Score >= this -> full size (default 60)
        half_size_threshold: Score >= this -> half size (default 40)
        skip_threshold:      Score < this  -> skip entirely (default 40)
    """

    def __init__(
        self,
        full_size_threshold: int = 60,
        half_size_threshold: int = 40,
    ):
        self.full_threshold = full_size_threshold
        self.half_threshold = half_size_threshold

    def compute(
        self,
        regime:           str,
        htf_bias:         str,
        signal_direction: str,
        vix_regime:       str = "OPTIMAL",
        econ_impact:      str = "NONE",
        bar_hour:         int = 10,
        bar_minute:       int = 30,
    ) -> GreenLightResult:
        """
        Compute composite readiness score.

        Args:
            regime:           Market regime string ('TRENDING', 'RANGING', etc.)
            htf_bias:         HTF bias string ('BULL', 'BEAR', 'NEUTRAL')
            signal_direction: Trade direction ('LONG' or 'SHORT')
            vix_regime:       VIX regime string ('QUIET', 'OPTIMAL', 'ELEVATED', 'CRISIS')
            econ_impact:      Economic calendar impact ('NONE', 'MEDIUM', 'HIGH')
            bar_hour:         Hour of signal bar (ET, 24h)
            bar_minute:       Minute of signal bar (ET)

        Returns:
            GreenLightResult with score, size_mult, breakdown, and flags.
        """
        breakdown: Dict[str, int] = {}
        flags:     Dict[str, str] = {}

        # ── Component 1: Market Regime (25 pts) ───────────────────────────────
        regime_scores = {
            "TRENDING":      25,
            "TRANSITIONING": 15,
            "RANGING":       10,
            "HIGH_VOL":       5,
            "CRISIS":         0,
        }
        r_score = regime_scores.get(regime.upper(), 10)
        breakdown["regime"] = r_score
        flags["regime"] = regime

        # ── Component 2: HTF Bias alignment (20 pts) ──────────────────────────
        direction = signal_direction.upper()
        bias = htf_bias.upper() if htf_bias else "NEUTRAL"
        if bias == "NEUTRAL" or bias == "":
            htf_score = 10   # no bias data -- neutral (don't penalise)
            flags["htf"] = "NEUTRAL"
        elif (bias == "BULL" and direction == "LONG") or (bias == "BEAR" and direction == "SHORT"):
            htf_score = 20   # HTF agrees with signal
            flags["htf"] = f"ALIGNED ({bias})"
        elif (bias == "BULL" and direction == "SHORT") or (bias == "BEAR" and direction == "LONG"):
            htf_score = 0    # HTF opposes signal
            flags["htf"] = f"COUNTER ({bias} vs {direction})"
        else:
            htf_score = 10
            flags["htf"] = "NEUTRAL"
        breakdown["htf"] = htf_score

        # ── Component 3: VIX Regime (20 pts) ──────────────────────────────────
        vix_scores = {
            "OPTIMAL":  20,
            "ELEVATED": 10,
            "QUIET":     5,
            "CRISIS":    0,
        }
        v_score = vix_scores.get(vix_regime.upper(), 15)
        breakdown["vix"] = v_score
        flags["vix"] = vix_regime

        # ── Component 4: Economic Calendar (20 pts) ───────────────────────────
        news_scores = {
            "NONE":   20,
            "MEDIUM": 10,
            "HIGH":    0,
        }
        n_score = news_scores.get(econ_impact.upper(), 20)
        breakdown["news"] = n_score
        flags["news"] = econ_impact if econ_impact else "NONE"

        # ── Component 5: Time of Day (15 pts) ─────────────────────────────────
        # FHB signals fire around 10:00-10:30 ET; best momentum window ends ~12:30
        bar_time = bar_hour * 60 + bar_minute   # minutes since midnight ET
        if 630 <= bar_time < 720:               # 10:30-12:00 ET
            t_score = 15
            time_flag = "PRIME (10:30-12:00)"
        elif 720 <= bar_time < 780:             # 12:00-13:00 ET
            t_score = 10
            time_flag = "GOOD (12:00-13:00)"
        elif 780 <= bar_time < 840:             # 13:00-14:00 ET
            t_score = 5
            time_flag = "LATE (13:00-14:00)"
        else:
            t_score = 0
            time_flag = "OFF-HOURS"
        breakdown["time"] = t_score
        flags["time"] = time_flag

        # ── Total score ────────────────────────────────────────────────────────
        score = sum(breakdown.values())   # max 100

        # ── Size multiplier ────────────────────────────────────────────────────
        if score >= self.full_threshold:
            size_mult = 1.0
            action    = "FULL_SIZE"
        elif score >= self.half_threshold:
            size_mult = 0.5
            action    = "HALF_SIZE"
        else:
            size_mult = 0.0
            action    = "SKIP"

        # ── Hard overrides (always skip regardless of score) ──────────────────
        if econ_impact.upper() == "HIGH":
            size_mult = 0.0
            action    = "SKIP"
            flags["override"] = "HIGH_IMPACT_NEWS"
        elif vix_regime.upper() == "CRISIS":
            size_mult = 0.0
            action    = "SKIP"
            flags["override"] = "VIX_CRISIS"

        # ── Summary string ─────────────────────────────────────────────────────
        summary = (
            f"Score={score}/100 | {action} | "
            f"Regime={flags['regime']} ({breakdown['regime']}pts) | "
            f"HTF={flags['htf']} ({breakdown['htf']}pts) | "
            f"VIX={flags['vix']} ({breakdown['vix']}pts) | "
            f"News={flags['news']} ({breakdown['news']}pts) | "
            f"Time={flags['time']} ({breakdown['time']}pts)"
        )

        return GreenLightResult(
            score=score,
            size_mult=size_mult,
            action=action,
            breakdown=breakdown,
            flags=flags,
            summary=summary,
        )

    def compute_from_row(
        self,
        regime:           str,
        htf_bias:         str,
        signal_direction: str,
        vix_regime:       str = "OPTIMAL",
        econ_impact:      str = "NONE",
        bar_hour:         int = 10,
        bar_minute:       int = 30,
    ) -> GreenLightResult:
        """Alias for compute() -- kept for backward compatibility."""
        return self.compute(
            regime=regime,
            htf_bias=htf_bias,
            signal_direction=signal_direction,
            vix_regime=vix_regime,
            econ_impact=econ_impact,
            bar_hour=bar_hour,
            bar_minute=bar_minute,
        )
