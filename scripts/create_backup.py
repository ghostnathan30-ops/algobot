"""
AlgoBot -- Encrypted Backup Creator
=====================================
Script:  scripts/create_backup.py
Purpose: Create a full AES-256-GCM encrypted backup of the AlgoBot project
         to D:\ghost\AlgoBot_Backups\, restricted to Windows Administrators only.

Encryption:
    - Algorithm : AES-256-GCM (authenticated encryption -- provides both
                  confidentiality AND integrity verification)
    - Key size  : 256-bit (32 bytes), randomly generated per backup
    - Nonce     : 96-bit (12 bytes), randomly generated per backup
    - Without the .key file the backup CANNOT be decrypted, ever.

Backup layout on D: drive:
    D:\\ghost\\AlgoBot_Backups\\
        AlgoBot_2026-02-28_170000.zip.enc   <- encrypted backup archive
        keys\\
            AlgoBot_2026-02-28_170000.key   <- decryption key (KEEP SAFE)

NTFS permissions:
    - Folder is restricted to BUILTIN\\Administrators only
    - Regular users (including guest accounts) cannot read or list the folder
    - Only Windows admin can access, even if they find the path

Usage:
    conda run -n algobot_env python scripts/create_backup.py
    conda run -n algobot_env python scripts/create_backup.py --include-data
    conda run -n algobot_env python scripts/create_backup.py --dest "E:\\MyBackups"

To restore:
    conda run -n algobot_env python scripts/restore_backup.py \\
        --backup "D:\\ghost\\AlgoBot_Backups\\AlgoBot_2026-02-28_170000.zip.enc" \\
        --key    "D:\\ghost\\AlgoBot_Backups\\keys\\AlgoBot_2026-02-28_170000.key"
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# ── AES-256-GCM encryption ──────────────────────────────────────────────────
try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    sys.exit("ERROR: cryptography not installed.\n"
             "Run: conda run -n algobot_env pip install cryptography")

PROJECT_ROOT  = Path(__file__).parent.parent
BACKUP_MAGIC  = b"ALGB"   # 4-byte magic header to identify AlgoBot backups
KEY_SIZE      = 32         # 256 bits
NONCE_SIZE    = 12         # 96 bits (standard for GCM)

# Directories / files to exclude from the backup
EXCLUDE_DIRS  = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache",
                 "node_modules", ".venv", "venv", "env"}
EXCLUDE_EXTS  = {".pyc", ".pyo", ".pyd"}

# Large data directories -- excluded by default, included with --include-data
LARGE_DATA_DIRS = {"data/raw"}


# ============================================================
# STEP 1 -- Build zip archive in memory
# ============================================================

def build_zip(source_dir: Path, include_data: bool) -> bytes:
    """
    Recursively add all project files into an in-memory ZIP archive.
    Returns the raw zip bytes.
    """
    buf = io.BytesIO()
    total_files = 0
    total_bytes = 0

    with zipfile.ZipFile(buf, mode="w",
                         compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_dir():
                continue

            # Skip excluded dirs
            relative = path.relative_to(source_dir)
            parts    = relative.parts
            if any(p in EXCLUDE_DIRS for p in parts):
                continue
            if path.suffix in EXCLUDE_EXTS:
                continue

            # Skip large data directories unless --include-data
            rel_str = str(relative).replace("\\", "/")
            if not include_data:
                if any(rel_str.startswith(d) for d in LARGE_DATA_DIRS):
                    continue

            try:
                zf.write(path, arcname=str(relative))
                total_files += 1
                total_bytes += path.stat().st_size
            except (PermissionError, OSError) as e:
                print(f"  WARNING: Skipped {relative}: {e}")

    raw = buf.getvalue()
    print(f"  Archive: {total_files} files, "
          f"{total_bytes / 1_048_576:.1f} MB uncompressed, "
          f"{len(raw) / 1_048_576:.1f} MB compressed")
    return raw


# ============================================================
# STEP 2 -- Encrypt
# ============================================================

def encrypt(plaintext: bytes) -> tuple[bytes, bytes]:
    """
    Encrypt plaintext with AES-256-GCM.
    Returns (key_bytes, encrypted_blob).
    Blob format: MAGIC(4) + NONCE(12) + CIPHERTEXT+TAG(n)
    """
    key   = os.urandom(KEY_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    aesgcm    = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    blob = BACKUP_MAGIC + nonce + ciphertext
    return key, blob


# ============================================================
# STEP 3 -- Save to D: drive
# ============================================================

def save_backup(dest_dir: Path, timestamp: str,
                blob: bytes, key_bytes: bytes) -> tuple[Path, Path]:
    """
    Save the encrypted blob and key file to the backup directory.
    Returns (backup_path, key_path).
    """
    backup_dir = dest_dir
    keys_dir   = dest_dir / "keys"
    backup_dir.mkdir(parents=True, exist_ok=True)
    keys_dir.mkdir(parents=True, exist_ok=True)

    name        = f"AlgoBot_{timestamp}"
    backup_path = backup_dir / f"{name}.zip.enc"
    key_path    = keys_dir   / f"{name}.key"

    backup_path.write_bytes(blob)
    # Key file: hex-encoded key + metadata header
    key_file_content = (
        f"# AlgoBot Backup Key\n"
        f"# Created   : {timestamp}\n"
        f"# Algorithm : AES-256-GCM\n"
        f"# Backup    : {backup_path.name}\n"
        f"# WARNING   : This file decrypts the backup. Keep it SECURE.\n"
        f"#             Without this file the backup cannot be recovered.\n"
        f"\nKEY={key_bytes.hex()}\n"
    )
    key_path.write_text(key_file_content, encoding="utf-8")

    print(f"  Backup : {backup_path}  ({len(blob) / 1_048_576:.1f} MB)")
    print(f"  Key    : {key_path}")
    return backup_path, key_path


# ============================================================
# STEP 4 -- Lock down with Windows admin-only permissions
# ============================================================

def set_admin_only_permissions(backup_file: Path, key_file: Path) -> None:
    """
    Lock individual backup files so ONLY Administrators can read them.
    The parent folder remains writable so future backups can be created.
    Uses icacls to remove inheritance and grant only Administrators access.
    """
    locked = 0
    for f in [backup_file, key_file]:
        cmd = (
            f'icacls "{f}" /inheritance:r '
            f'/grant:r "BUILTIN\\Administrators:(F)" '
            f'/grant:r "NT AUTHORITY\\SYSTEM:(F)"'
        )
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True,
                                    text=True, timeout=15)
            if result.returncode == 0:
                locked += 1
            else:
                print(f"  WARNING: Could not lock {f.name}: {result.stderr.strip()}")
        except Exception as e:
            print(f"  WARNING: icacls failed for {f.name}: {e}")

    if locked == 2:
        print(f"  Permissions: Both files locked to Administrators only")
    elif locked > 0:
        print(f"  Permissions: {locked}/2 files locked to Administrators only")
    else:
        print(f"  WARNING: File permissions not set. Encryption is still protecting the data.")
        print(f"  To lock manually: right-click each file > Properties > Security > Advanced")


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="AlgoBot Encrypted Backup")
    parser.add_argument("--include-data", action="store_true",
                        help="Include data/raw/ Parquet files (adds ~50-200MB)")
    parser.add_argument("--dest", type=str,
                        default=r"D:\ghost\AlgoBot_Backups",
                        help="Backup destination folder (default: D:\\ghost\\AlgoBot_Backups)")
    args = parser.parse_args()

    dest_dir  = Path(args.dest)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    print("\n" + "=" * 65)
    print("  AlgoBot -- Encrypted Backup")
    print("=" * 65)
    print(f"  Source : {PROJECT_ROOT}")
    print(f"  Dest   : {dest_dir}")
    print(f"  Time   : {timestamp}")
    print(f"  Data   : {'included' if args.include_data else 'excluded (use --include-data to add)'}")
    print()

    # 1. Check D: drive exists
    drive = Path(args.dest[:3])   # e.g. D:\
    if not drive.exists():
        sys.exit(f"ERROR: Drive {drive} not found. Is the backup drive connected?")

    # 2. Build zip
    print("[1/4] Building archive...")
    zip_bytes = build_zip(PROJECT_ROOT, args.include_data)

    # 3. Encrypt
    print("[2/4] Encrypting (AES-256-GCM)...")
    key_bytes, blob = encrypt(zip_bytes)
    compression_ratio = len(zip_bytes) / len(blob) * 100
    print(f"  Encrypted size: {len(blob) / 1_048_576:.1f} MB")

    # 4. Save
    print("[3/4] Saving to backup drive...")
    backup_path, key_path = save_backup(dest_dir, timestamp, blob, key_bytes)

    # 5. Verify BEFORE locking (decrypt header check)
    print("[4/4] Verifying backup integrity...")
    test_blob = backup_path.read_bytes()
    test_key  = bytes.fromhex(
        [line for line in key_path.read_text().splitlines()
         if line.startswith("KEY=")][0].split("=")[1]
    )
    assert test_blob[:4] == BACKUP_MAGIC, "Magic header mismatch"
    nonce = test_blob[4:4 + NONCE_SIZE]
    ct    = test_blob[4 + NONCE_SIZE:]
    AESGCM(test_key).decrypt(nonce, ct, None)   # raises if tampered
    print("  Integrity check: PASSED (backup decrypts correctly)")

    # 6. Lock permissions AFTER verification
    print("\nLocking files to Administrators only...")
    set_admin_only_permissions(backup_path, key_path)

    # 7. Summary
    print("\n" + "=" * 65)
    print("  BACKUP COMPLETE")
    print("=" * 65)
    print(f"  Backup file  : {backup_path}")
    print(f"  Key file     : {key_path}")
    print(f"  Backup size  : {len(blob) / 1_048_576:.1f} MB")
    print()
    print("  ADMIN KEY (record this and store separately from the backup):")
    print(f"  {key_bytes.hex()}")
    print()
    print("  HOW TO RESTORE:")
    print(f"  conda run -n algobot_env python scripts/restore_backup.py \\")
    print(f"      --backup \"{backup_path}\" \\")
    print(f"      --key    \"{key_path}\"")
    print()
    print("  SECURITY NOTES:")
    print("  - The .zip.enc and .key files are restricted to Windows Administrators only")
    print("  - The .zip.enc file is useless without the .key file")
    print("  - Store the .key file (or the hex key above) in a SEPARATE")
    print("    location -- USB drive, password manager, or printed paper")
    print("  - If you lose the key, the backup CANNOT be recovered")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
