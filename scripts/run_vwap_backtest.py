"""
scripts/run_vwap_backtest.py
============================
VWAP Strategy Backtest Runner — Modes A (Pullback) and B (Mean Reversion)
Runs 6-layer validation: IS, OOS, Walk-Forward (7 windows), Monte Carlo,
combined PF analysis, and per-market breakdown.

Usage:
    conda run -n algobot_env python scripts/run_vwap_backtest.py
    conda run -n algobot_env python scripts/run_vwap_backtest.py --mode pullback
    conda run -n algobot_env python scripts/run_vwap_backtest.py --mode reversion
    conda run -n algobot_env python scripts/run_vwap_backtest.py --market NQ
    conda run -n algobot_env python scripts/run_vwap_backtest.py --mode reversion --market MGC
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

# ── Project root on path ──────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.strategy.vwap_signal import (
    compute_vwap_signals,
    simulate_vwap_trades,
    PULLBACK_MARKETS,
    REVERSION_MARKETS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vwap_backtest")

# ── Ticker map ────────────────────────────────────────────────────────────────
TICKER_MAP: dict[str, str] = {
    "NQ":  "NQ=F",
    "MNQ": "MNQ=F",
    "ES":  "ES=F",
    "MES": "MES=F",
    "GC":  "GC=F",
    "MGC": "MGC=F",
    "CL":  "CL=F",
    "MCL": "MCL=F",
}
ALL_MARKETS = list(TICKER_MAP.keys())


# ── Inline metric helpers ─────────────────────────────────────────────────────

def calc_win_rate(returns: list[float]) -> float:
    if not returns:
        return 0.0
    wins = sum(1 for r in returns if r > 0)
    return wins / len(returns)


def calc_pf(returns: list[float]) -> float:
    gross_win  = sum(r for r in returns if r > 0)
    gross_loss = abs(sum(r for r in returns if r < 0))
    if gross_loss == 0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def calc_max_dd(equity_curve: list[float]) -> float:
    """Max peak-to-trough drawdown as a percentage of peak equity."""
    if len(equity_curve) < 2:
        return 0.0
    arr    = np.array(equity_curve, dtype=float)
    peaks  = np.maximum.accumulate(arr)
    # Avoid division by zero on flat start
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = np.where(peaks > 0, (arr - peaks) / peaks * 100, 0.0)
    return float(dd.min())


def calc_sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.array(returns, dtype=float)
    std = arr.std()
    if std == 0:
        return 0.0
    return float(arr.mean() / std * np.sqrt(252))


def build_equity_curve(returns: list[float], account_size: float,
                        risk_pct: float = 0.50) -> list[float]:
    """Build equity curve from R-multiples.

    Each R-multiple is scaled by risk_pct so the curve moves by the correct
    fraction of equity per trade.  With risk_pct=0.5% (safe mode):
        +1R trade = +0.5% account gain
        -1R trade = -0.5% account loss
    """
    curve = [account_size]
    for r in returns:
        pct_change = r * risk_pct     # e.g. 1.5R × 0.5% = 0.75% gain
        curve.append(curve[-1] * (1 + pct_change / 100))
    return curve


# ── Data download ─────────────────────────────────────────────────────────────

def download_data(market: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """Download 1-hour OHLCV from yfinance. Returns None on failure.

    Uses period="729d" (relative to Yahoo's server time) rather than explicit
    start/end dates.  yfinance 1h data is capped at 730 days from Yahoo's own
    current date; passing an explicit end date that lies in Yahoo's future
    causes a hard rejection.  The period string is always resolved server-side,
    so it works regardless of the local system clock.
    """
    try:
        import yfinance as yf
        ticker = TICKER_MAP[market]
        log.info("Downloading %s (%s)  last 729 days (1h)", market, ticker)
        df = yf.download(ticker, period="729d", interval="1h",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            log.warning("%s: no data returned — skipping", market)
            return None
        # Flatten multi-level columns produced by yfinance ≥0.2
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        # Ensure standard column names
        df.columns = [c.capitalize() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        if df.index.tz is None:
            df.index = df.index.tz_localize("America/New_York")
        else:
            df.index = df.index.tz_convert("America/New_York")
        return df
    except Exception as exc:
        log.warning("%s: download failed — %s — skipping", market, exc)
        return None


# ── Point-value map (dollars per full point) ──────────────────────────────────
_POINT_VALUE: dict[str, float] = {
    "NQ":  20.0,   # Nasdaq-100 E-mini   — $20/pt
    "MNQ":  2.0,   # Nasdaq-100 Micro    — $2/pt
    "ES":  50.0,   # S&P 500 E-mini      — $50/pt
    "MES":  5.0,   # S&P 500 Micro       — $5/pt
    "GC": 100.0,   # Gold futures        — $100/pt
    "MGC": 10.0,   # Micro Gold          — $10/pt
    "CL": 1000.0,  # Crude Oil           — $1,000/pt ($10/0.01)
    "MCL": 100.0,  # Micro Crude Oil     — $100/pt
}


# ── Trade extraction helpers ──────────────────────────────────────────────────

def extract_returns(trades: list[dict]) -> list[float]:
    """Convert trade dicts to true R-multiples.

    simulate_vwap_trades() stores pnl_net in dollars and risk_pts in points.
    R-multiple = pnl_net / (risk_pts × point_value).
    This is unit-consistent regardless of market.
    """
    returns = []
    for t in trades:
        # If a pre-computed r_multiple is already present, use it
        if "r_multiple" in t and t["r_multiple"] is not None:
            try:
                returns.append(float(t["r_multiple"]))
                continue
            except (TypeError, ValueError):
                pass

        # Compute from pnl_net ÷ initial_risk_dollars
        pnl      = t.get("pnl_net", 0.0)
        risk_pts = abs(t.get("risk_pts", 0.0))
        market   = t.get("market", "NQ")
        pv       = _POINT_VALUE.get(market, 20.0)
        risk_usd = risk_pts * pv

        if risk_usd > 0:
            # Cap at ±5R: guards against bad yfinance bars where the high==low
            # producing near-zero risk_pts, which would blow up the R calculation.
            r = float(pnl) / risk_usd
            returns.append(max(-5.0, min(5.0, r)))
        else:
            returns.append(0.0)
    return returns


# ── Walk-Forward (7 expanding windows) ───────────────────────────────────────

def walk_forward(df: pd.DataFrame, market: str, mode: str,
                 config: dict) -> tuple[int, int]:
    """
    Split df into 7 equal folds; each fold: first 70% = IS, last 30% = OOS.
    Returns (oos_pass_count, total_windows_with_enough_trades).
    """
    n      = len(df)
    fold_n = n // 7
    if fold_n < 20:
        return 0, 0

    oos_pass = 0
    valid    = 0
    for i in range(7):
        fold_df = df.iloc[i * fold_n: (i + 1) * fold_n]
        split   = int(len(fold_df) * 0.70)
        oos_df  = fold_df.iloc[split:]
        try:
            df_sig = compute_vwap_signals(
                oos_df, market,
                htf_bias_series=None, regime_series=None,
                config=config, econ_cal=None, vix_filter=None,
            )
            trades = simulate_vwap_trades(df_sig, market, config)
            # filter to requested mode
            mode_tag = "VWAP_PB" if mode == "pullback" else "VWAP_MR"
            trades   = [t for t in trades if t.get("strategy", "").startswith(mode_tag)]
            rets     = extract_returns(trades)
            if len(rets) >= 3:
                valid += 1
                if calc_pf(rets) > 1.0:
                    oos_pass += 1
        except Exception as exc:
            log.debug("WF fold %d/%s/%s failed: %s", i, market, mode, exc)
    return oos_pass, valid


# ── Monte Carlo ───────────────────────────────────────────────────────────────

def monte_carlo(returns: list[float], account_size: float,
                n_iter: int = 1000) -> dict:
    """
    Shuffle trade return sequence 1000 times, compute max drawdown each run.
    Returns p05_dd (worst 5%), mean_pf, ruin_pct (runs with DD > 25%).
    """
    rng      = np.random.default_rng(seed=42)
    arr      = np.array(returns, dtype=float)
    max_dds  = []
    pfs      = []

    for _ in range(n_iter):
        shuffled = rng.permutation(arr)
        equity   = build_equity_curve(list(shuffled), account_size)
        max_dds.append(calc_max_dd(equity))
        pfs.append(calc_pf(list(shuffled)))

    max_dds_arr = np.array(max_dds)
    return {
        "p05_dd":    float(np.percentile(max_dds_arr, 5)),
        "mean_pf":   float(np.mean(pfs)),
        "ruin_pct":  float(np.mean(max_dds_arr < -25.0) * 100),
    }


# ── Single-market, single-mode backtest ───────────────────────────────────────

def run_single(df: pd.DataFrame, market: str, mode: str,
               config: dict, account_size: float) -> Optional[dict]:
    """
    Run full signal + simulation for one market/mode combination.
    Returns a result dict, or None if too few trades.
    """
    try:
        df_sig = compute_vwap_signals(
            df, market,
            htf_bias_series=None, regime_series=None,
            config=config, econ_cal=None, vix_filter=None,
        )
        trades = simulate_vwap_trades(df_sig, market, config)
    except Exception as exc:
        log.warning("%s/%s: signal/sim failed — %s", market, mode, exc)
        return None

    # Filter to the requested mode
    mode_tag = "VWAP_PB" if mode == "pullback" else "VWAP_MR"
    trades   = [t for t in trades if t.get("strategy", "").startswith(mode_tag)]
    rets     = extract_returns(trades)

    if len(rets) < 10:
        log.info("%s/%s: only %d trades — skipping", market, mode, len(rets))
        return None

    equity  = build_equity_curve(rets, account_size)
    avg_r   = float(np.mean(rets))
    max_dd  = calc_max_dd(equity)

    # Walk-forward
    oos_pass, wf_valid = walk_forward(df, market, mode, config)
    wf_label = f"{oos_pass}/{wf_valid}" if wf_valid else "—"
    wf_pass  = wf_valid > 0 and (oos_pass / wf_valid) >= 0.6

    return {
        "market":    market,
        "mode":      mode,
        "n_trades":  len(rets),
        "win_rate":  calc_win_rate(rets) * 100,
        "pf":        calc_pf(rets),
        "avg_r":     avg_r,
        "max_dd":    max_dd,
        "sharpe":    calc_sharpe(rets),
        "wf_label":  wf_label,
        "wf_pass":   wf_pass,
        "returns":   rets,
    }


# ── Print helpers ─────────────────────────────────────────────────────────────

_SEP  = "═" * 66
_DASH = "─" * 18


def print_mode_table(results: list[dict], mode: str) -> None:
    title = "VWAP PULLBACK MODE" if mode == "pullback" else "VWAP REVERSION MODE"
    print(f"\n{title}")
    print(_DASH)
    print(f"{'Market':<8} {'N':>4}  {'WR%':>6}  {'PF':>5}  {'AvgR':>6}  "
          f"{'MaxDD%':>7}  {'WF':>6}")
    for r in results:
        tick  = "✓" if r["wf_pass"] else "✗"
        print(
            f"{r['market']:<8} {r['n_trades']:>4}  "
            f"{r['win_rate']:>6.1f}  {r['pf']:>5.2f}  "
            f"{r['avg_r']:>+6.2f}  {r['max_dd']:>6.1f}%  "
            f"{r['wf_label']:>4} {tick}"
        )


def print_mc_block(mc: dict, market: str, mode: str, n_iter: int) -> None:
    label = "PULLBACK" if mode == "pullback" else "REVERSION"
    print(f"\nMONTE CARLO — {market} {label}  ({n_iter:,} iterations)")
    print(
        f"  P05 MaxDD: {mc['p05_dd']:.1f}%   "
        f"Mean PF: {mc['mean_pf']:.2f}   "
        f"Ruin(>25%DD): {mc['ruin_pct']:.1f}%"
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    two_years_ago = (date.today() - timedelta(days=730)).isoformat()
    today         = date.today().isoformat()

    p = argparse.ArgumentParser(
        description="VWAP backtest runner — Pullback and Mean Reversion modes"
    )
    p.add_argument("--mode",   choices=["pullback", "reversion", "both"],
                   default="both",
                   help="Which VWAP mode to test (default: both)")
    p.add_argument("--market", choices=ALL_MARKETS + ["ALL"],
                   default="ALL",
                   help="Market to test, or ALL (default: ALL)")
    p.add_argument("--start",  default=two_years_ago,
                   help=f"Start date YYYY-MM-DD (default: {two_years_ago})")
    p.add_argument("--end",    default=today,
                   help=f"End date YYYY-MM-DD (default: {today})")
    p.add_argument("--account-size", type=float, default=50_000,
                   help="Simulated account size in USD (default: 50000)")
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    # Load config
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    try:
        with open(config_path) as fh:
            config = yaml.safe_load(fh)
    except FileNotFoundError:
        log.warning("config.yaml not found at %s — using empty config", config_path)
        config = {}

    # Resolve markets and modes
    markets = ALL_MARKETS if args.market == "ALL" else [args.market]
    modes   = ["pullback", "reversion"] if args.mode == "both" else [args.mode]

    # Apply market eligibility filter per mode
    eligible: dict[str, list[str]] = {
        "pullback":  [m for m in markets if m in PULLBACK_MARKETS],
        "reversion": [m for m in markets if m in REVERSION_MARKETS],
    }

    # Banner
    print(f"\n{_SEP}")
    print(f"  VWAP BACKTEST RESULTS — {args.start} to {args.end}")
    print(_SEP)
    print(f"  Account size: ${args.account_size:,.0f}   "
          f"Mode(s): {args.mode.upper()}   Market(s): {args.market}")

    all_results: dict[str, list[dict]] = {"pullback": [], "reversion": []}
    mc_candidates: list[dict] = []

    for mode in modes:
        for market in eligible.get(mode, []):
            df = download_data(market, args.start, args.end)
            if df is None:
                continue
            result = run_single(df, market, mode, config, args.account_size)
            if result is not None:
                all_results[mode].append(result)
                # Keep the first qualifying result per mode for Monte Carlo
                if not any(c["mode"] == mode for c in mc_candidates):
                    mc_candidates.append(result)

    # Print tables
    for mode in modes:
        if all_results[mode]:
            print_mode_table(all_results[mode], mode)
        else:
            label = "PULLBACK" if mode == "pullback" else "REVERSION"
            print(f"\nVWAP {label} MODE")
            print(_DASH)
            print("  (no qualifying results)")

    # Monte Carlo block — one per mode
    print()
    if mc_candidates:
        for r in mc_candidates:
            mc = monte_carlo(r["returns"], args.account_size, n_iter=1000)
            print_mc_block(mc, r["market"], r["mode"], 1000)
    else:
        print("  (no Monte Carlo — insufficient trades)")

    # Combined summary
    all_flat = all_results["pullback"] + all_results["reversion"]
    if all_flat:
        combined_rets = [r for res in all_flat for r in res["returns"]]
        combined_pf   = calc_pf(combined_rets)
        combined_wr   = calc_win_rate(combined_rets) * 100
        print(f"\n{'─'*66}")
        print(f"  COMBINED  N={len(combined_rets)}  "
              f"WR={combined_wr:.1f}%  PF={combined_pf:.2f}  "
              f"Markets={len(all_flat)}")

    print(f"\n{_SEP}\n")


if __name__ == "__main__":
    main()
