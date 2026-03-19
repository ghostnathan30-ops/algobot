"""
AlgoBot -- VIX Regime Filter
==============================
Module:  src/utils/vix_filter.py
Phase:   5D -- Institutional Filters
Purpose: Classify the VIX volatility regime each day and gate trade entries.

Why VIX matters for intraday strategies:
  - VIX < 13 (QUIET):  Ranges are too tight. FHB first-hour ranges are 6-10 pts
    (ES), making 2R target nearly unreachable within the hold window.
    False breakouts dominate as institutions wait for catalysts.
  - VIX 13-28 (OPTIMAL): Normal operating environment. ATR-based stops and 2R
    targets calibrated to this regime. Best win rates historically.
  - VIX 28-35 (ELEVATED): Wider ranges but also wider intrabar whipsaws.
    Trade at half size.  Swing strategy performs well here (trends accelerate).
  - VIX > 35 (CRISIS): Gap risk, circuit breakers, policy interventions. All
    intraday entries suspended. Swing positions held with wider stops.

Data source: Yahoo Finance ^VIX (daily close), free, already in data pipeline.

Usage:
    vf = VIXFilter(vix_series)          # pd.Series indexed by date
    vf.get_regime(date(2024, 3, 20))    # -> "OPTIMAL"
    vf.get_size_mult(date(2024, 3, 20)) # -> 1.0
    vf.should_skip(date(2024, 8, 5))    # -> True (CRISIS or QUIET)
"""

from __future__ import annotations

import datetime
from typing import Optional

import pandas as pd

from src.utils.logger import get_logger

log = get_logger(__name__)

# ── VIX thresholds ─────────────────────────────────────────────────────────────
VIX_QUIET_MAX    = 13.0   # Below this: too quiet, skip intraday
VIX_OPTIMAL_MAX  = 28.0   # Below this (and above QUIET): full size
VIX_ELEVATED_MAX = 35.0   # Below this (and above OPTIMAL): half size
# Above ELEVATED_MAX: CRISIS -- skip all intraday

_REGIME_QUIET    = "QUIET"
_REGIME_OPTIMAL  = "OPTIMAL"
_REGIME_ELEVATED = "ELEVATED"
_REGIME_CRISIS   = "CRISIS"

# Position size multiplier per regime
_SIZE_MULT = {
    _REGIME_QUIET:    0.0,   # skip
    _REGIME_OPTIMAL:  1.0,   # full size
    _REGIME_ELEVATED: 0.5,   # half size
    _REGIME_CRISIS:   0.0,   # skip
}


class VIXFilter:
    """
    VIX-based volatility regime classifier.

    Args:
        vix_series: pd.Series with DatetimeIndex (or date keys) and float VIX values.
                    If None, VIX filtering is disabled (all dates return OPTIMAL).
    """

    def __init__(self, vix_series: Optional[pd.Series] = None):
        self._vix: Optional[pd.Series] = None
        if vix_series is not None:
            # Normalise index to date objects for fast lookup
            idx = vix_series.index
            if hasattr(idx, "date"):
                vix_series.index = idx.date
            elif hasattr(idx[0], "date") and callable(idx[0].date):
                vix_series.index = pd.Index([x.date() for x in idx])
            self._vix = vix_series.dropna()
            log.info(
                "VIXFilter loaded: {n} days | range {lo:.1f}-{hi:.1f}",
                n=len(self._vix),
                lo=float(self._vix.min()),
                hi=float(self._vix.max()),
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    def get_vix_level(self, dt) -> Optional[float]:
        """Return the VIX close for `dt`, or None if not available."""
        if self._vix is None:
            return None
        d = _to_date(dt)
        return float(self._vix.get(d, float("nan"))) if d in self._vix.index else None

    def get_regime(self, dt) -> str:
        """
        Return VIX regime for `dt`.

        Returns one of: 'QUIET', 'OPTIMAL', 'ELEVATED', 'CRISIS'.
        Returns 'OPTIMAL' if VIX data is unavailable (conservative fallback).
        """
        level = self.get_vix_level(dt)
        if level is None or pd.isna(level):
            return _REGIME_OPTIMAL   # data missing -> don't block
        return _classify(level)

    def get_size_mult(self, dt) -> float:
        """
        Return position size multiplier for `dt` based on VIX regime.

        Returns:
            1.0 for OPTIMAL, 0.5 for ELEVATED, 0.0 for QUIET or CRISIS.
        """
        return _SIZE_MULT[self.get_regime(dt)]

    def should_skip(self, dt) -> bool:
        """Return True if ALL intraday entries should be skipped today."""
        return self.get_size_mult(dt) == 0.0

    def get_summary(self, dt) -> dict:
        """Return a full summary dict for logging / alert display."""
        level = self.get_vix_level(dt)
        regime = _classify(level) if level is not None and not pd.isna(level) else _REGIME_OPTIMAL
        return {
            "vix_level":  round(level, 2) if level is not None and not pd.isna(level) else None,
            "vix_regime": regime,
            "size_mult":  _SIZE_MULT[regime],
            "skip":       _SIZE_MULT[regime] == 0.0,
        }

    def add_to_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Add vix_level and vix_regime columns to a daily DataFrame.
        The DataFrame must have a DatetimeIndex or date index.

        Returns df with new columns added (or 'OPTIMAL'/'NaN' if VIX unavailable).
        """
        df = df.copy()
        if self._vix is None:
            df["vix_level"]  = float("nan")
            df["vix_regime"] = _REGIME_OPTIMAL
            return df

        dates = [_to_date(d) for d in df.index]
        df["vix_level"]  = [self._vix.get(d, float("nan")) for d in dates]
        df["vix_regime"] = [
            _classify(v) if not pd.isna(v) else _REGIME_OPTIMAL
            for v in df["vix_level"]
        ]
        return df

    @classmethod
    def from_yahoo(cls, start: str = "2003-01-01", end: str = "2027-12-31") -> "VIXFilter":
        """
        Convenience constructor: download VIX from Yahoo Finance and return filter.

        Args:
            start: ISO date string, start of download range.
            end:   ISO date string, end of download range.

        Returns:
            VIXFilter instance with downloaded data.
        """
        try:
            import yfinance as yf
            ticker = yf.Ticker("^VIX")
            raw = ticker.history(start=start, end=end, interval="1d")
            if raw.empty:
                log.warning("VIXFilter.from_yahoo: no data returned, filter disabled")
                return cls(None)
            series = raw["Close"].rename("vix")
            log.info(
                "VIXFilter.from_yahoo: downloaded {n} VIX bars {s} to {e}",
                n=len(series), s=start, e=end,
            )
            return cls(series)
        except Exception as exc:
            log.warning("VIXFilter.from_yahoo failed: {err} -- filter disabled", err=exc)
            return cls(None)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _classify(vix: float) -> str:
    if vix < VIX_QUIET_MAX:
        return _REGIME_QUIET
    if vix <= VIX_OPTIMAL_MAX:
        return _REGIME_OPTIMAL
    if vix <= VIX_ELEVATED_MAX:
        return _REGIME_ELEVATED
    return _REGIME_CRISIS


def _to_date(dt) -> datetime.date:
    if hasattr(dt, "date") and callable(dt.date):
        return dt.date()
    if isinstance(dt, datetime.datetime):
        return dt.date()
    if isinstance(dt, datetime.date):
        return dt
    return datetime.date.fromisoformat(str(dt)[:10])
