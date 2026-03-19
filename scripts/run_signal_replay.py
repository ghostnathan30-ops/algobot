"""
AlgoBot -- Signal Replay & Live Preview
=========================================
Script:  scripts/run_signal_replay.py
Purpose: Replay the bot's signal decisions on real historical data.
         Shows exactly what the bot would have done on each day --
         which signals fired, which were filtered, entry/stop/target,
         and the actual outcome.  Runs anytime, no market hours needed.

Modes:
    python scripts/run_signal_replay.py              # Last 60 days (quick)
    python scripts/run_signal_replay.py --days 120   # Last 120 days
    python scripts/run_signal_replay.py --full       # Full 730-day dataset
    python scripts/run_signal_replay.py --today      # Today's signal preview
    python scripts/run_signal_replay.py --submit     # Submit today's signal to IBKR paper

Run:
    cd AlgoBot
    conda run -n algobot_env python scripts/run_signal_replay.py
"""

from __future__ import annotations

import json
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from run_fhb_backtest import (
    download_1h_intraday, compute_fhb_signals, simulate_fhb_trades,
    compute_1h_atr, get_htf_data,
    FHB_ATR_PERIOD, FHB_ATR_STOP_MULT, FHB_GLS_HALF_SCORE, FHB_GLS_MIN_SCORE,
    FHB_OVERNIGHT_CARRY,
)
from src.utils.yf_intraday import download_all_intraday
from src.strategy.orb_signal import compute_orb_signals
from src.strategy.indicators import calculate_indicators, add_atr_baseline
from src.strategy.regime_classifier import classify_regimes
from src.strategy.htf_bias import add_htf_bias
from src.utils.data_downloader import download_market
from src.utils.econ_calendar import EconCalendar
from src.utils.vix_filter import VIXFilter
from src.utils.trade_readiness import GreenLightScore
from src.utils.logger import get_logger

log = get_logger(__name__)
MARKETS = ["ES", "NQ"]

# Topstep $150k account: $3,000/day maximum loss rule
DAILY_HARD_STOP_USD = 3_000.0

# Signal ordering within a day (ORB fires 09:45, FHB fires 10:30)
_STRATEGY_ORDER = {"ORB": 0, "FHB": 1}


def _apply_daily_hard_stop(trades: list[dict], hard_stop_usd: float) -> list[dict]:
    """
    Simulate the Topstep daily hard stop: once cumulative day P&L drops to or
    below -hard_stop_usd no further signals are taken for that session.
    Ordering within a day: ORB first (09:45 ET), then FHB (10:30 ET).
    """
    by_date: dict[str, list[dict]] = {}
    for t in trades:
        by_date.setdefault(t["date"], []).append(t)

    kept: list[dict] = []
    for date_str in sorted(by_date):
        day_trades = sorted(
            by_date[date_str],
            key=lambda t: (_STRATEGY_ORDER.get(t.get("strategy", ""), 9), t.get("market", "")),
        )
        day_pnl = 0.0
        for t in day_trades:
            if day_pnl <= -hard_stop_usd:
                break  # daily stop fired — no more entries today
            kept.append(t)
            day_pnl += t["pnl_net"]
    return kept


def load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "config.yaml") as f:
        return yaml.safe_load(f)


def get_htf_bias_series(market: str, config: dict) -> pd.Series:
    raw = download_market(market, "2022-01-01", "2026-12-31")
    if raw is None or raw.empty:
        return pd.Series(dtype=str)
    strat_cfg  = config.get("strategy", config)
    regime_cfg = config.get("regime", strat_cfg)
    df = calculate_indicators(raw, strat_cfg, market)
    df = add_atr_baseline(df)
    df = classify_regimes(df, regime_cfg, market)
    df = add_htf_bias(df, config, market)
    bias = df["htf_combined_bias"].copy()
    bias.index = pd.to_datetime(bias.index).normalize()
    return bias


# ============================================================
# ORB replay helpers
# ============================================================

def simulate_orb_single_day(df_day: pd.DataFrame, market: str,
                             config: dict) -> dict | None:
    """Run ORB simulation on a single day's 5-min bars. Return trade dict or None."""
    orb_cfg     = config.get("intraday", {}).get("orb", {})
    partial_r   = float(orb_cfg.get("partial_exit_r",   1.0))
    full_r      = float(orb_cfg.get("profit_target_r",  2.0))
    partial_pct = float(orb_cfg.get("partial_exit_pct", 0.5))
    max_bars    = int(orb_cfg.get("max_hold_bars",      24))
    mkt_cfg     = config.get("markets", {}).get(market, {})
    point_value = float(mkt_cfg.get("point_value",  50.0))
    commission  = float(mkt_cfg.get("commission",    5.0))
    tick_size   = float(mkt_cfg.get("tick_size",    0.25))
    slippage    = int(  mkt_cfg.get("slippage_ticks", 1)) * tick_size

    bars = df_day.reset_index()
    for i, row in bars.iterrows():
        is_long  = bool(row.get("orb_long_signal",  False))
        is_short = bool(row.get("orb_short_signal", False))
        blocked  = bool(row.get("orb_htf_blocked",  False))
        if not (is_long or is_short) or blocked:
            continue
        if i + 1 >= len(bars):
            continue

        rh    = float(row["orb_range_high"])
        rl    = float(row["orb_range_low"])
        entry_raw = float(bars.iloc[i + 1]["Open"])
        entry = entry_raw + slippage if is_long else entry_raw - slippage
        stop  = rl - slippage       if is_long else rh + slippage
        risk  = abs(entry - stop)
        if risk <= 0:
            continue

        # Priority 1A: Hard dollar loss cap (same as run_orb_backtest.py)
        max_risk = 2_000.0 / point_value
        if risk > max_risk:
            risk = max_risk
            stop = (entry - risk) if is_long else (entry + risk)

        t1 = entry + partial_r * risk * (1 if is_long else -1)
        t2 = entry + full_r    * risk * (1 if is_long else -1)

        partial_taken = False
        exit_price    = None
        exit_reason   = "time"
        stop_be       = stop

        for j in range(1, max_bars + 1):
            bi = i + 1 + j
            if bi >= len(bars):
                exit_price  = float(bars.iloc[bi - 1]["Close"])
                exit_reason = "eod"
                break
            b  = bars.iloc[bi]
            bh, bl = float(b["High"]), float(b["Low"])
            if is_long:
                if bl <= stop_be:
                    exit_price  = stop_be
                    exit_reason = "stop"
                    break
                if not partial_taken and bh >= t1:
                    partial_taken = True
                    stop_be       = entry
                if bh >= t2:
                    exit_price  = t2
                    exit_reason = "target"
                    break
            else:
                if bh >= stop_be:
                    exit_price  = stop_be
                    exit_reason = "stop"
                    break
                if not partial_taken and bl <= t1:
                    partial_taken = True
                    stop_be       = entry
                if bl <= t2:
                    exit_price  = t2
                    exit_reason = "target"
                    break

        if exit_price is None:
            exit_price  = float(bars.iloc[min(i + 1 + max_bars, len(bars)-1)]["Close"])
            exit_reason = "time"

        if partial_taken and exit_reason == "target":
            pnl_pts = ((t1 - entry) * partial_pct +
                       (t2 - entry) * (1 - partial_pct)) * (1 if is_long else -1)
        elif partial_taken:
            pnl_pts = ((t1 - entry) * partial_pct +
                       (exit_price - entry) * (1 - partial_pct)) * (1 if is_long else -1)
        elif exit_reason == "stop":
            # Priority 1B: half-stop at -0.5R (50% exits early, 50% at full stop)
            sign    = 1 if is_long else -1
            half_px = entry - 0.5 * risk * sign
            pnl_pts = ((half_px - entry) * sign * 0.5 +
                       (exit_price - entry) * sign * 0.5)
        else:
            pnl_pts = (exit_price - entry) * (1 if is_long else -1)

        pnl_net = pnl_pts * point_value - 2 * commission
        return {
            "strategy":   "ORB",
            "market":     market,
            "direction":  "LONG" if is_long else "SHORT",
            "entry":      round(entry, 2),
            "stop":       round(stop,  2),
            "target":     round(t2,    2),
            "exit":       round(exit_price, 2),
            "exit_reason":exit_reason,
            "risk_pts":   round(risk, 2),
            "pnl_pts":    round(pnl_pts, 2),
            "pnl_net":    round(pnl_net, 2),
            "range_h":    round(rh, 2),
            "range_l":    round(rl, 2),
            "htf_bias":   str(row.get("orb_htf_bias", "")),
        }
    return None


# ============================================================
# PER-DAY DISPLAY
# ============================================================

def fmt_pnl(pnl: float) -> str:
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:,.0f}"


def display_day(date_str: str, orb_results: list, fhb_results: list) -> None:
    has_any = orb_results or fhb_results
    if not has_any:
        return

    print(f"\n  {'─' * 60}")
    print(f"  {date_str}")
    print(f"  {'─' * 60}")

    for t in orb_results:
        direction_sym = "^" if t["direction"] == "LONG" else "v"
        win  = t["pnl_net"] > 0
        icon = "[WIN]" if win else "[LOSS]" if t["pnl_net"] < 0 else "[EVEN]"
        print(f"  ORB {t['market']} {direction_sym}{t['direction']:<5}  "
              f"Range {t['range_l']:.2f}-{t['range_h']:.2f}  "
              f"Entry {t['entry']:.2f}  Stop {t['stop']:.2f}  "
              f"Target {t['target']:.2f}  "
              f"Exit {t['exit']:.2f} ({t['exit_reason']:<6})  "
              f"{fmt_pnl(t['pnl_net'])} {icon}")

    for t in fhb_results:
        direction_sym = "^" if t["direction"] == "LONG" else "v"
        win  = t["pnl_net"] > 0
        icon = "[WIN]" if win else "[LOSS]" if t["pnl_net"] < 0 else "[EVEN]"
        of   = t.get("of_score", 0)
        gls  = t.get("gls_score", 0)
        entry   = t.get("entry",      t.get("entry_price", 0))
        exit_px = t.get("exit_price", t.get("exit", 0))
        risk    = t.get("risk_pts",   0)
        stop    = round(entry - risk, 2) if t["direction"] == "LONG" else round(entry + risk, 2)
        target  = round(entry + 2 * risk, 2) if t["direction"] == "LONG" else round(entry - 2 * risk, 2)
        print(f"  FHB {t['market']} {direction_sym}{t['direction']:<5}  "
              f"Range {t.get('range_l', 0):.2f}-{t.get('range_h', 0):.2f}  "
              f"Entry {entry:.2f}  Stop {stop:.2f}  "
              f"Target {target:.2f}  "
              f"Exit {exit_px:.2f} ({t.get('exit_reason','?'):<6})  "
              f"{fmt_pnl(t['pnl_net'])} {icon}  "
              f"GLS={gls} OF={of}")


# ============================================================
# MAIN REPLAY
# ============================================================

def run_replay(days: int = 60) -> None:
    config = load_config()

    print("\n" + "=" * 65)
    print(f"  AlgoBot -- Signal Replay (last {days} trading days)")
    print(f"  Markets: {', '.join(MARKETS)}")
    print("=" * 65)

    # ── Filters ──────────────────────────────────────────────────────────────
    print("\nLoading filters...")
    econ_cal   = EconCalendar()
    vix_filter = VIXFilter.from_yahoo(start="2023-01-01", end="2026-12-31")
    gls_engine = GreenLightScore(
        full_size_threshold=FHB_GLS_HALF_SCORE,
        half_size_threshold=FHB_GLS_MIN_SCORE,
    )

    # ── ORB data ─────────────────────────────────────────────────────────────
    print("Downloading 5-min data for ORB...")
    intraday_5m = download_all_intraday(markets=MARKETS, interval="5m")

    orb_all: dict[str, dict] = {}   # market -> {date_str -> trade|None}
    htf_bias_cache: dict[str, pd.Series] = {}

    for market in MARKETS:
        df_5m = intraday_5m.get(market, pd.DataFrame())
        if df_5m.empty:
            print(f"  {market}: no 5-min data")
            continue
        bias = get_htf_bias_series(market, config)
        htf_bias_cache[market] = bias
        df_orb = compute_orb_signals(df_5m, market, config, bias)

        orb_all[market] = {}
        nq_vix_skipped = 0
        for day_date in df_orb.index.normalize().unique():
            day_str = str(pd.Timestamp(day_date).date())
            day_df  = df_orb[df_orb.index.normalize() == day_date]

            # P3: NQ ORB -- skip HIGH_VOL and QUIET VIX days (false breakout risk)
            if market == "NQ":
                vix_regime = vix_filter.get_regime(day_date)
                if vix_regime in ("HIGH_VOL", "QUIET"):
                    orb_all[market][day_str] = None
                    nq_vix_skipped += 1
                    continue

            result  = simulate_orb_single_day(day_df, market, config)
            orb_all[market][day_str] = result

        n_trades = sum(1 for v in orb_all[market].values() if v)
        n_days   = len(orb_all[market])
        skip_note = f" (VIX-filtered: {nq_vix_skipped} days)" if market == "NQ" else ""
        print(f"  ORB {market}: {n_trades} signals across {n_days} days{skip_note}")

    # ── FHB data ─────────────────────────────────────────────────────────────
    print("Downloading 1-hour data for FHB...")
    fhb_all: dict[str, list] = {}

    for market in MARKETS:
        df_1h = download_1h_intraday(market)
        if df_1h.empty:
            print(f"  {market}: no 1-hour data")
            continue
        atr    = compute_1h_atr(df_1h, FHB_ATR_PERIOD)
        htf_b, htf_r, *_ = get_htf_data(market, config)

        df_sig = compute_fhb_signals(
            df_1h, market, htf_b, htf_r, config,
            econ_cal=econ_cal, vix_filter=vix_filter, gls_engine=gls_engine,
        )
        trades = simulate_fhb_trades(
            df_sig, atr, market, config,
            use_atr_stop=True, trail_be=True,
            overnight_carry=FHB_OVERNIGHT_CARRY,
            label="replay",
        )
        # Add range info to each trade for display
        for t in trades:
            t_date = str(t.get("date", ""))
            mask   = df_sig.index.normalize() == pd.Timestamp(t_date)
            day_df = df_sig[mask]
            if not day_df.empty:
                t["range_h"] = float(day_df["fhb_range_high"].dropna().iloc[0]) if "fhb_range_high" in day_df.columns else 0
                t["range_l"] = float(day_df["fhb_range_low"].dropna().iloc[0])  if "fhb_range_low"  in day_df.columns else 0

        fhb_all[market] = trades
        n_trades = len(trades)
        n_days   = df_1h.index.normalize().nunique()
        print(f"  FHB {market}: {n_trades} signals across {n_days} days")

    # ── Build unified day index ───────────────────────────────────────────────
    all_dates: set[str] = set()
    for market in MARKETS:
        all_dates.update(orb_all.get(market, {}).keys())
        for t in fhb_all.get(market, []):
            all_dates.add(str(t.get("date", "")))

    sorted_dates = sorted(all_dates)[-days:]  # Last N days

    # ── Per-day display ───────────────────────────────────────────────────────
    print(f"\nSignal log (last {len(sorted_dates)} trading days):\n")

    total_trades = 0
    total_pnl    = 0.0
    wins = losses = 0
    daily_pnls   = []

    for date_str in sorted_dates:
        day_orb = []
        for market in MARKETS:
            t = orb_all.get(market, {}).get(date_str)
            if t:
                day_orb.append(t)

        day_fhb = []
        for market in MARKETS:
            for t in fhb_all.get(market, []):
                if str(t.get("date", "")) == date_str:
                    day_fhb.append(t)

        display_day(date_str, day_orb, day_fhb)

        day_pnl = 0.0
        for t in day_orb + day_fhb:
            pnl = t.get("pnl_net", 0)
            total_pnl += pnl
            day_pnl   += pnl
            total_trades += 1
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1

        if day_orb or day_fhb:
            daily_pnls.append(day_pnl)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print(f"  REPLAY SUMMARY -- Last {len(sorted_dates)} trading days")
    print("=" * 65)
    if total_trades > 0:
        win_rate  = wins / total_trades * 100
        avg_daily = sum(daily_pnls) / len(daily_pnls) if daily_pnls else 0
        # Profit Factor
        gross_win  = sum(t.get("pnl_net", 0)
                         for m in MARKETS
                         for t in (list(orb_all.get(m, {}).values()) + fhb_all.get(m, []))
                         if t and t.get("pnl_net", 0) > 0)
        gross_loss = abs(sum(t.get("pnl_net", 0)
                             for m in MARKETS
                             for t in (list(orb_all.get(m, {}).values()) + fhb_all.get(m, []))
                             if t and t.get("pnl_net", 0) < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")

        print(f"  Total trades   : {total_trades}")
        print(f"  Win rate       : {win_rate:.1f}%  ({wins}W / {losses}L)")
        print(f"  Profit Factor  : {pf:.2f}")
        print(f"  Total P&L      : {fmt_pnl(total_pnl)}")
        print(f"  Avg daily P&L  : {fmt_pnl(avg_daily)}")
        print(f"  Trades/day     : {total_trades / max(len(sorted_dates), 1):.2f}")
        ann = avg_daily * 252
        print(f"  Annualised P&L : {fmt_pnl(ann)}")
    else:
        print("  No trades in this period.")

    print("=" * 65 + "\n")

    # ── Save to dashboard cache ──────────────────────────────────────────────
    _save_to_dashboard(orb_all, fhb_all, sorted_dates)


def _save_to_dashboard(
    orb_all: dict,
    fhb_all: dict,
    sorted_dates: list[str],
) -> None:
    """Write replay results to dashboard/cache/trades.json and bust server cache."""
    cache_dir = PROJECT_ROOT / "dashboard" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Normalise all trades into the dashboard schema
    all_trades: list[dict] = []
    markets = list(orb_all.keys()) or list(fhb_all.keys())

    for date_str in sorted_dates:
        # ORB trades (date stored as dict key, not inside the dict)
        for market in markets:
            t = orb_all.get(market, {}).get(date_str)
            if t:
                all_trades.append({
                    "date":        date_str,
                    "strategy":    "ORB",
                    "market":      t.get("market", market),
                    "direction":   t.get("direction", ""),
                    "entry":       round(float(t.get("entry", 0)), 2),
                    "stop":        round(float(t.get("stop",  0)), 2),
                    "target":      round(float(t.get("target", 0)), 2),
                    "exit":        round(float(t.get("exit", 0)), 2),
                    "exit_reason": t.get("exit_reason", ""),
                    "pnl_net":     round(float(t.get("pnl_net", 0)), 2),
                    "risk_pts":    round(float(t.get("risk_pts", 0)), 2),
                    "range_h":     round(float(t.get("range_h", 0)), 2),
                    "range_l":     round(float(t.get("range_l", 0)), 2),
                    "gls_score":   0,
                    "of_score":    0,
                })

        # FHB trades (date stored inside the dict)
        for market in markets:
            for t in fhb_all.get(market, []):
                if str(t.get("date", "")) == date_str:
                    all_trades.append({
                        "date":        date_str,
                        "strategy":    "FHB",
                        "market":      t.get("market", market),
                        "direction":   t.get("direction", ""),
                        "entry":       round(float(t.get("entry", 0)), 2),
                        "stop":        round(float(t.get("stop",  0)), 2),
                        "target":      round(float(t.get("target", 0)), 2),
                        "exit":        round(float(t.get("exit_price", t.get("exit", 0))), 2),
                        "exit_reason": t.get("exit_reason", ""),
                        "pnl_net":     round(float(t.get("pnl_net", 0)), 2),
                        "risk_pts":    round(float(t.get("risk_pts", 0)), 2),
                        "range_h":     round(float(t.get("range_h", 0)), 2),
                        "range_l":     round(float(t.get("range_l", 0)), 2),
                        "gls_score":   int(t.get("gls_score", 0)),
                        "of_score":    int(t.get("of_score",  0)),
                    })

    # Apply Topstep daily hard stop simulation
    n_before = len(all_trades)
    all_trades = _apply_daily_hard_stop(all_trades, DAILY_HARD_STOP_USD)
    if n_before != len(all_trades):
        print(f"  Daily hard stop (${DAILY_HARD_STOP_USD:,.0f}/day): "
              f"{n_before - len(all_trades)} trades blocked")

    # Daily aggregates
    daily_map: dict[str, dict] = {}
    for t in all_trades:
        d = t["date"]
        if d not in daily_map:
            daily_map[d] = {"date": d, "pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}
        daily_map[d]["pnl"]    += t["pnl_net"]
        daily_map[d]["trades"] += 1
        if t["pnl_net"] > 0:
            daily_map[d]["wins"] += 1
        elif t["pnl_net"] < 0:
            daily_map[d]["losses"] += 1
    for row in daily_map.values():
        row["pnl"] = round(row["pnl"], 2)
    daily_list = sorted(daily_map.values(), key=lambda x: x["date"])

    dates_with_trades = sorted(daily_map.keys())
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period_days":  len(dates_with_trades),
        "period_start": dates_with_trades[0]  if dates_with_trades else "",
        "period_end":   dates_with_trades[-1] if dates_with_trades else "",
        "trades":       sorted(all_trades, key=lambda x: x["date"]),
        "daily":        daily_list,
    }

    out_path = cache_dir / "trades.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"  Dashboard cache updated: {len(all_trades)} trades saved to {out_path}")

    # Tell the running server to reload its in-memory cache
    try:
        import urllib.request
        urllib.request.urlopen(
            urllib.request.Request("http://localhost:8000/api/reload", method="POST"),
            timeout=3,
        )
        print("  Server cache reloaded -- refresh http://localhost:8000")
    except Exception:
        print("  Dashboard server not running -- start it and refresh manually")


# ============================================================
# TODAY PREVIEW
# ============================================================

def run_today_preview(submit: bool = False) -> None:
    """
    Show what signals WOULD fire today based on available data.
    If --submit, also send to IBKR paper account.
    """
    config = load_config()
    today  = datetime.now().strftime("%Y-%m-%d")

    print("\n" + "=" * 65)
    print(f"  AlgoBot -- Today's Signal Preview ({today})")
    print("=" * 65)

    econ_cal   = EconCalendar()
    vix_filter = VIXFilter.from_yahoo(start="2023-01-01", end="2026-12-31")
    gls_engine = GreenLightScore(
        full_size_threshold=FHB_GLS_HALF_SCORE,
        half_size_threshold=FHB_GLS_MIN_SCORE,
    )

    from src.execution.live_signal_engine import LiveSignalEngine, get_front_month_expiry
    print(f"  Front month contract: {get_front_month_expiry()}")

    # FHB preview from 1-hour cached data
    print("\n  FHB signal analysis (1-hour data)...")
    for market in MARKETS:
        df_1h = download_1h_intraday(market)
        if df_1h.empty:
            print(f"  {market}: no data")
            continue
        atr    = compute_1h_atr(df_1h, FHB_ATR_PERIOD)
        htf_b, htf_r, *_ = get_htf_data(market, config)
        df_sig = compute_fhb_signals(
            df_1h, market, htf_b, htf_r, config,
            econ_cal=econ_cal, vix_filter=vix_filter, gls_engine=gls_engine,
        )

        # Show last 5 days of signals
        recent = df_sig[df_sig.index >= (pd.Timestamp.now() - pd.Timedelta(days=7))]
        long_sigs  = recent[recent.get("fhb_long_signal",  False) == True] if "fhb_long_signal"  in recent.columns else pd.DataFrame()
        short_sigs = recent[recent.get("fhb_short_signal", False) == True] if "fhb_short_signal" in recent.columns else pd.DataFrame()

        if long_sigs.empty and short_sigs.empty:
            htf_today = str(htf_b[htf_b.index <= pd.Timestamp(today)].iloc[-1]) if len(htf_b) > 0 else "?"
            print(f"  {market}: No FHB signal in last 7 days | HTF={htf_today}")
            continue

        for direction, sigs in [("LONG", long_sigs), ("SHORT", short_sigs)]:
            if sigs.empty:
                continue
            row = sigs.iloc[-1]
            rh   = float(row.get("fhb_range_high", 0))
            rl   = float(row.get("fhb_range_low",  0))
            gls  = int(row.get("fhb_gls_score",    0))
            act  = str(row.get("fhb_gls_action",   "?"))
            vwap = bool(row.get("fhb_vwap_aligned", True))
            of   = int(row.get("fhb_of_score",     0))
            date_of_sig = str(pd.Timestamp(row.name).date())

            atr_val = float(atr.get(row.name, 0))
            tick    = float(config.get("markets", {}).get(market, {}).get("tick_size", 0.25))
            buf     = tick
            if direction == "LONG":
                entry  = round(rh + buf, 2)
                stop   = round(entry - 0.75 * atr_val, 2)
            else:
                entry  = round(rl - buf, 2)
                stop   = round(entry + 0.75 * atr_val, 2)
            risk   = abs(entry - stop)
            target = round(entry + 2.0 * risk * (1 if direction == "LONG" else -1), 2)

            print(f"\n  FHB {market} {direction} [{date_of_sig}]")
            print(f"    Range   : {rl:.2f} -- {rh:.2f}")
            print(f"    Entry   : {entry:.2f}  Stop: {stop:.2f}  Target: {target:.2f}")
            print(f"    GLS     : {gls} ({act})  VWAP aligned: {vwap}  OF score: {of}")

    print("\n" + "=" * 65)

    if submit:
        print("\n  Submitting latest signal to IBKR paper account...")
        print("  (Requires TWS to be open on port 7497)")
        # Import bridge and submit -- only if TWS is running
        try:
            from src.execution.ibkr_bridge import IBKRBridge
            bridge = IBKRBridge(paper=True)
            if bridge.connect(timeout=5):
                print("  IBKR connected -- signal submission not yet implemented in preview mode")
                print("  Use run_paper_trading.py during market hours for live submission")
                bridge.disconnect()
        except Exception as e:
            print(f"  Could not connect to IBKR: {e}")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AlgoBot Signal Replay")
    parser.add_argument("--days",   type=int,  default=60,
                        help="Number of trading days to replay (default: 60)")
    parser.add_argument("--full",   action="store_true",
                        help="Replay full 730-day dataset")
    parser.add_argument("--today",  action="store_true",
                        help="Show today's signal preview only")
    parser.add_argument("--submit", action="store_true",
                        help="With --today: submit signal to IBKR paper account")
    args = parser.parse_args()

    if args.today or args.submit:
        run_today_preview(submit=args.submit)
    elif args.full:
        run_replay(days=730)
    else:
        run_replay(days=args.days)
