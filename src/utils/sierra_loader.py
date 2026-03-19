"""
AlgoBot — Sierra Charts Data Loader
=====================================
Module:  src/utils/sierra_loader.py
Purpose: Load, parse, and stitch Sierra Charts exported contract files into
         continuous price series for backtesting.

Sierra Charts exports individual expiry contracts (e.g. NQH26, GCJ26) as
.txt files. This module stitches them chronologically into a single DataFrame
per market/timeframe using proper front-contract roll logic.

Supported markets:
  NQ   — E-mini Nasdaq-100 (CME)
  MNQ  — Micro E-mini Nasdaq-100 (CME)
  GC   — Gold Futures (COMEX)
  MGC  — Micro Gold Futures (COMEX)
  CL   — Crude Oil Futures (NYMEX)

Supported timeframes:
  daily — End-of-day bars
  1h    — 1-hour bars
  5m    — 5-minute bars

File naming convention (as exported by Sierra Charts):
  NQZ25-CME_5m.scid_BarData.txt
  NQH26-CME_1H.scid_BarData.txt
  NQM26-CME.DAILY_BarData.txt
  GCJ26-COMEX_5m.scid_BarData.txt
  CLK26-NYMEX_1H.scid_BarData.txt

Column format (intraday):
  Date, Time, Open, High, Low, Last, Volume, NumberOfTrades, BidVolume, AskVolume

Column format (daily):
  Date, Time, Open, High, Low, Last, Volume, OpenInterest

Date format: YYYY/M/D (e.g. 2025/11/19)
Time format: HH:MM:SS

Timezone:  All Sierra Charts times are assumed to be Eastern Time (ET),
           which matches the user's Windows system timezone. The loader
           localises to America/New_York and the RTH filter (09:30–16:00)
           applies correctly to intraday strategies.

Roll logic:
  - For each trading date, use the contract whose expiry is the nearest
    future date that hasn't yet hit its roll threshold.
  - Roll thresholds (calendar days before expiry):
      CME equity (NQ, MNQ):  10 days before 3rd Friday of expiry month
      COMEX gold  (GC, MGC): 10 days before last business day of prior month
      NYMEX crude (CL):      10 days before ~22nd of prior month

Usage:
    from src.utils.sierra_loader import load_sc_continuous

    df_1h = load_sc_continuous("NQ",  "1h",    sc_dir)  # 1-hour continuous
    df_5m = load_sc_continuous("GC",  "5m",    sc_dir)  # 5-minute continuous
    df_d  = load_sc_continuous("CL",  "daily", sc_dir)  # daily continuous
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# ── Expiry month letter map (CME/COMEX/NYMEX standard) ────────────────────────
_MONTH_LETTERS: dict[str, int] = {
    "F": 1,  "G": 2,  "H": 3,  "J": 4,  "K": 5,  "M": 6,
    "N": 7,  "Q": 8,  "U": 9,  "V": 10, "X": 11, "Z": 12,
}

# ── Root-symbol → exchange tag in filename ─────────────────────────────────────
_EXCHANGE: dict[str, str] = {
    "NQ":  "CME",
    "MNQ": "CME",
    "GC":  "COMEX",
    "MGC": "COMEX",
    "CL":  "NYMEX",
}

# ── Roll days: how many calendar days before expiry we roll off ────────────────
_ROLL_DAYS: dict[str, int] = {
    "NQ":  10,
    "MNQ": 10,
    "GC":  10,
    "MGC": 10,
    "CL":  10,
}


# ── Expiry date helpers ────────────────────────────────────────────────────────

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the n-th occurrence of weekday (0=Mon, 4=Fri) in given month."""
    d = date(year, month, 1)
    # Advance to first occurrence of target weekday
    days_ahead = weekday - d.weekday()
    if days_ahead < 0:
        days_ahead += 7
    d += timedelta(days=days_ahead)
    return d + timedelta(weeks=n - 1)


def _last_business_day(year: int, month: int) -> date:
    """Return the last Mon–Fri of given month."""
    # Start from last day of month and walk back to weekday
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    while last.weekday() > 4:   # Sat=5, Sun=6
        last -= timedelta(days=1)
    return last


def _expiry_date(root: str, month_letter: str, year_2digit: int) -> date:
    """
    Compute the last trading day for a futures contract.

    CME equity (NQ, MNQ):
        3rd Friday of expiry month (Quarterly: Mar/Jun/Sep/Dec)

    COMEX gold (GC, MGC):
        Last business day of the month BEFORE the delivery month.
        e.g. GCJ26 (Apr delivery) → last biz day of Mar 2026

    NYMEX crude (CL):
        ~22nd of the month before delivery (simplified: last biz day of prior
        month minus ~3 days). We use the 20th as a conservative approximation
        so rolls happen well before actual expiry.
    """
    year  = 2000 + year_2digit
    month = _MONTH_LETTERS[month_letter]

    if root in ("NQ", "MNQ"):
        return _nth_weekday(year, month, 4, 3)   # 3rd Friday

    if root in ("GC", "MGC"):
        # Last biz day of month PRIOR to delivery month
        prior_month = month - 1 if month > 1 else 12
        prior_year  = year if month > 1 else year - 1
        return _last_business_day(prior_year, prior_month)

    if root == "CL":
        # ~20th of the month prior to delivery month (conservative)
        prior_month = month - 1 if month > 1 else 12
        prior_year  = year if month > 1 else year - 1
        d = date(prior_year, prior_month, 20)
        while d.weekday() > 4:
            d -= timedelta(days=1)
        return d

    # Fallback: last biz day of expiry month
    return _last_business_day(year, month)


def _roll_date(root: str, month_letter: str, year_2digit: int) -> date:
    """Roll off this contract N days before its last trading day."""
    exp   = _expiry_date(root, month_letter, year_2digit)
    delta = _ROLL_DAYS.get(root, 10)
    d     = exp - timedelta(days=delta)
    while d.weekday() > 4:
        d -= timedelta(days=1)
    return d


# ── Filename parser ────────────────────────────────────────────────────────────

# Matches: NQH26-CME_5m, GCJ26-COMEX_1H, CLK26-NYMEX.DAILY, MNQZ25-CME_1h
# Also handles oddly-named: NQM26-CME.scid_1H, MNQZ26-CME_1h
_FILE_RE = re.compile(
    r"^(?P<root>[A-Z]+)(?P<month>[FGHJKMNQUVXZ])(?P<year>\d{2})"
    r"-(?P<exch>[A-Z]+)"
    r"(?:[._](?:scid_)?(?P<tf>[A-Za-z0-9]+))"
    r"(?:\.scid)?"        # some files: _1H.scid_BarData.txt — .scid after TF
    r"(?:_BarData)?\.txt$",
    re.IGNORECASE,
)

def _tf_canonical(raw: str) -> str:
    """Normalise timeframe token to 'daily', '1h', or '5m'."""
    t = raw.lower()
    if "daily" in t:
        return "daily"
    if t in ("1h", "1hour"):
        return "1h"
    if t in ("5m", "5min"):
        return "5m"
    return t


def _parse_filename(fname: str) -> dict | None:
    """
    Parse a Sierra Charts BarData filename.

    Returns dict with keys: root, month, year, exchange, tf
    or None if the filename doesn't match.

    Handles naming quirks like:
        NQM26-CME.scid_1H_BarData.txt
        MNQZ26-CME_1h.scid_BarData.txt
        NQM26-CME.DAILY_BarData.txt
    """
    # Normalize the filename slightly before matching
    name = fname
    m = _FILE_RE.match(name)
    if not m:
        return None
    tf = _tf_canonical(m.group("tf") or "")
    if tf not in ("daily", "1h", "5m"):
        return None
    return {
        "root":     m.group("root").upper(),
        "month":    m.group("month").upper(),
        "year":     int(m.group("year")),
        "exchange": m.group("exch").upper(),
        "tf":       tf,
        "fname":    fname,
    }


# ── File loader ────────────────────────────────────────────────────────────────

def _load_sc_file(path: Path) -> pd.DataFrame:
    """
    Load a single Sierra Charts BarData .txt file into a DataFrame.

    Standardises columns to: Open, High, Low, Close, Volume
    Returns DataFrame with DatetimeIndex (timezone-naive, ET-assumed).
    Returns empty DataFrame on any parse failure.
    """
    try:
        raw = pd.read_csv(
            path,
            sep=",",
            skipinitialspace=True,
            low_memory=False,
        )
    except Exception:
        return pd.DataFrame()

    raw.columns = [c.strip() for c in raw.columns]

    # Rename 'Last' → 'Close' (Sierra Charts standard)
    if "Last" in raw.columns:
        raw = raw.rename(columns={"Last": "Close"})

    required = {"Date", "Time", "Open", "High", "Low", "Close"}
    if not required.issubset(raw.columns):
        return pd.DataFrame()

    # Build datetime index: Date="2025/11/19", Time="20:15:00"
    try:
        raw["_dt"] = pd.to_datetime(
            raw["Date"].str.strip() + " " + raw["Time"].str.strip(),
            format="%Y/%m/%d %H:%M:%S",
        )
    except Exception:
        # Try flexible parser as fallback
        try:
            raw["_dt"] = pd.to_datetime(
                raw["Date"].str.strip() + " " + raw["Time"].str.strip()
            )
        except Exception:
            return pd.DataFrame()

    raw = raw.set_index("_dt")
    raw.index.name = "Timestamp"

    keep = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in raw.columns]
    df   = raw[keep].copy()

    for col in ["Open", "High", "Low", "Close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "Volume" in df.columns:
        df["Volume"] = pd.to_numeric(df["Volume"], errors="coerce").fillna(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    df.sort_index(inplace=True)
    return df


# ── Continuous contract stitcher ───────────────────────────────────────────────

def load_sc_continuous(
    root:   str,
    tf:     str,
    sc_dir: Path | str,
    tz:     str = "America/New_York",
) -> pd.DataFrame:
    """
    Build a continuous price series for ``root`` at timeframe ``tf`` by
    loading and stitching all matching Sierra Charts contract files.

    Args:
        root:   Market root symbol — "NQ", "MNQ", "GC", "MGC", "CL"
        tf:     Timeframe — "daily", "1h", "5m"
        sc_dir: Path to the sierra_charts data folder
        tz:     Target timezone for the DatetimeIndex (default ET)

    Returns:
        DataFrame with tz-aware DatetimeIndex and columns:
        Open, High, Low, Close, Volume, contract

        The ``contract`` column records which expiry was active for each bar
        (useful for debugging roll behaviour).

    Raises:
        ValueError: If no files match root+tf in sc_dir.
    """
    sc_dir = Path(sc_dir)
    tf_    = _tf_canonical(tf)
    root_  = root.upper()

    # ── 1. Collect and parse all matching files ────────────────────────────
    contracts: list[dict] = []
    for fpath in sorted(sc_dir.glob("*.txt")):
        info = _parse_filename(fpath.name)
        if info is None:
            continue
        if info["root"] != root_ or info["tf"] != tf_:
            continue
        info["path"]      = fpath
        info["roll_off"]  = _roll_date(root_, info["month"], info["year"])
        info["expiry"]    = _expiry_date(root_, info["month"], info["year"])
        contracts.append(info)

    if not contracts:
        raise ValueError(
            f"No Sierra Charts files found for {root_} / {tf_} in {sc_dir}. "
            f"Files must match pattern like NQH26-CME_1H_BarData.txt"
        )

    # Sort by expiry so we process contracts in chronological order
    contracts.sort(key=lambda x: x["expiry"])

    print(f"  [{root_}/{tf_}] Found {len(contracts)} contract file(s):")
    for c in contracts:
        print(f"    {c['fname']}  roll_off={c['roll_off']}  expiry={c['expiry']}")

    # ── 2. Load each file ──────────────────────────────────────────────────
    loaded: list[tuple[dict, pd.DataFrame]] = []
    for c in contracts:
        df = _load_sc_file(c["path"])
        if df.empty:
            print(f"    WARNING: {c['fname']} loaded 0 bars — skipping")
            continue
        loaded.append((c, df))

    if not loaded:
        raise ValueError(f"All contract files for {root_}/{tf_} were empty.")

    # ── 3. Stitch: for each bar use the front contract ─────────────────────
    # Build a roll schedule: {date → contract_label}
    # A contract is "active" from its previous roll-off date until its own
    # roll-off date. The first contract is active from the beginning.

    # For each loaded contract, determine its active date window:
    #   start: roll_off of the PREVIOUS contract (or beginning of time)
    #   end:   roll_off of THIS contract (exclusive)

    active_windows: list[tuple[date, date, str, pd.DataFrame]] = []
    for i, (c, df) in enumerate(loaded):
        start_d = loaded[i - 1][0]["roll_off"] if i > 0 else date(2000, 1, 1)
        end_d   = c["roll_off"]
        label   = f"{c['root']}{c['month']}{c['year']:02d}"
        active_windows.append((start_d, end_d, label, df))

    # The LAST contract is active from its window start until data ends
    # (no end cap — it's the front month now)
    if active_windows:
        s, _, label, df = active_windows[-1]
        active_windows[-1] = (s, date(2099, 12, 31), label, df)

    # ── 4. Filter each df to its active window and concatenate ─────────────
    pieces: list[pd.DataFrame] = []
    for start_d, end_d, label, df in active_windows:
        mask = (df.index.date >= start_d) & (df.index.date < end_d)
        chunk = df[mask].copy()
        if chunk.empty:
            continue
        chunk["contract"] = label
        pieces.append(chunk)

    if not pieces:
        raise ValueError(f"Stitching produced 0 bars for {root_}/{tf_}.")

    combined = pd.concat(pieces).sort_index()

    # Remove any duplicate timestamps (overlap at roll boundaries)
    combined = combined[~combined.index.duplicated(keep="last")]

    # ── 5. Localise to target timezone ────────────────────────────────────
    if combined.index.tz is None:
        combined.index = combined.index.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    else:
        combined.index = combined.index.tz_convert(tz)

    combined.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    combined.sort_index(inplace=True)

    n_days = combined.index.normalize().nunique()
    print(
        f"  [{root_}/{tf_}] Continuous series: {len(combined):,} bars | "
        f"{n_days} trading days | "
        f"{combined.index[0].strftime('%Y-%m-%d')} → "
        f"{combined.index[-1].strftime('%Y-%m-%d')}"
    )
    return combined


# ── HTF daily data from Sierra Charts ─────────────────────────────────────────

def load_sc_daily_for_htf(
    root:   str,
    sc_dir: Path | str,
) -> pd.DataFrame:
    """
    Load daily SC data for a market and normalise the index to date-only
    (timezone-naive) so it can be used directly as HTF daily input to
    calculate_indicators() / add_htf_bias().

    Returns DataFrame with DatetimeIndex (tz-naive, date-normalised) and
    columns: Open, High, Low, Close, Volume.
    """
    df = load_sc_continuous(root, "daily", sc_dir)
    # Drop tz info and normalize to midnight for compatibility with htf_bias
    df.index = df.index.tz_localize(None).normalize()
    df.index.name = "Date"
    df = df.drop(columns=["contract"], errors="ignore")
    return df


# ── Convenience: load all markets ─────────────────────────────────────────────

def load_all_sc_markets(
    sc_dir: Path | str,
    timeframe: str,
    markets: list[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Load continuous price series for all available SC markets at one timeframe.

    Args:
        sc_dir:    Path to data/sierra_charts/
        timeframe: "daily", "1h", or "5m"
        markets:   List of root symbols to load. Defaults to all available.

    Returns:
        Dict mapping root symbol → continuous DataFrame.
        Markets that fail to load are excluded (with a warning printed).
    """
    sc_dir    = Path(sc_dir)
    markets_  = [m.upper() for m in (markets or list(_EXCHANGE.keys()))]
    results   = {}

    for mkt in markets_:
        try:
            df = load_sc_continuous(mkt, timeframe, sc_dir)
            results[mkt] = df
        except ValueError as e:
            print(f"  WARNING [{mkt}/{timeframe}]: {e}")

    return results
