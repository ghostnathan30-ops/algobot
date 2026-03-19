"""
AlgoBot -- Dashboard Auth Setup
=================================
Run this ONCE to create your dashboard login credentials.
Stores a bcrypt-hashed password (never stored in plain text).

    conda run -n algobot_env python scripts/setup_dashboard_auth.py
"""

import json
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import bcrypt as _bcrypt
except ImportError:
    sys.exit("Run: conda run -n algobot_env pip install bcrypt")

CONFIG_DIR = PROJECT_ROOT / "dashboard" / "config"
AUTH_FILE  = CONFIG_DIR / "auth.json"


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 55)
    print("  AlgoBot Dashboard -- Auth Setup")
    print("=" * 55)

    # Detect non-interactive terminal (e.g. conda run without -i flag)
    if not sys.stdin.isatty():
        print("\n  ERROR: This script requires an interactive terminal.")
        print("  Run it like this instead:\n")
        print("    conda activate algobot_env")
        print("    python scripts/setup_dashboard_auth.py\n")
        sys.exit(1)

    if AUTH_FILE.exists():
        print(f"\n  Auth file already exists: {AUTH_FILE}")
        overwrite = input("  Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            print("  Cancelled.\n")
            return

    username = input("\n  Choose a username: ").strip()
    if not username:
        sys.exit("ERROR: Username cannot be empty.")

    import getpass
    while True:
        pw1 = getpass.getpass("  Choose a password (min 8 chars, max 72): ")
        if len(pw1) < 8:
            print("  Password must be at least 8 characters.")
            continue
        if len(pw1.encode("utf-8")) > 72:
            print("  Password too long (max 72 bytes for bcrypt). Use a shorter password.")
            continue
        pw2 = getpass.getpass("  Confirm password: ")
        if pw1 != pw2:
            print("  Passwords do not match. Try again.")
            continue
        break

    hashed     = _bcrypt.hashpw(pw1.encode("utf-8"), _bcrypt.gensalt(rounds=12)).decode("utf-8")
    secret_key = secrets.token_hex(48)   # 384-bit random secret for JWT signing

    config = {
        "username":      username,
        "password_hash": hashed,
        "secret_key":    secret_key,
    }

    AUTH_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # Lock the file to current user only
    try:
        import stat
        AUTH_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass

    print(f"\n  Auth saved to: {AUTH_FILE}")
    print("  Password stored as bcrypt hash (never plain text).")
    print("\n  Start the dashboard:")
    print("  conda run -n algobot_env uvicorn dashboard.server:app --host 127.0.0.1 --port 8000")
    print("  Open: http://localhost:8000")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    main()
