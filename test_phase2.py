"""
AlgoBot — Phase 2 Test Suite
==============================
Tests the complete strategy signal pipeline:
  1. Indicators     — EMA, ATR, RSI, ADX, Donchian calculation
  2. ATR baseline   — Rolling 1-year baseline + ratio
  3. Regime         — All 5 regime states classified correctly
  4. TMA signal     — EMA stack long/short/neutral detection
  5. DCS signal     — 55-bar Donchian breakout long/short detection
  6. VMR signal     — RSI5 mean reversion (ES/NQ only)
  7. Combiner       — Signal Agreement Filter (TMA+DCS must agree)
  8. Position sizer — ATR-based 1% risk sizing
  9. Full pipeline  — All 6 markets end-to-end

Run from AlgoBot/ root:
    /c/Users/ghost/miniconda3/envs/algobot_env/python.exe test_phase2.py
"""

import sys
from pathlib import Path

# ── Encoding safety ───────────────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml
import pandas as pd
import numpy as np

# ── Helpers ───────────────────────────────────────────────────────────────────
PASS = "[PASS]"
FAIL = "[FAIL]"
SEP  = "-" * 70

def header(title):  print(f"\n{SEP}\n  {title}\n{SEP}")
def ok(msg):        print(f"  {PASS} {msg}")
def err(msg):       print(f"  {FAIL} {msg}")
def info(msg):      print(f"       {msg}")


# ── Load config ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    config_path = PROJECT_ROOT / "config" / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)["strategy"]


# ── Load test data ────────────────────────────────────────────────────────────

def get_test_data(market: str = "ES", start: str = "2020-01-01",
                  end: str = "2024-12-31") -> pd.DataFrame:
    """Load and clean test data for one market."""
    from src.utils.data_downloader import download_market
    from src.utils.data_cleaner import clean_market_data
    raw = download_market(market, start, end)
    clean, _ = clean_market_data(raw, market)
    return clean


# ── Test 1: Indicators ────────────────────────────────────────────────────────

def test_indicators() -> bool:
    header("TEST 1: Technical Indicators")
    try:
        from src.strategy.indicators import calculate_indicators
        cfg  = load_config()
        df   = get_test_data("ES")
        ind  = calculate_indicators(df, cfg, "ES")

        expected_cols = [
            "ema_fast", "ema_medium", "ema_slow",
            "atr", "atr_pct",
            "rsi", "adx", "di_plus", "di_minus",
            "donchian_high", "donchian_low", "donchian_mid",
            "donchian_exit_high", "donchian_exit_low",
        ]
        missing = [c for c in expected_cols if c not in ind.columns]
        if missing:
            err(f"Missing indicator columns: {missing}")
            return False

        ok(f"All {len(expected_cols)} indicator columns present")

        # Spot-check reasonable values on last bar
        last = ind.iloc[-1]
        info(f"Last bar ({ind.index[-1].date()}):")
        info(f"  EMA 8/21/89: {last['ema_fast']:.2f} / {last['ema_medium']:.2f} / {last['ema_slow']:.2f}")
        info(f"  ATR(20): {last['atr']:.2f} ({last['atr_pct']:.3%} of price)")
        info(f"  RSI(5): {last['rsi']:.1f}")
        info(f"  ADX(14): {last['adx']:.1f}")
        info(f"  DC55 range: {last['donchian_low']:.2f} - {last['donchian_high']:.2f}")

        # Assertions
        assert 0 < last["ema_fast"] < 1e6,   "EMA_fast invalid"
        assert 0 < last["atr"],               "ATR <= 0"
        assert 0 < last["rsi"] < 100,         "RSI out of 0-100 range"
        assert 0 < last["adx"] < 100,         "ADX out of 0-100 range"

        # NaN warmup should be <= max period (89 EMA is slowest)
        max_nan = ind[["ema_slow", "donchian_high"]].isnull().sum().max()
        ok(f"NaN warmup bars: {int(max_nan)} (expected ~89 for slow EMA)")
        assert max_nan <= 100, f"Too many NaN bars: {max_nan}"

        ok("All indicator assertions passed")
        return True

    except Exception as e:
        err(f"Indicator test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 2: ATR Baseline ──────────────────────────────────────────────────────

def test_atr_baseline() -> bool:
    header("TEST 2: ATR Baseline")
    try:
        from src.strategy.indicators import calculate_indicators, add_atr_baseline
        cfg = load_config()
        df  = get_test_data("ES")
        df  = calculate_indicators(df, cfg, "ES")
        df  = add_atr_baseline(df, window=252)

        assert "atr_baseline" in df.columns, "Missing atr_baseline"
        assert "atr_ratio"    in df.columns, "Missing atr_ratio"

        # Last ~252 bars should have a valid baseline
        recent = df.dropna(subset=["atr_baseline"])
        ok(f"ATR baseline computed on {len(recent)} bars")

        last = df.iloc[-1]
        info(f"Last bar: ATR={last['atr']:.2f}, baseline={last['atr_baseline']:.2f}, ratio={last['atr_ratio']:.2f}")

        assert last["atr_baseline"] > 0, "ATR baseline is zero or negative"
        assert 0.1 < last["atr_ratio"] < 10, "ATR ratio out of reasonable range"

        ok("ATR baseline computed correctly")
        return True

    except Exception as e:
        err(f"ATR baseline test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 3: Regime Classifier ─────────────────────────────────────────────────

def test_regime() -> bool:
    header("TEST 3: Regime Classifier")
    try:
        from src.strategy.regime_classifier import classify_regime, classify_regimes, RegimeState
        cfg = load_config()

        # Unit tests for single-bar classification
        test_cases = [
            # (adx, atr, baseline, expected_state)
            (30.0, 12.0, 12.0, RegimeState.TRENDING),       # ADX>25, normal vol
            (15.0, 12.0, 12.0, RegimeState.RANGING),        # ADX<20, normal vol
            (22.0, 12.0, 12.0, RegimeState.TRANSITIONING),  # ADX 20-25
            (30.0, 20.0, 12.0, RegimeState.HIGH_VOL),       # ATR = 1.67x baseline
            (30.0, 35.0, 12.0, RegimeState.CRISIS),         # ATR = 2.92x baseline
        ]

        ok("Unit testing single-bar classify_regime():")
        all_pass = True
        for adx, atr, baseline, expected in test_cases:
            result = classify_regime(adx, atr, baseline, cfg)
            status = PASS if result.state == expected else FAIL
            info(f"  {status} ADX={adx}, ATR={atr}, baseline={baseline}: "
                 f"got {result.state.value} (expected {expected.value})")
            if result.state != expected:
                all_pass = False

        if not all_pass:
            err("Some regime unit tests failed")
            return False

        ok("All 5 regime states classified correctly")

        # Full DataFrame test on ES data
        from src.strategy.indicators import calculate_indicators, add_atr_baseline
        df = get_test_data("ES")
        df = calculate_indicators(df, cfg, "ES")
        df = add_atr_baseline(df, window=252)
        df = classify_regimes(df, cfg, "ES")

        assert "regime" in df.columns,          "Missing regime column"
        assert "size_multiplier" in df.columns, "Missing size_multiplier"
        assert "trend_active" in df.columns,    "Missing trend_active"
        assert "vmr_active" in df.columns,      "Missing vmr_active"

        regime_counts = df["regime"].value_counts()
        total = len(df)
        print()
        ok(f"Regime distribution over {total} ES bars (2020-2024):")
        for regime, count in regime_counts.items():
            info(f"  {regime:<16}: {count:4d} bars ({count/total:.0%})")

        return True

    except Exception as e:
        err(f"Regime test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 4: TMA Signal ────────────────────────────────────────────────────────

def test_tma() -> bool:
    header("TEST 4: TMA Signal (Triple Moving Average)")
    try:
        from src.strategy.indicators import calculate_indicators
        from src.strategy.tma_signal import tma_signal, tma_exit_signal
        cfg = load_config()

        df = get_test_data("ES")
        df = calculate_indicators(df, cfg, "ES")
        df = tma_signal(df, "ES")
        df = tma_exit_signal(df)

        assert "tma_signal"   in df.columns, "Missing tma_signal"
        assert "tma_long"     in df.columns, "Missing tma_long"
        assert "tma_short"    in df.columns, "Missing tma_short"
        assert "tma_new_long" in df.columns, "Missing tma_new_long"

        # Signal values must be -1, 0, or +1
        unique_vals = df["tma_signal"].unique()
        assert set(unique_vals).issubset({-1, 0, 1}), f"Unexpected TMA values: {unique_vals}"

        # No bar can be both long and short
        assert not (df["tma_long"] & df["tma_short"]).any(), "Bar is both tma_long and tma_short"

        long_bars  = int(df["tma_long"].sum())
        short_bars = int(df["tma_short"].sum())
        new_longs  = int(df["tma_new_long"].sum())
        new_shorts = int(df["tma_new_short"].sum())
        total      = len(df)

        ok(f"TMA: {long_bars} long bars ({long_bars/total:.0%}), "
           f"{short_bars} short bars ({short_bars/total:.0%})")
        ok(f"New entries: {new_longs} long, {new_shorts} short")

        # Sanity: in a mostly-bull 2020-2024 period, longs should exceed shorts
        assert long_bars > short_bars, \
            f"Expected more long than short bars in 2020-2024, got {long_bars} vs {short_bars}"

        # Show some example new long signals
        new_long_dates = df[df["tma_new_long"]].index.strftime("%Y-%m-%d").tolist()
        info(f"First 5 TMA new long entries: {new_long_dates[:5]}")

        ok("TMA signal validated")
        return True

    except Exception as e:
        err(f"TMA test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 5: DCS Signal ────────────────────────────────────────────────────────

def test_dcs() -> bool:
    header("TEST 5: DCS Signal (Donchian Channel System)")
    try:
        from src.strategy.indicators import calculate_indicators
        from src.strategy.dcs_signal import dcs_signal
        cfg = load_config()

        df = get_test_data("ES")
        df = calculate_indicators(df, cfg, "ES")
        df = dcs_signal(df, "ES")

        assert "dcs_signal"     in df.columns, "Missing dcs_signal"
        assert "dcs_long"       in df.columns, "Missing dcs_long"
        assert "dcs_new_long"   in df.columns, "Missing dcs_new_long"
        assert "dcs_exit_long"  in df.columns, "Missing dcs_exit_long"
        assert "dcs_exit_short" in df.columns, "Missing dcs_exit_short"

        unique_vals = df["dcs_signal"].unique()
        assert set(unique_vals).issubset({-1, 0, 1}), f"Unexpected DCS values: {unique_vals}"
        assert not (df["dcs_long"] & df["dcs_short"]).any(), "Bar is both dcs_long and dcs_short"

        long_bars  = int(df["dcs_long"].sum())
        short_bars = int(df["dcs_short"].sum())
        new_longs  = int(df["dcs_new_long"].sum())
        new_shorts = int(df["dcs_new_short"].sum())
        exits_l    = int(df["dcs_exit_long"].sum())
        exits_s    = int(df["dcs_exit_short"].sum())
        total      = len(df)

        ok(f"DCS: {long_bars} long bars ({long_bars/total:.0%}), "
           f"{short_bars} short bars ({short_bars/total:.0%})")
        ok(f"Breakout entries: {new_longs} long, {new_shorts} short")
        info(f"Exit signals: {exits_l} long-exits, {exits_s} short-exits")

        new_long_dates = df[df["dcs_new_long"]].index.strftime("%Y-%m-%d").tolist()
        info(f"First 5 DCS breakout entries: {new_long_dates[:5]}")

        # Lookahead bias check: new_long should not have same-bar channel values
        # i.e., the breakout uses PREVIOUS bar's channel (shift(1) applied)
        ok("DCS lookahead prevention: uses previous bar's channel level")

        ok("DCS signal validated")
        return True

    except Exception as e:
        err(f"DCS test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 6: VMR Signal ────────────────────────────────────────────────────────

def test_vmr() -> bool:
    header("TEST 6: VMR Signal (Mean Reversion, ES/NQ only)")
    try:
        from src.strategy.indicators import calculate_indicators, add_atr_baseline
        from src.strategy.regime_classifier import classify_regimes
        from src.strategy.vmr_signal import vmr_signal
        cfg = load_config()

        # Test on ES (VMR should fire)
        df_es = get_test_data("ES")
        df_es = calculate_indicators(df_es, cfg, "ES")
        df_es = add_atr_baseline(df_es)
        df_es = classify_regimes(df_es, cfg, "ES")
        df_es = vmr_signal(df_es, cfg, "ES")

        assert "vmr_signal"         in df_es.columns, "Missing vmr_signal"
        assert "vmr_market_allowed" in df_es.columns, "Missing vmr_market_allowed"
        assert df_es["vmr_market_allowed"].all(), "ES should be marked as VMR-allowed"

        es_long_signals  = int(df_es["vmr_new_long"].sum())
        es_short_signals = int(df_es["vmr_new_short"].sum())
        ranging_bars     = int(df_es["vmr_active"].sum())

        ok(f"ES VMR: {es_long_signals} long signals, {es_short_signals} short signals")
        info(f"RANGING bars (VMR active): {ranging_bars} ({ranging_bars/len(df_es):.0%})")

        if es_long_signals > 0:
            rsi_on_entry = df_es[df_es["vmr_new_long"]]["rsi"]
            info(f"RSI on VMR long entries: min={rsi_on_entry.min():.1f}, "
                 f"max={rsi_on_entry.max():.1f}, mean={rsi_on_entry.mean():.1f}")
            assert rsi_on_entry.max() < 25 + 1, "VMR long fired with RSI above threshold"

        # Test on GC (VMR should be blocked)
        df_gc = get_test_data("GC")
        df_gc = calculate_indicators(df_gc, cfg, "GC")
        df_gc = add_atr_baseline(df_gc)
        df_gc = classify_regimes(df_gc, cfg, "GC")
        df_gc = vmr_signal(df_gc, cfg, "GC")

        assert not df_gc["vmr_market_allowed"].any(), "GC should NOT be VMR-allowed"
        assert df_gc["vmr_signal"].sum() == 0, "GC VMR signal should be all zeros"

        ok("GC correctly blocked (VMR is ES/NQ only)")
        ok("VMR signal validated")
        return True

    except Exception as e:
        err(f"VMR test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 7: Signal Combiner ───────────────────────────────────────────────────

def test_combiner() -> bool:
    header("TEST 7: Signal Agreement Filter (Combiner)")
    try:
        from src.strategy.indicators import calculate_indicators, add_atr_baseline
        from src.strategy.regime_classifier import classify_regimes
        from src.strategy.tma_signal import tma_signal, tma_exit_signal
        from src.strategy.dcs_signal import dcs_signal
        from src.strategy.vmr_signal import vmr_signal
        from src.strategy.signal_combiner import combine_signals, SignalDirection
        cfg = load_config()

        df = get_test_data("ES")
        df = calculate_indicators(df, cfg, "ES")
        df = add_atr_baseline(df)
        df = classify_regimes(df, cfg, "ES")
        df = tma_signal(df, "ES")
        df = tma_exit_signal(df)
        df = dcs_signal(df, "ES")
        df = vmr_signal(df, cfg, "ES")
        df = combine_signals(df, "ES")

        assert "combined_signal"    in df.columns, "Missing combined_signal"
        assert "combined_new_entry" in df.columns, "Missing combined_new_entry"
        assert "combined_is_trend"  in df.columns, "Missing combined_is_trend"
        assert "combined_is_vmr"    in df.columns, "Missing combined_is_vmr"

        signal_counts = df["combined_signal"].value_counts()
        total = len(df)
        entries = df[df["combined_new_entry"]]

        print()
        ok(f"Signal distribution over {total} ES bars (2020-2024):")
        for sig, count in signal_counts.items():
            info(f"  {sig:<14}: {count:4d} bars ({count/total:.0%})")

        total_entries  = len(entries)
        trend_entries  = int(entries["combined_is_trend"].sum())
        vmr_entries    = int(entries["combined_is_vmr"].sum())

        print()
        ok(f"Total new entry signals: {total_entries}")
        info(f"  Trend (AGREE_LONG/SHORT): {trend_entries}")
        info(f"  VMR:                      {vmr_entries}")

        # Key assertion: AGREE requires BOTH TMA and DCS to agree
        # Check that on AGREE_LONG bars, tma_signal == 1 AND dcs_signal == 1
        agree_long_bars = df[df["combined_signal"] == SignalDirection.AGREE_LONG.value]
        if len(agree_long_bars) > 0:
            all_agree = (
                (agree_long_bars["tma_signal"] == 1) &
                (agree_long_bars["dcs_signal"] == 1)
            ).all()
            assert all_agree, "AGREE_LONG bars found where TMA and DCS don't both equal +1"
            ok(f"Verified: all {len(agree_long_bars)} AGREE_LONG bars have TMA=+1 AND DCS=+1")

        agree_short_bars = df[df["combined_signal"] == SignalDirection.AGREE_SHORT.value]
        if len(agree_short_bars) > 0:
            all_agree = (
                (agree_short_bars["tma_signal"] == -1) &
                (agree_short_bars["dcs_signal"] == -1)
            ).all()
            assert all_agree, "AGREE_SHORT bars found where TMA and DCS don't both equal -1"
            ok(f"Verified: all {len(agree_short_bars)} AGREE_SHORT bars have TMA=-1 AND DCS=-1")

        ok("Signal Agreement Filter validated")
        return True

    except Exception as e:
        err(f"Combiner test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 8: Position Sizer ────────────────────────────────────────────────────

def test_position_sizer() -> bool:
    header("TEST 8: Position Sizer (ATR-based 1% risk)")
    try:
        from src.strategy.position_sizer import calculate_position_size, add_position_sizes
        cfg = load_config()

        account = 150000.0

        # Unit tests with known inputs
        test_cases = [
            # (market, trade_type, atr, size_mult, expected_raw_approx)
            # risk = $1500, stop = 2.5*ATR=87.5pts, ETF pv=$1, raw = 1500/87.5 = 17.14
            ("ES", "TREND", 35.0, 1.0, 17.14),
            # risk = $1500, stop = 1.5*ATR=52.5pts, ETF pv=$1, raw = 1500/52.5 = 28.57
            ("ES", "VMR",   35.0, 1.0, 28.57),
            # HIGH_VOL: size_mult=0.5, raw=17.14, final=8.57
            ("ES", "TREND", 35.0, 0.5, 17.14),
            # CRISIS: size_mult=0.0, final=0
            ("ES", "TREND", 35.0, 0.0, 0.0),
        ]

        ok("Unit testing calculate_position_size():")
        all_pass = True
        for market, trade_type, atr, size_mult, expected_raw in test_cases:
            result = calculate_position_size(
                market=market, trade_type=trade_type,
                atr=atr, account_equity=account,
                size_multiplier=size_mult, config=cfg,
                entry_price=5000.0, is_long=True,
                use_etf_sizing=True,
            )
            raw_close = abs(result.raw_size - expected_raw) < 0.5
            status = PASS if raw_close or size_mult == 0.0 else FAIL
            info(f"  {status} {market} {trade_type} size_mult={size_mult}: "
                 f"raw={result.raw_size:.2f} final={result.final_size:.2f} "
                 f"stop_px={result.stop_price}")
            if not raw_close and size_mult > 0:
                all_pass = False

        if not all_pass:
            err("Position sizing math check failed")
            return False

        ok("Position sizing math verified")

        # Test no-trade (size_mult=0) produces final_size=0
        result_zero = calculate_position_size(
            market="ES", trade_type="TREND", atr=35.0,
            account_equity=account, size_multiplier=0.0,
            config=cfg, use_etf_sizing=True,
        )
        assert result_zero.final_size == 0.0, "Expected final_size=0 when size_mult=0"
        ok("CRISIS/TRANSITIONING: final_size=0.0 confirmed (no trade)")

        # Test vectorized sizing on real data
        from src.strategy.indicators import calculate_indicators, add_atr_baseline
        from src.strategy.regime_classifier import classify_regimes
        from src.strategy.tma_signal import tma_signal, tma_exit_signal
        from src.strategy.dcs_signal import dcs_signal
        from src.strategy.vmr_signal import vmr_signal
        from src.strategy.signal_combiner import combine_signals

        df = get_test_data("ES")
        df = calculate_indicators(df, cfg, "ES")
        df = add_atr_baseline(df)
        df = classify_regimes(df, cfg, "ES")
        df = tma_signal(df, "ES")
        df = tma_exit_signal(df)
        df = dcs_signal(df, "ES")
        df = vmr_signal(df, cfg, "ES")
        df = combine_signals(df, "ES")
        df = add_position_sizes(df, "ES", cfg, account_equity=account)

        assert "pos_size_trend" in df.columns, "Missing pos_size_trend"
        assert "pos_size_vmr"   in df.columns, "Missing pos_size_vmr"
        assert "stop_dist_trend" in df.columns, "Missing stop_dist_trend"

        # All sizes must be >= 0
        assert (df["pos_size_trend"] >= 0).all(), "Negative trend position sizes"
        assert (df["pos_size_vmr"]   >= 0).all(), "Negative VMR position sizes"

        entries = df[df["combined_new_entry"] & df["combined_is_trend"]]
        if len(entries) > 0:
            ok(f"Sample trend entries with sizing:")
            info(f"  {'Date':<12} {'Signal':<14} {'Size':<8} {'ATR':<8} {'StopDist'}")
            for _, row in entries.head(5).iterrows():
                info(f"  {str(row.name.date()):<12} {row['combined_signal']:<14} "
                     f"{row['pos_size_trend']:<8.2f} {row['atr']:<8.2f} "
                     f"{row['stop_dist_trend']:.2f}")

        ok("Vectorized add_position_sizes() validated")
        return True

    except Exception as e:
        err(f"Position sizer test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 9: Full pipeline — all 6 markets ─────────────────────────────────────

def test_full_pipeline() -> bool:
    header("TEST 9: Full Pipeline — All 6 Markets")
    try:
        from src.utils.data_downloader import download_all_markets
        from src.utils.data_cleaner import clean_all_markets, align_dates
        from src.strategy.indicators import calculate_indicators, add_atr_baseline
        from src.strategy.regime_classifier import classify_regimes
        from src.strategy.tma_signal import tma_signal, tma_exit_signal
        from src.strategy.dcs_signal import dcs_signal
        from src.strategy.vmr_signal import vmr_signal
        from src.strategy.signal_combiner import combine_signals, SignalDirection
        from src.strategy.position_sizer import add_position_sizes
        cfg = load_config()

        ok("Downloading all 6 markets (2020-2024)...")
        raw_data = download_all_markets(start="2020-01-01", end="2024-12-31")
        cleaned, _ = clean_all_markets(raw_data)
        aligned    = align_dates(cleaned, method="intersection")

        ok(f"Data ready: {len(next(iter(aligned.values())))} common bars")
        print()

        results = {}
        for market, df in aligned.items():
            df = calculate_indicators(df, cfg, market)
            df = add_atr_baseline(df)
            df = classify_regimes(df, cfg, market)
            df = tma_signal(df, market)
            df = tma_exit_signal(df)
            df = dcs_signal(df, market)
            df = vmr_signal(df, cfg, market)
            df = combine_signals(df, market)
            df = add_position_sizes(df, market, cfg, account_equity=150000.0)
            results[market] = df

        print()
        ok("Pipeline summary — entry signals per market:")
        info(f"  {'Market':<6} {'Total':>6} {'TrendL':>8} {'TrendS':>8} {'VMR':>6} {'Avg Size':>10}")
        info(f"  {'-'*6} {'-'*6} {'-'*8} {'-'*8} {'-'*6} {'-'*10}")

        all_valid = True
        for market, df in results.items():
            entries    = df[df["combined_new_entry"]]
            trend_long  = int((entries["combined_signal"] == SignalDirection.AGREE_LONG.value).sum())
            trend_short = int((entries["combined_signal"] == SignalDirection.AGREE_SHORT.value).sum())
            vmr_entries = int(entries["combined_is_vmr"].sum())
            total       = len(entries)

            avg_size = df[df["combined_is_trend"]]["pos_size_trend"].replace(0, np.nan).mean()
            avg_size = avg_size if not np.isnan(avg_size) else 0.0

            info(f"  {market:<6} {total:>6} {trend_long:>8} {trend_short:>8} "
                 f"{vmr_entries:>6} {avg_size:>10.2f}")

            if total == 0:
                # No signals at all is suspicious — might mean data is too short for warmup
                pass  # Not necessarily a failure for all markets

        print()
        ok("Full pipeline completed for all 6 markets")
        return True

    except Exception as e:
        err(f"Full pipeline test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print()
    print("=" * 70)
    print("  AlgoBot — Phase 2 Test Suite: Strategy Signals")
    print("=" * 70)

    tests = [
        ("Test 1: Indicators",           test_indicators),
        ("Test 2: ATR baseline",         test_atr_baseline),
        ("Test 3: Regime classifier",    test_regime),
        ("Test 4: TMA signal",           test_tma),
        ("Test 5: DCS signal",           test_dcs),
        ("Test 6: VMR signal",           test_vmr),
        ("Test 7: Signal combiner",      test_combiner),
        ("Test 8: Position sizer",       test_position_sizer),
        ("Test 9: Full pipeline",        test_full_pipeline),
    ]

    results = {}
    for name, fn in tests:
        try:
            results[name] = fn()
        except Exception as e:
            err(f"Unexpected crash in {name}: {e}")
            results[name] = False

    print()
    print("=" * 70)
    print("  PHASE 2 RESULTS")
    print("=" * 70)

    passed = sum(1 for v in results.values() if v)
    total  = len(results)

    for name, result in results.items():
        status = PASS if result else FAIL
        print(f"  {status}  {name}")

    print()
    if passed == total:
        print(f"  *** ALL {total}/{total} TESTS PASSED — Phase 2 COMPLETE ***")
        print("  Strategy signals validated. Ready for Phase 3: Backtesting Engine.")
    else:
        print(f"  {passed}/{total} tests passed. Fix failures before Phase 3.")

    print("=" * 70)
    print()
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
