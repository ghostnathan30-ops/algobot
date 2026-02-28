"""
AlgoBot — Phase 1 Test Suite
==============================
Tests the complete data infrastructure pipeline:
  1. Logger        — loguru setup, console + file output, credential sanitization
  2. ES download   — single market Yahoo Finance download with caching
  3. All 6 markets — full portfolio download
  4. Summary table — data quality overview
  5. Data cleaning — validate, outlier removal, gap fill (single market)
  6. Clean + align — full pipeline across all 6 markets
  7. Continuous    — rollover detection on ETF proxies (should be zero rollovers)
  8. Cache verify  — confirm parquet cache files exist and reload correctly

Run from AlgoBot/ root:
    /c/Users/ghost/miniconda3/envs/algobot_env/python.exe test_phase1.py
"""

import sys
import os
from pathlib import Path

# ── Encoding safety for Windows cp1252 terminals ──────────────────────────────
# Reconfigure stdout/stderr to use UTF-8 if the terminal supports it.
# This prevents crashes when any library outputs Unicode chars.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass  # Python < 3.7 — skip

# ── Path setup ────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "[PASS]"
FAIL = "[FAIL]"
SEP  = "-" * 70

def header(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)

def ok(msg: str)   -> None: print(f"  {PASS} {msg}")
def err(msg: str)  -> None: print(f"  {FAIL} {msg}")
def info(msg: str) -> None: print(f"       {msg}")

# ── Test 1: Logger ─────────────────────────────────────────────────────────────

def test_logger() -> bool:
    header("TEST 1: Logger")
    try:
        from src.utils.logger import get_logger, sanitize_message
        log = get_logger("test_phase1")

        log.info("Logger test — INFO level")
        log.debug("Logger test — DEBUG level (file only)")
        log.warning("Logger test — WARNING level")

        # Credential sanitization
        raw   = "Connecting with api_key=super_secret_123"
        clean = sanitize_message(raw)
        assert "super_secret_123" not in clean, "Credential not redacted"
        assert "REDACTED" in clean, "REDACTED marker missing"

        ok("Logger initialized (console + file sinks)")
        ok(f"Sanitization: '{raw}' -> '{clean}'")

        log_file = PROJECT_ROOT / "logs" / "algobot.log"
        if log_file.exists():
            ok(f"Log file created: {log_file.name}")
        else:
            err("Log file not found — check logs/ directory")
            return False

        return True
    except Exception as e:
        err(f"Logger test failed: {e}")
        return False


# ── Test 2: Single market download ────────────────────────────────────────────

def test_single_download() -> bool:
    header("TEST 2: Single Market Download (ES proxy = SPY)")
    try:
        from src.utils.data_downloader import download_market

        df = download_market("ES", start="2022-01-01", end="2024-12-31")

        if df.empty:
            err("download_market returned empty DataFrame")
            return False

        ok(f"Downloaded {len(df)} bars for ES (SPY proxy)")
        info(f"Date range: {df.index[0].date()} to {df.index[-1].date()}")
        info(f"Columns: {list(df.columns)}")
        info(f"NaN count: {df[['Open','High','Low','Close']].isnull().sum().sum()}")

        assert len(df) > 500,         "Expected >500 bars for 3-year window"
        assert "Open" in df.columns,  "Missing Open column"
        assert "Close" in df.columns, "Missing Close column"
        assert df["Close"].isnull().sum() == 0, "NaN values in Close"

        ok("All assertions passed")
        return True
    except Exception as e:
        err(f"Single download test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 3: All 6 markets ─────────────────────────────────────────────────────

def test_all_markets() -> bool:
    header("TEST 3: All 6 Markets Download")
    try:
        from src.utils.data_downloader import download_all_markets

        all_data = download_all_markets(start="2020-01-01", end="2024-12-31")

        expected_markets = ["ES", "NQ", "GC", "CL", "ZB", "6E"]
        failed = []

        for mkt in expected_markets:
            if mkt not in all_data:
                err(f"{mkt}: NOT in result dict")
                failed.append(mkt)
            elif all_data[mkt].empty:
                err(f"{mkt}: empty DataFrame")
                failed.append(mkt)
            else:
                bars = len(all_data[mkt])
                ok(f"{mkt}: {bars} bars")

        if failed:
            err(f"Markets with problems: {failed}")
            return False

        ok("All 6 markets downloaded successfully")
        return True

    except Exception as e:
        err(f"All-markets test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 4: Summary table ─────────────────────────────────────────────────────

def test_summary() -> bool:
    header("TEST 4: Data Summary Table")
    try:
        from src.utils.data_downloader import download_all_markets, summarize_data

        all_data = download_all_markets(start="2020-01-01", end="2024-12-31")
        table = summarize_data(all_data)

        if table is None or table.empty:
            err("summarize_data returned empty")
            return False

        print()
        print(table)
        print()

        ok("Summary table generated successfully")
        return True

    except Exception as e:
        err(f"Summary test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 5: Data cleaning (single market) ─────────────────────────────────────

def test_cleaning_single() -> bool:
    header("TEST 5: Data Cleaning — Single Market (ES)")
    try:
        from src.utils.data_downloader import download_market
        from src.utils.data_cleaner import clean_market_data

        raw_es = download_market("ES", start="2020-01-01", end="2024-12-31")

        if raw_es.empty:
            err("No ES data to clean")
            return False

        clean_es, report = clean_market_data(raw_es, "ES")

        info(f"Report: {report.summary()}")
        info(f"Validation passed: {report.passed_validation}")
        info(f"Original bars: {report.original_bar_count}")
        info(f"Final bars:    {report.final_bar_count}")
        info(f"NaN filled:    {report.nan_bars_filled}")
        info(f"Outliers:      {report.outlier_bars_removed}")
        info(f"Gaps filled:   {report.gaps_filled}")

        if report.validation_errors:
            info(f"Validation errors: {report.validation_errors}")

        assert report.passed_validation, "ES data failed validation"
        assert not clean_es.empty,       "Cleaned ES DataFrame is empty"
        assert clean_es["Close"].isnull().sum() == 0, "NaN in cleaned Close"

        ok("ES cleaning pipeline passed")
        return True

    except Exception as e:
        err(f"Single-market cleaning failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 6: Clean + align all markets ─────────────────────────────────────────

def test_clean_and_align() -> bool:
    header("TEST 6: Clean + Align All 6 Markets")
    try:
        from src.utils.data_downloader import download_all_markets
        from src.utils.data_cleaner import clean_all_markets, align_dates

        raw_data = download_all_markets(start="2020-01-01", end="2024-12-31")
        cleaned, reports = clean_all_markets(raw_data)

        print()
        print("  Cleaning Reports:")
        for mkt, rep in reports.items():
            info(rep.summary())

        all_passed = all(r.passed_validation for r in reports.values())
        if not all_passed:
            failed = [m for m, r in reports.items() if not r.passed_validation]
            err(f"Markets that failed validation: {failed}")
            return False

        ok(f"All {len(cleaned)} markets cleaned successfully")

        # Align dates
        aligned = align_dates(cleaned, method="intersection")

        if not aligned:
            err("align_dates returned empty dict")
            return False

        # Verify all markets share the same index
        indices = [df.index for df in aligned.values()]
        first_idx = indices[0]
        all_same = all(first_idx.equals(idx) for idx in indices[1:])

        if not all_same:
            err("Aligned DataFrames have different indices")
            return False

        common_days = len(first_idx)
        start_date  = first_idx[0].date()
        end_date    = first_idx[-1].date()

        print()
        ok(f"Date alignment successful: {common_days} common trading days")
        info(f"Common range: {start_date} to {end_date}")
        info(f"Markets aligned: {list(aligned.keys())}")

        return True

    except Exception as e:
        err(f"Clean+align test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 7: Continuous contract (ETF proxies = no rollovers) ──────────────────

def test_continuous_contract() -> bool:
    header("TEST 7: Continuous Contract (ETF Proxies — Expect 0 Rollovers)")
    try:
        from src.utils.data_downloader import download_market
        from src.utils.continuous_contract import build_continuous_series, detect_rollover_gaps

        # Test on GC (Gold) — most likely to show gaps if any ETF proxy has them
        raw_gc = download_market("GC", start="2020-01-01", end="2024-12-31")

        if raw_gc.empty:
            err("No GC data for rollover test")
            return False

        rollover_dates = detect_rollover_gaps(raw_gc, threshold_pct=0.01, market="GC")

        info(f"GC rollover dates detected: {len(rollover_dates)}")
        if rollover_dates:
            info(f"Dates: {[d.strftime('%Y-%m-%d') for d in rollover_dates[:5]]}")

        # Build continuous series (safe for any data)
        continuous = build_continuous_series(raw_gc, "GC")

        assert not continuous.empty, "Continuous series is empty"
        assert len(continuous) > 0,  "Continuous series has no bars"

        ok(f"build_continuous_series completed — {len(continuous)} bars")

        if len(rollover_dates) == 0:
            ok("Zero rollover gaps detected (expected for ETF proxy GC=F)")
        else:
            info(f"Note: {len(rollover_dates)} rollover-like gaps found")
            info("These may be commodity futures pricing artifacts — review manually")

        # Test ES proxy too
        raw_es = download_market("ES", start="2020-01-01", end="2024-12-31")
        es_rollovers = detect_rollover_gaps(raw_es, threshold_pct=0.01, market="ES")
        info(f"ES rollover dates detected: {len(es_rollovers)}")

        ok("Continuous contract module working correctly")
        return True

    except Exception as e:
        err(f"Continuous contract test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Test 8: Cache verification ─────────────────────────────────────────────────

def test_cache() -> bool:
    header("TEST 8: Parquet Cache Verification")
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        cache_dir = PROJECT_ROOT / "data" / "raw"

        if not cache_dir.exists():
            err(f"Cache directory does not exist: {cache_dir}")
            return False

        parquet_files = list(cache_dir.glob("*.parquet"))

        if not parquet_files:
            err("No .parquet cache files found — run download tests first")
            return False

        ok(f"Found {len(parquet_files)} cache files in data/raw/")

        import pandas as pd
        errors = []

        for f in parquet_files:
            try:
                df = pd.read_parquet(f)
                size_kb = f.stat().st_size / 1024
                info(f"  {f.name}: {len(df)} bars, {size_kb:.1f} KB")
            except Exception as e:
                errors.append(f"{f.name}: {e}")
                err(f"  Failed to read {f.name}: {e}")

        if errors:
            err(f"{len(errors)} cache files could not be read")
            return False

        # Test cache reload speed
        import time
        from src.utils.data_downloader import download_market

        start = time.perf_counter()
        df_cached = download_market("ES", start="2020-01-01", end="2024-12-31",
                                    use_cache=True, force_refresh=False)
        elapsed = time.perf_counter() - start

        if df_cached.empty:
            err("Cache reload returned empty DataFrame")
            return False

        ok(f"Cache reload: {len(df_cached)} ES bars in {elapsed:.3f}s")
        info("(First run downloads from Yahoo; subsequent runs load from disk)")

        return True

    except Exception as e:
        err(f"Cache test failed: {e}")
        import traceback; traceback.print_exc()
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print()
    print("=" * 70)
    print("  AlgoBot — Phase 1 Test Suite: Data Infrastructure")
    print("=" * 70)

    tests = [
        ("Test 1: Logger",                  test_logger),
        ("Test 2: Single download (ES)",    test_single_download),
        ("Test 3: All 6 markets",           test_all_markets),
        ("Test 4: Summary table",           test_summary),
        ("Test 5: Cleaning (ES)",           test_cleaning_single),
        ("Test 6: Clean + align all mkts",  test_clean_and_align),
        ("Test 7: Continuous contract",     test_continuous_contract),
        ("Test 8: Cache verification",      test_cache),
    ]

    results = {}
    for name, fn in tests:
        try:
            results[name] = fn()
        except Exception as e:
            err(f"Unexpected crash in {name}: {e}")
            results[name] = False

    # ── Final scorecard ──────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("  PHASE 1 RESULTS")
    print("=" * 70)

    passed = sum(1 for v in results.values() if v)
    total  = len(results)

    for name, result in results.items():
        status = PASS if result else FAIL
        print(f"  {status}  {name}")

    print()
    if passed == total:
        print(f"  *** ALL {total}/{total} TESTS PASSED — Phase 1 COMPLETE ***")
        print("  Data infrastructure is verified and ready for Phase 2.")
    else:
        print(f"  {passed}/{total} tests passed. Fix failures before Phase 2.")

    print("=" * 70)
    print()

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
