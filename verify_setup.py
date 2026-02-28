"""
AlgoBot — Environment & Security Verification Script
=====================================================
Run this after installation to confirm:
  1. All Python libraries are correctly installed
  2. Project files and structure are in place
  3. Configuration file loads without errors
  4. No secrets are accidentally exposed
  5. Security baseline is met

Usage:
    conda activate algobot_env
    python verify_setup.py

Exit codes:
    0 = All checks passed — environment is ready
    1 = One or more checks failed — see output for details
"""

import sys
import os
import importlib
import pathlib


# ── Colors for terminal output ────────────────────────────────────────────────
class Color:
    GREEN  = "\033[92m"
    RED    = "\033[91m"
    YELLOW = "\033[93m"
    CYAN   = "\033[96m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

def ok(msg):    print(f"  {Color.GREEN}[OK]   {Color.RESET}{msg}")
def fail(msg):  print(f"  {Color.RED}[FAIL] {Color.RESET}{msg}")
def warn(msg):  print(f"  {Color.YELLOW}[WARN] {Color.RESET}{msg}")
def info(msg):  print(f"  {Color.CYAN}[INFO] {Color.RESET}{msg}")
def header(msg): print(f"\n{Color.BOLD}{msg}{Color.RESET}")


# ── Check 1: Python version ───────────────────────────────────────────────────
def check_python_version() -> bool:
    """Verify Python 3.11 or higher is being used."""
    header("CHECK 1 — Python Version")
    major = sys.version_info.major
    minor = sys.version_info.minor
    patch = sys.version_info.micro
    version_str = f"{major}.{minor}.{patch}"

    info(f"Detected: Python {version_str}")

    if major == 3 and minor >= 11:
        ok(f"Python {version_str} — meets requirement (3.11+)")
        return True
    elif major == 3 and minor >= 9:
        warn(f"Python {version_str} — works but 3.11+ is recommended")
        return True
    else:
        fail(f"Python {version_str} — too old. Install Python 3.11+")
        return False


# ── Check 2: Conda environment ────────────────────────────────────────────────
def check_conda_environment() -> bool:
    """Verify we are running inside the algobot_env conda environment."""
    header("CHECK 2 — Conda Environment")
    env_name = os.environ.get("CONDA_DEFAULT_ENV", "")
    env_prefix = os.environ.get("CONDA_PREFIX", "")

    info(f"Active environment: {env_name or 'none detected'}")
    info(f"Environment path: {env_prefix or 'not set'}")

    if "algobot" in env_name.lower():
        ok("algobot_env is active")
        return True
    elif env_name:
        warn(f"Running in '{env_name}' — expected 'algobot_env'")
        warn("Run: conda activate algobot_env")
        return False
    else:
        warn("No conda environment detected")
        warn("Run: conda activate algobot_env")
        return False


# ── Check 3: Required libraries ───────────────────────────────────────────────
def check_libraries() -> bool:
    """Verify all required Python libraries can be imported."""
    header("CHECK 3 — Required Libraries")

    required = [
        ("numpy",           "NumPy",            "Scientific computing"),
        ("pandas",          "Pandas",            "Data manipulation"),
        ("scipy",           "SciPy",             "Statistical analysis"),
        ("matplotlib",      "Matplotlib",        "Charting"),
        ("plotly",          "Plotly",            "Interactive charts"),
        ("vectorbt",        "VectorBT",          "Fast backtesting"),
        ("empyrical",       "Empyrical",         "Risk metrics"),
        ("statsmodels",     "StatsModels",       "Statistical models"),
        ("pandas_ta",       "Pandas-TA",         "Technical indicators"),
        ("ta",              "TA Library",        "Additional indicators"),
        ("yfinance",        "yFinance",          "Yahoo Finance data"),
        ("fredapi",         "FRED API",          "Macro economic data"),
        ("sklearn",         "Scikit-Learn",      "Machine learning"),
        ("yaml",            "PyYAML",            "Config file parsing"),
        ("loguru",          "Loguru",            "Logging"),
        ("jinja2",          "Jinja2",            "HTML report templates"),
        ("dotenv",          "Python-Dotenv",     "Secure env var loading"),
        ("requests",        "Requests",          "HTTP calls"),
        ("schedule",        "Schedule",          "Task scheduling"),
        ("tqdm",            "tqdm",              "Progress bars"),
    ]

    passed = 0
    failed_libs = []

    for module, display, purpose in required:
        try:
            mod = importlib.import_module(module)
            version = getattr(mod, "__version__", "?")
            ok(f"{display:<20} v{version:<10} — {purpose}")
            passed += 1
        except ImportError:
            fail(f"{display:<20} NOT INSTALLED — {purpose}")
            failed_libs.append(display)

    if failed_libs:
        print(f"\n  To install missing libraries:")
        print(f"  pip install -r requirements.txt")

    return len(failed_libs) == 0


# ── Check 4: Project file structure ──────────────────────────────────────────
def check_project_structure() -> bool:
    """Verify all required project files and directories exist."""
    header("CHECK 4 — Project File Structure")

    base = pathlib.Path(__file__).parent

    required_dirs = [
        "config",
        "docs",
        "src",
        "src/strategy",
        "src/backtest",
        "src/live",
        "src/analysis",
        "src/utils",
        "data/raw",
        "data/processed",
        "reports/backtests",
        "reports/validation",
        "reports/live",
        "notebooks",
        "logs",
    ]

    required_files = [
        "README.md",
        "requirements.txt",
        ".gitignore",
        ".env.example",
        "config/config.yaml",
        "docs/LAB_001_Environment_Setup.md",
    ]

    all_present = True

    info("Checking directories:")
    for d in required_dirs:
        path = base / d
        if path.is_dir():
            ok(f"  {d}/")
        else:
            fail(f"  {d}/ — MISSING")
            all_present = False

    info("Checking files:")
    for f in required_files:
        path = base / f
        if path.is_file():
            ok(f"  {f}")
        else:
            fail(f"  {f} — MISSING")
            all_present = False

    return all_present


# ── Check 5: Configuration file ───────────────────────────────────────────────
def check_config() -> bool:
    """Verify config.yaml loads correctly and contains required sections."""
    header("CHECK 5 — Configuration File")

    config_path = pathlib.Path(__file__).parent / "config" / "config.yaml"

    if not config_path.exists():
        fail("config/config.yaml not found")
        return False

    try:
        import yaml
        with open(config_path, "r") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        fail(f"config.yaml failed to parse: {e}")
        return False

    required_sections = [
        "project", "mode", "strategy", "regime",
        "position_sizing", "risk", "markets",
        "backtest", "validation", "live", "reporting"
    ]

    all_present = True
    for section in required_sections:
        if section in config:
            ok(f"Section '{section}' — present")
        else:
            fail(f"Section '{section}' — MISSING from config.yaml")
            all_present = False

    if all_present:
        # Validate critical values
        mode = config.get("mode", "")
        if mode == "backtest":
            ok(f"Mode is 'backtest' — safe for development")
        elif mode == "paper":
            warn(f"Mode is 'paper' — ensure you intend this")
        elif mode == "live":
            warn(f"Mode is 'LIVE' — only acceptable after full validation!")

    return all_present


# ── Check 6: Security baseline ────────────────────────────────────────────────
def check_security() -> bool:
    """
    Security baseline verification.
    Checks that no secrets are accidentally exposed.
    """
    header("CHECK 6 — Security Baseline")

    base = pathlib.Path(__file__).parent
    all_secure = True

    # 6a: .env file should exist but NOT contain real keys
    env_file = base / ".env"
    if env_file.exists():
        ok(".env file exists (will be used for secrets)")
        # Check it doesn't contain obviously dangerous exposed values
        with open(env_file, "r") as f:
            content = f.read()
        dangerous_patterns = [
            ("sk-", "OpenAI-style key detected in .env"),
            ("AKIA", "AWS access key detected in .env"),
        ]
        for pattern, message in dangerous_patterns:
            if pattern in content:
                warn(f"Possible exposed credential: {message}")
    else:
        warn(".env file not found")
        warn("  Create it: copy .env.example to .env and fill in your keys")
        warn("  The bot will not have API access without it")

    # 6b: .env must NOT be committed (verify .gitignore)
    gitignore_path = base / ".gitignore"
    if gitignore_path.exists():
        with open(gitignore_path, "r") as f:
            gitignore_content = f.read()
        if ".env" in gitignore_content:
            ok(".env is in .gitignore — secrets will not be committed to git")
        else:
            fail(".env is NOT in .gitignore — secrets could be committed!")
            all_secure = False
    else:
        fail(".gitignore not found — secrets could be committed to git!")
        all_secure = False

    # 6c: data/ and logs/ are gitignored
    for sensitive_dir in ["data/", "logs/"]:
        if sensitive_dir in gitignore_content:
            ok(f"{sensitive_dir} is gitignored — large/sensitive files protected")
        else:
            warn(f"{sensitive_dir} is not explicitly gitignored")

    # 6d: No hardcoded credentials in any Python source files
    src_path = base / "src"
    suspicious_strings = [
        "password =", "password=", "api_key =", "api_key=",
        "secret =", "secret=", "token =", "token=",
    ]

    if src_path.exists():
        found_in_source = False
        for py_file in src_path.rglob("*.py"):
            with open(py_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            for i, line in enumerate(lines, 1):
                line_lower = line.lower().strip()
                # Skip comments and docstrings
                if line_lower.startswith("#") or line_lower.startswith('"""'):
                    continue
                for suspicious in suspicious_strings:
                    if suspicious in line_lower and "os.environ" not in line_lower:
                        warn(f"Possible hardcoded credential in {py_file.name}:{i}")
                        warn(f"  Line: {line.strip()[:60]}...")
                        found_in_source = True
        if not found_in_source:
            ok("No hardcoded credentials found in src/ Python files")
    else:
        info("src/ directory is empty — no source files to scan yet")

    # 6e: Config mode safety check
    config_path = base / "config" / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path, "r") as f:
                cfg = yaml.safe_load(f)
            mode = cfg.get("mode", "backtest")
            if mode == "backtest":
                ok(f"Config mode = 'backtest' — safe")
            elif mode == "paper":
                ok(f"Config mode = 'paper' — safe for paper trading")
            elif mode == "live":
                warn("Config mode = 'LIVE' — only acceptable after full validation!")
        except Exception:
            pass

    return all_secure


# ── Check 7: Git repository status ───────────────────────────────────────────
def check_git() -> bool:
    """Verify git is initialized and .env is not tracked."""
    header("CHECK 7 — Git Repository")

    import subprocess

    base = pathlib.Path(__file__).parent

    # Check if git repo exists
    git_dir = base / ".git"
    if not git_dir.exists():
        warn("Git repository not initialized yet")
        warn("  Run: git init")
        warn("  Then: git add . && git commit -m 'LAB_001: Initial setup'")
        return False

    ok("Git repository initialized")

    # Check if .env is tracked (it should NOT be)
    result = subprocess.run(
        ["git", "ls-files", ".env"],
        capture_output=True, text=True, cwd=str(base)
    )
    if result.stdout.strip():
        fail(".env IS tracked by git — this exposes your secrets!")
        fail("  Fix: git rm --cached .env && git commit -m 'Remove .env from tracking'")
        return False
    else:
        ok(".env is NOT tracked by git — secrets are safe")

    return True


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    """Run all verification checks. Return 0 if all pass, 1 if any fail."""
    sep = "=" * 60

    print(f"\n{Color.BOLD}{sep}")
    print("  ALGOBOT — ENVIRONMENT & SECURITY VERIFICATION")
    print(f"  Phase 0 — Setup Check")
    print(f"{sep}{Color.RESET}")

    checks = [
        ("Python Version",        check_python_version),
        ("Conda Environment",     check_conda_environment),
        ("Required Libraries",    check_libraries),
        ("Project Structure",     check_project_structure),
        ("Configuration File",    check_config),
        ("Security Baseline",     check_security),
        ("Git Repository",        check_git),
    ]

    results = {}
    for name, func in checks:
        try:
            results[name] = func()
        except Exception as e:
            fail(f"Check '{name}' threw an exception: {e}")
            results[name] = False

    # Summary
    print(f"\n{Color.BOLD}{sep}")
    print("  SUMMARY")
    print(sep)

    passed = 0
    failed = 0
    warned = []

    for name, result in results.items():
        if result:
            print(f"  {Color.GREEN}PASS{Color.RESET}  {name}")
            passed += 1
        else:
            print(f"  {Color.RED}FAIL{Color.RESET}  {name}")
            failed += 1

    print(f"\n  Passed: {passed}/{len(checks)}")

    if failed == 0:
        print(f"\n  {Color.GREEN}{Color.BOLD}STATUS: ALL CLEAR{Color.RESET}")
        print(f"  Environment is fully set up and secure.")
        print(f"  Proceed to: LAB_002 — Data Infrastructure")
        print(f"{sep}\n")
        return 0
    else:
        print(f"\n  {Color.RED}{Color.BOLD}STATUS: {failed} CHECK(S) FAILED{Color.RESET}")
        print(f"  Fix the FAIL items above before proceeding.")
        print(f"  Refer to: docs/LAB_001_Environment_Setup.md")
        print(f"{sep}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
