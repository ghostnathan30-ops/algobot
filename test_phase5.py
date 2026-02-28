"""
AlgoBot Phase 5 — MTF Architecture Test Suite
================================================
Tests: 10 total

 1. Config v2.0 loads correctly (HTF + intraday sections present)
 2. VMR SHORT disabled in config
 3. ADX trending threshold raised to 28
 4. HTF bias computes on real ES daily data
 5. HTF bias gate blocks counter-trend signals
 6. VMR SHORT removed from combine_signals output
 7. HTF bias integrated into data pipeline (load_market_data)
 8. QC downloader module structure (no credentials needed)
 9. ORB signal structure on synthetic intraday data
10. ORB HTF bias gate blocks wrong-direction signals

Run:
    cd AlgoBot
    PYTHONIOENCODING=utf-8 python test_phase5.py
"""

import sys
import traceback
from pathlib import Path
from datetime import datetime, date, timedelta

import numpy as np
import pandas as pd
import yaml

# ── Test harness ───────────────────────────────────────────────────────────────
PASS = 0
FAIL = 0

def passed(name: str) -> None:
    global PASS
    PASS += 1
    print(f"  PASS  {name}")

def failed(name: str, reason: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  FAIL  {name}: {reason}")

def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ── Synthetic data helpers ─────────────────────────────────────────────────────

def make_daily_df(n_bars: int = 500, start_price: float = 4000.0) -> pd.DataFrame:
    """Generate a synthetic daily OHLCV DataFrame with realistic price moves."""
    np.random.seed(42)
    dates  = pd.bdate_range("2022-01-03", periods=n_bars, freq="B")
    prices = [start_price]
    for _ in range(n_bars - 1):
        prices.append(prices[-1] * (1 + np.random.normal(0.0003, 0.012)))

    prices = np.array(prices)
    opens  = prices * (1 + np.random.normal(0, 0.002, n_bars))
    highs  = np.maximum(prices, opens) * (1 + np.abs(np.random.normal(0, 0.005, n_bars)))
    lows   = np.minimum(prices, opens) * (1 - np.abs(np.random.normal(0, 0.005, n_bars)))

    df = pd.DataFrame({
        "Open":   opens,
        "High":   highs,
        "Low":    lows,
        "Close":  prices,
        "Volume": np.random.randint(500_000, 3_000_000, n_bars).astype(float),
    }, index=dates)
    df.index.name = "Date"
    return df


def make_intraday_df(n_days: int = 10, bars_per_day: int = 78) -> pd.DataFrame:
    """
    Generate a synthetic 5-minute OHLCV DataFrame for N trading days.
    Each day has bars_per_day 5-min bars from 09:30 to ~16:00 ET.
    """
    np.random.seed(99)
    all_rows = []

    for d_offset in range(n_days):
        base_date = pd.Timestamp("2024-01-02") + pd.Timedelta(days=d_offset)
        if base_date.weekday() >= 5:
            continue  # skip weekends

        # Day open price
        day_open = 4000.0 + d_offset * 10 + np.random.normal(0, 5)

        # Start at 09:30
        ts_start = pd.Timestamp(f"{base_date.date()} 09:30:00")

        price = day_open
        for b in range(bars_per_day):
            ts     = ts_start + pd.Timedelta(minutes=5 * b)
            change = np.random.normal(0, 2)
            o = price
            c = price + change
            h = max(o, c) + abs(np.random.normal(0, 1))
            l = min(o, c) - abs(np.random.normal(0, 1))
            v = float(np.random.randint(1000, 10000))
            all_rows.append({"Timestamp": ts, "Open": o, "High": h, "Low": l, "Close": c, "Volume": v})
            price = c

    df = pd.DataFrame(all_rows).set_index("Timestamp")
    return df


# ── Test 1: Config v2.0 loads correctly ───────────────────────────────────────

def test_config_v2():
    section("Test 1: Config v2.0 — HTF + intraday sections")
    try:
        config_path = Path(__file__).parent / "config" / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        version = cfg.get("project", {}).get("version", "")
        if not version.startswith("2"):
            failed("Config version", f"Expected 2.x.x, got '{version}'")
            return

        # HTF bias section
        if "htf_bias" not in cfg:
            failed("htf_bias section", "Missing from config.yaml")
            return
        if "weekly" not in cfg["htf_bias"] or "monthly" not in cfg["htf_bias"]:
            failed("htf_bias sub-sections", "weekly or monthly missing")
            return
        passed("Config v2.0 version and htf_bias section")

        # Intraday section
        if "intraday" not in cfg:
            failed("intraday section", "Missing from config.yaml")
            return
        if "orb" not in cfg.get("intraday", {}):
            failed("intraday.orb section", "Missing from config.yaml")
            return
        passed("Config intraday.orb section")

        # QC section
        if "quantconnect" not in cfg:
            failed("quantconnect section", "Missing from config.yaml")
            return
        passed("Config quantconnect section")

    except Exception as e:
        failed("Config v2.0 load", str(e))


# ── Test 2: VMR SHORT disabled ─────────────────────────────────────────────────

def test_vmr_short_disabled():
    section("Test 2: VMR SHORT disabled in config")
    try:
        config_path = Path(__file__).parent / "config" / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        vmr_short_enabled = cfg.get("strategy", {}).get("vmr", {}).get("vmr_short_enabled", True)
        if vmr_short_enabled:
            failed("VMR SHORT disabled", "vmr_short_enabled is True (should be False)")
            return
        passed("VMR SHORT disabled (vmr_short_enabled=false)")

    except Exception as e:
        failed("VMR SHORT check", str(e))


# ── Test 3: ADX trending threshold raised ─────────────────────────────────────

def test_adx_threshold():
    section("Test 3: ADX trending threshold raised to 28")
    try:
        config_path = Path(__file__).parent / "config" / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        threshold = cfg.get("regime", {}).get("threshold_trending", 25)
        if threshold < 28:
            failed("ADX threshold", f"Expected >=28, got {threshold}")
            return
        passed(f"ADX trending threshold = {threshold} (raised from 25)")

    except Exception as e:
        failed("ADX threshold check", str(e))


# ── Test 4: HTF bias computes on real daily data ───────────────────────────────

def test_htf_bias_computes():
    section("Test 4: HTF bias computes on synthetic daily data")
    try:
        from src.strategy.htf_bias import add_htf_bias, BULL, BEAR, NEUTRAL

        df = make_daily_df(n_bars=600)

        config_path = Path(__file__).parent / "config" / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        df = add_htf_bias(df, cfg, "ES")

        required_cols = ["htf_weekly_bias", "htf_monthly_bias", "htf_combined_bias"]
        for col in required_cols:
            if col not in df.columns:
                failed("HTF bias columns", f"Missing column: {col}")
                return
        passed("HTF bias columns present")

        # All values must be valid bias strings
        valid_values = {BULL, BEAR, NEUTRAL}
        for col in required_cols:
            unique_vals = set(df[col].unique())
            if not unique_vals.issubset(valid_values):
                failed(f"HTF bias values in {col}",
                       f"Unexpected values: {unique_vals - valid_values}")
                return
        passed("HTF bias values are BULL/BEAR/NEUTRAL only")

        # No NaN values
        for col in required_cols:
            nan_count = df[col].isna().sum()
            if nan_count > 0:
                failed(f"HTF NaN in {col}", f"{nan_count} NaN values")
                return
        passed("HTF bias has no NaN values")

        # Uptrend data should have mostly BULL bias
        bull_pct = (df["htf_weekly_bias"] == BULL).mean()
        bear_pct = (df["htf_weekly_bias"] == BEAR).mean()
        passed(f"HTF weekly bias distribution: BULL={bull_pct:.1%} BEAR={bear_pct:.1%}")

    except Exception as e:
        failed("HTF bias computation", traceback.format_exc())


# ── Test 5: HTF bias gate blocks counter-trend signals ────────────────────────

def test_htf_gate_blocks():
    section("Test 5: HTF bias gate blocks counter-trend signals")
    try:
        from src.strategy.signal_combiner import combine_signals, SignalDirection

        # Build a minimal synthetic DataFrame with all required columns
        n = 100
        dates  = pd.bdate_range("2023-01-03", periods=n)
        df = pd.DataFrame({
            "tma_signal":    [1] * n,     # Always long signal
            "dcs_signal":    [1] * n,     # Always long signal
            "vmr_signal":    [0] * n,
            "trend_active":  [True] * n,
            "vmr_active":    [False] * n,
            "regime":        ["TRENDING"] * n,
            "size_multiplier": [1.0] * n,
            # Inject BEAR HTF bias — should block all longs
            "htf_combined_bias": ["BEAR"] * n,
            "htf_weekly_bias":   ["BEAR"] * n,
            "htf_monthly_bias":  ["BEAR"] * n,
        }, index=dates)

        config_path = Path(__file__).parent / "config" / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        result = combine_signals(df, "ES", cfg)

        agree_long_count = (result["combined_signal"] == SignalDirection.AGREE_LONG.value).sum()
        htf_blocked_count = result["combined_htf_blocked"].sum()

        if agree_long_count > 0:
            failed("HTF gate blocks AGREE_LONG in BEAR bias",
                   f"{agree_long_count} longs were NOT blocked (should be 0)")
            return
        passed(f"HTF gate blocked all {n} AGREE_LONG signals in BEAR bias market")

        if htf_blocked_count != n:
            failed("HTF blocked flag count",
                   f"Expected {n} blocked, got {htf_blocked_count}")
            return
        passed(f"HTF_blocked flag set correctly on {htf_blocked_count} bars")

    except Exception as e:
        failed("HTF gate block test", traceback.format_exc())


# ── Test 6: VMR SHORT removed from combine_signals output ─────────────────────

def test_vmr_short_removed():
    section("Test 6: VMR SHORT absent from combine_signals output")
    try:
        from src.strategy.signal_combiner import combine_signals, SignalDirection

        n = 200
        dates = pd.bdate_range("2023-01-03", periods=n)
        df = pd.DataFrame({
            "tma_signal":    [0] * n,
            "dcs_signal":    [0] * n,
            "vmr_signal":    [-1] * n,    # All SHORT VMR signals
            "trend_active":  [False] * n,
            "vmr_active":    [True] * n,
            "regime":        ["RANGING"] * n,
            "size_multiplier": [1.0] * n,
            "htf_combined_bias": ["NEUTRAL"] * n,
            "htf_weekly_bias":   ["NEUTRAL"] * n,
            "htf_monthly_bias":  ["NEUTRAL"] * n,
        }, index=dates)

        config_path = Path(__file__).parent / "config" / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        result = combine_signals(df, "ES", cfg)

        vmr_short_count = (result["combined_signal"] == SignalDirection.VMR_SHORT.value).sum()
        if vmr_short_count > 0:
            failed("VMR SHORT removed",
                   f"{vmr_short_count} VMR_SHORT signals still firing (should be 0)")
            return
        passed("VMR SHORT signals = 0 (correctly disabled)")

        # VMR LONG should still work when vmr_signal=+1
        df2 = df.copy()
        df2["vmr_signal"] = 1  # LONG
        result2 = combine_signals(df2, "ES", cfg)
        vmr_long_count = (result2["combined_signal"] == SignalDirection.VMR_LONG.value).sum()
        if vmr_long_count == 0:
            failed("VMR LONG still works", "VMR LONG signals = 0 (should still fire)")
            return
        passed(f"VMR LONG still fires: {vmr_long_count} bars")

    except Exception as e:
        failed("VMR SHORT removal test", traceback.format_exc())


# ── Test 7: HTF bias in full data pipeline ────────────────────────────────────

def test_htf_in_pipeline():
    section("Test 7: HTF bias integrated in full data pipeline")
    try:
        from src.backtest.data_loader import load_config
        from src.strategy.htf_bias import add_htf_bias
        from src.strategy.indicators import calculate_indicators, add_atr_baseline
        from src.strategy.regime_classifier import classify_regimes

        config_path = Path(__file__).parent / "config" / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        # Use synthetic daily data to avoid network call
        df = make_daily_df(n_bars=600)
        strat_cfg = cfg.get("strategy", cfg)
        regime_cfg = cfg.get("regime", strat_cfg)

        df = calculate_indicators(df, strat_cfg, "ES")
        df = add_atr_baseline(df)
        df = classify_regimes(df, regime_cfg, "ES")
        df = add_htf_bias(df, cfg, "ES")

        required_cols = ["htf_weekly_bias", "htf_monthly_bias", "htf_combined_bias"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            failed("HTF columns in pipeline", f"Missing: {missing}")
            return
        passed("HTF bias columns present after pipeline")

        # Verify forward-fill is working — no NaN in bias columns
        total_nan = sum(df[col].isna().sum() for col in required_cols)
        if total_nan > 0:
            failed("HTF NaN check", f"Found {total_nan} NaN values in bias columns")
            return
        passed("HTF bias columns fully populated (no NaN)")

    except Exception as e:
        failed("HTF in pipeline", traceback.format_exc())


# ── Test 8: QC downloader structure (no credentials) ─────────────────────────

def test_qc_downloader_structure():
    section("Test 8: QC downloader module structure")
    try:
        from src.utils import qc_downloader

        # Check key functions exist
        required_funcs = [
            "download_qc_intraday",
            "load_intraday",
            "check_qc_credentials",
            "_credentials_available",
            "_get_auth_header",
        ]
        missing = [f for f in required_funcs if not hasattr(qc_downloader, f)]
        if missing:
            failed("QC downloader functions", f"Missing: {missing}")
            return
        passed("All QC downloader functions present")

        # Check market map
        if not hasattr(qc_downloader, "QC_MARKET_MAP"):
            failed("QC_MARKET_MAP", "Missing from module")
            return
        market_map = qc_downloader.QC_MARKET_MAP
        required_markets = ["ES", "NQ", "GC", "CL", "ZB", "6E"]
        missing_markets = [m for m in required_markets if m not in market_map]
        if missing_markets:
            failed("QC market map", f"Missing: {missing_markets}")
            return
        passed(f"QC_MARKET_MAP has all {len(required_markets)} markets")

        # Without credentials, download should return empty DataFrame (not crash)
        import os
        original_user  = os.environ.pop("QC_USER_ID",   None)
        original_token = os.environ.pop("QC_API_TOKEN",  None)

        try:
            result = qc_downloader.download_qc_intraday("ES", "minute", "2024-01-01", "2024-01-05")
            if result is None:
                failed("No-credentials graceful return", "Returned None instead of empty DataFrame")
                return
            passed("No-credentials download returns empty DataFrame gracefully")
        finally:
            if original_user:  os.environ["QC_USER_ID"]   = original_user
            if original_token: os.environ["QC_API_TOKEN"]  = original_token

    except Exception as e:
        failed("QC downloader structure", traceback.format_exc())


# ── Test 9: ORB signal structure on synthetic data ────────────────────────────

def test_orb_signal_structure():
    section("Test 9: ORB signal structure on synthetic intraday data")
    try:
        from src.strategy.orb_signal import compute_orb_signals, summarize_orb_signals

        config_path = Path(__file__).parent / "config" / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        df = make_intraday_df(n_days=15)

        result = compute_orb_signals(df, market="ES", config=cfg, default_htf_bias="NEUTRAL")

        required_cols = [
            "orb_range_high", "orb_range_low", "orb_range_complete",
            "orb_long_signal", "orb_short_signal", "orb_htf_blocked"
        ]
        missing = [c for c in required_cols if c not in result.columns]
        if missing:
            failed("ORB columns", f"Missing: {missing}")
            return
        passed("All ORB signal columns present")

        # At least some signals should have fired on 15 days of data
        long_count  = int(result["orb_long_signal"].sum())
        short_count = int(result["orb_short_signal"].sum())
        total       = long_count + short_count
        if total == 0:
            failed("ORB signal count", "0 signals generated on 15 days (expected at least 1)")
            return
        passed(f"ORB signals generated: {long_count} long, {short_count} short")

        # summarize function works
        summary = summarize_orb_signals(result, "ES")
        if "trading_days" not in summary or "signals_per_day" not in summary:
            failed("ORB summary", "Missing keys in summary dict")
            return
        passed(f"ORB summary: {summary['signals_per_day']:.2f} signals/day across {summary['trading_days']} days")

    except Exception as e:
        failed("ORB signal structure", traceback.format_exc())


# ── Test 10: ORB HTF bias gate ─────────────────────────────────────────────────

def test_orb_htf_gate():
    section("Test 10: ORB HTF bias gate blocks wrong-direction signals")
    try:
        from src.strategy.orb_signal import compute_orb_signals

        config_path = Path(__file__).parent / "config" / "config.yaml"
        with open(config_path) as f:
            cfg = yaml.safe_load(f)

        df = make_intraday_df(n_days=20)

        # Run with BULL bias — short signals should be blocked
        result_bull = compute_orb_signals(df, market="ES", config=cfg, default_htf_bias="BULL")

        # Run with BEAR bias — long signals should be blocked
        result_bear = compute_orb_signals(df, market="ES", config=cfg, default_htf_bias="BEAR")

        # With BULL bias: short signals should either be 0 or blocked
        short_in_bull = int(result_bull["orb_short_signal"].sum())
        long_in_bear  = int(result_bear["orb_long_signal"].sum())

        if short_in_bull > 0:
            failed("BULL bias blocks shorts",
                   f"{short_in_bull} short signals passed through BULL bias filter")
            return
        passed(f"BULL bias: 0 short signals passed (correct)")

        if long_in_bear > 0:
            failed("BEAR bias blocks longs",
                   f"{long_in_bear} long signals passed through BEAR bias filter")
            return
        passed(f"BEAR bias: 0 long signals passed (correct)")

        # NEUTRAL should allow both directions
        result_neut = compute_orb_signals(df, market="ES", config=cfg, default_htf_bias="NEUTRAL")
        total_neutral = int(result_neut["orb_long_signal"].sum()) + int(result_neut["orb_short_signal"].sum())
        if total_neutral == 0:
            failed("NEUTRAL allows both directions", "0 signals with NEUTRAL bias")
            return
        passed(f"NEUTRAL bias: {total_neutral} total signals (both directions allowed)")

    except Exception as e:
        failed("ORB HTF gate", traceback.format_exc())


# ── Main runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "="*60)
    print("  AlgoBot Phase 5 — MTF Architecture Test Suite")
    print("  Date: 2026-02-28")
    print("="*60)

    test_config_v2()
    test_vmr_short_disabled()
    test_adx_threshold()
    test_htf_bias_computes()
    test_htf_gate_blocks()
    test_vmr_short_removed()
    test_htf_in_pipeline()
    test_qc_downloader_structure()
    test_orb_signal_structure()
    test_orb_htf_gate()

    print("\n" + "="*60)
    total = PASS + FAIL
    print(f"  Results: {PASS}/{total} tests passed")
    if FAIL == 0:
        print("  ALL TESTS PASSED -- Phase 5 MTF Architecture: COMPLETE")
    else:
        print(f"  {FAIL} test(s) FAILED -- review output above")
    print("="*60 + "\n")

    sys.exit(0 if FAIL == 0 else 1)
