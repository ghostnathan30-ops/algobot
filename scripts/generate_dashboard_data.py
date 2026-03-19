"""
AlgoBot -- Dashboard Data Generator
=====================================
Script:  scripts/generate_dashboard_data.py
Purpose: Run full ORB + FHB + GC_REV + 6E_LON replay and export structured
         trade data to dashboard/cache/ as JSON for the performance dashboard.

Steps:
  [1/6] Load filters (EconCalendar, VIX, GLS)
  [2/6] ORB signals (ES, NQ — 5-min bars)
  [3/6] FHB signals (ES, NQ — 1-hour bars)
  [4/6] GC Mean Reversion (Gold — 1-hour bars, inverted FHB)
  [5/6] 6E London Open Breakout (Euro FX — 1-hour overnight bars)
  [6/6] Aggregate, sort, save JSON

Run once (takes ~90s), then start the dashboard server:
    conda run -n algobot_env python scripts/generate_dashboard_data.py
    conda run -n algobot_env uvicorn dashboard.server:app --reload
    Open: http://localhost:8000
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from run_fhb_backtest import (
    download_1h_intraday, compute_fhb_signals, simulate_fhb_trades,
    compute_1h_atr, get_htf_data,
    FHB_ATR_PERIOD, FHB_GLS_HALF_SCORE, FHB_GLS_MIN_SCORE, FHB_OVERNIGHT_CARRY,
)
from run_signal_replay import (
    simulate_orb_single_day, get_htf_bias_series, load_config, MARKETS,
)
from run_gc_backtest import (
    download_1h_intraday as download_1h_gc,
    get_htf_data as get_htf_data_gc,
)
from run_6e_backtest import (
    download_1h_overnight as download_1h_6e,
    get_htf_data as get_htf_data_6e,
)
from src.utils.yf_intraday import download_all_intraday
from src.strategy.orb_signal import compute_orb_signals
from src.strategy.gc_signal import compute_gc_signals, simulate_gc_trades
from src.strategy.london_open_signal import compute_london_signals, simulate_london_trades
from src.utils.econ_calendar import EconCalendar
from src.utils.vix_filter import VIXFilter
from src.utils.trade_readiness import GreenLightScore

CACHE_DIR = PROJECT_ROOT / "dashboard" / "cache"
DAYS = 730


def main():
    print("\n" + "=" * 65)
    print("  AlgoBot -- Dashboard Data Generator")
    print("=" * 65)
    print(f"  Period: last {DAYS} trading days")
    print(f"  Output: {CACHE_DIR}")
    print()

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    config = load_config()

    # ── Filters ──────────────────────────────────────────────────────────────
    print("[1/6] Loading filters...")
    econ_cal   = EconCalendar()
    vix_filter = VIXFilter.from_yahoo(start="2019-01-01", end="2026-12-31")
    gls_engine = GreenLightScore(
        full_size_threshold=FHB_GLS_HALF_SCORE,
        half_size_threshold=FHB_GLS_MIN_SCORE,
    )

    all_trades: list[dict] = []

    # ── ORB ──────────────────────────────────────────────────────────────────
    print("[2/6] Running ORB signals (5-min bars)...")
    intraday_5m = download_all_intraday(markets=MARKETS, interval="5m")

    for market in MARKETS:
        df_5m = intraday_5m.get(market, pd.DataFrame())
        if df_5m.empty:
            continue
        bias   = get_htf_bias_series(market, config)
        df_orb = compute_orb_signals(df_5m, market, config, bias)

        for day_date in df_orb.index.normalize().unique():
            day_str = str(pd.Timestamp(day_date).date())
            day_df  = df_orb[df_orb.index.normalize() == day_date]
            t = simulate_orb_single_day(day_df, market, config)
            if t:
                all_trades.append({
                    "date":        day_str,
                    "strategy":    "ORB",
                    "market":      market,
                    "direction":   t["direction"],
                    "entry":       t["entry"],
                    "stop":        t["stop"],
                    "target":      t["target"],
                    "exit":        t["exit"],
                    "exit_reason": t["exit_reason"],
                    "pnl_net":     t["pnl_net"],
                    "risk_pts":    t["risk_pts"],
                    "range_h":     t.get("range_h", 0),
                    "range_l":     t.get("range_l", 0),
                    "gls_score":   0,
                    "of_score":    0,
                })
        print(f"  ORB {market}: done")

    # ── FHB ──────────────────────────────────────────────────────────────────
    print("[3/6] Running FHB signals (1-hour bars)...")
    for market in MARKETS:
        df_1h = download_1h_intraday(market)
        if df_1h.empty:
            continue
        atr      = compute_1h_atr(df_1h, FHB_ATR_PERIOD)
        htf_b, htf_r, fast_bias = get_htf_data(market, config)
        df_sig   = compute_fhb_signals(
            df_1h, market, htf_b, htf_r, config,
            econ_cal=econ_cal, vix_filter=vix_filter, gls_engine=gls_engine,
            fast_bias_series=fast_bias,
        )
        trades = simulate_fhb_trades(
            df_sig, atr, market, config,
            use_atr_stop=True, trail_be=True,
            overnight_carry=FHB_OVERNIGHT_CARRY,
            label="dashboard",
        )
        # Attach range info
        for t in trades:
            t_date = str(t.get("date", ""))
            mask   = df_sig.index.normalize() == pd.Timestamp(t_date)
            day_df = df_sig[mask]
            rh = rl = 0.0
            if not day_df.empty:
                rh = float(day_df["fhb_range_high"].dropna().iloc[0]) if "fhb_range_high" in day_df.columns and not day_df["fhb_range_high"].dropna().empty else 0
                rl = float(day_df["fhb_range_low"].dropna().iloc[0])  if "fhb_range_low"  in day_df.columns and not day_df["fhb_range_low"].dropna().empty  else 0

            entry = t.get("entry", t.get("entry_price", 0))
            risk  = t.get("risk_pts", 0)
            stop  = round(entry - risk, 2) if t["direction"] == "LONG" else round(entry + risk, 2)
            tgt   = round(entry + 2 * risk, 2) if t["direction"] == "LONG" else round(entry - 2 * risk, 2)

            all_trades.append({
                "date":        t_date,
                "strategy":    "FHB",
                "market":      market,
                "direction":   t["direction"],
                "entry":       round(entry, 2),
                "stop":        stop,
                "target":      tgt,
                "exit":        round(t.get("exit_price", t.get("exit", 0)), 2),
                "exit_reason": t.get("exit_reason", ""),
                "pnl_net":     round(t.get("pnl_net", 0), 2),
                "risk_pts":    round(risk, 2),
                "range_h":     round(rh, 2),
                "range_l":     round(rl, 2),
                "gls_score":   int(t.get("gls_score", 0)),
                "of_score":    int(t.get("of_score", 0)),
            })
        print(f"  FHB {market}: {len(trades)} trades")

    # ── GC Mean Reversion ────────────────────────────────────────────────────
    print("[4/6] Running GC Mean Reversion signals (1-hour bars)...")
    try:
        df_gc = download_1h_gc("GC")
        if not df_gc.empty:
            htf_gc, regime_gc = get_htf_data_gc("GC", config)
            df_gc_sig = compute_gc_signals(
                df_gc, "GC", htf_gc, regime_gc, config,
                econ_cal=econ_cal, vix_filter=vix_filter,
            )
            gc_trades = simulate_gc_trades(df_gc_sig, "GC", config)
            for t in gc_trades:
                all_trades.append({
                    "date":        t["date"],
                    "strategy":    "GC_REV",
                    "market":      "GC",
                    "direction":   t["direction"],
                    "entry":       round(t.get("entry", 0), 2),
                    "stop":        round(t.get("stop",  0), 2),
                    "target":      round(t.get("target", 0), 2),
                    "exit":        round(t.get("exit_price", t.get("exit", 0)), 2),
                    "exit_reason": t.get("exit_reason", ""),
                    "pnl_net":     round(t.get("pnl_net", 0), 2),
                    "risk_pts":    round(t.get("risk_pts", 0), 4),
                    "range_h":     0.0,
                    "range_l":     0.0,
                    "gls_score":   0,
                    "of_score":    0,
                })
            print(f"  GC_REV: {len(gc_trades)} trades")
        else:
            print("  GC: No data — skipping")
    except Exception as e:
        print(f"  GC: Error — {e}")

    # ── 6E London Open — PARKED (PF=0.79, losing -$3,866, drags system PF) ──
    print("[5/6] 6E London Open: PARKED — no edge in current EUR/USD ranging regime (skipping)")

    # ── Sort and slice to last DAYS ──────────────────────────────────────────
    print("[6/6] Computing daily aggregates...")
    all_trades.sort(key=lambda x: x["date"])
    unique_dates = sorted({t["date"] for t in all_trades})
    cutoff_dates = set(unique_dates[-DAYS:])
    all_trades = [t for t in all_trades if t["date"] in cutoff_dates]

    # ── Daily aggregates ─────────────────────────────────────────────────────
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

    daily_list = sorted(daily_map.values(), key=lambda x: x["date"])
    for row in daily_list:
        row["pnl"] = round(row["pnl"], 2)

    # ── Save ─────────────────────────────────────────────────────────────────
    payload = {
        "generated_at":  datetime.now().isoformat(timespec="seconds"),
        "period_days":   len(cutoff_dates),
        "period_start":  min(cutoff_dates) if cutoff_dates else "",
        "period_end":    max(cutoff_dates) if cutoff_dates else "",
        "trades":        all_trades,
        "daily":         daily_list,
    }

    out_path = CACHE_DIR / "trades.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ── Summary ──────────────────────────────────────────────────────────────
    n      = len(all_trades)
    wins   = sum(1 for t in all_trades if t["pnl_net"] > 0)
    losses = sum(1 for t in all_trades if t["pnl_net"] < 0)
    total  = sum(t["pnl_net"] for t in all_trades)
    gw     = sum(t["pnl_net"] for t in all_trades if t["pnl_net"] > 0)
    gl     = abs(sum(t["pnl_net"] for t in all_trades if t["pnl_net"] < 0))
    pf     = gw / gl if gl > 0 else float("inf")

    print(f"\n  Trades       : {n}")
    print(f"  Win rate     : {wins/n*100:.1f}%  ({wins}W / {losses}L)")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Total P&L    : ${total:,.0f}")
    print(f"  Days covered : {len(daily_list)}")
    print(f"\n  Saved to: {out_path}")
    print("\n  Now run:")
    print("  conda run -n algobot_env uvicorn dashboard.server:app --reload")
    print("  Open: http://localhost:8000")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
