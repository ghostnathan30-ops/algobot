"""
AlgoBot -- Encrypted Backup Restore
======================================
Script:  scripts/restore_backup.py
Purpose: Decrypt and restore an AlgoBot backup created by create_backup.py.

Usage:
    conda run -n algobot_env python scripts/restore_backup.py \\
        --backup "D:\\ghost\\AlgoBot_Backups\\AlgoBot_2026-02-28_170000.zip.enc" \\
        --key    "D:\\ghost\\AlgoBot_Backups\\keys\\AlgoBot_2026-02-28_170000.key"

    # Or provide the raw hex key directly (if you printed it at backup time):
    conda run -n algobot_env python scripts/restore_backup.py \\
        --backup "D:\\ghost\\AlgoBot_Backups\\AlgoBot_2026-02-28_170000.zip.enc" \\
        --hex-key "a1b2c3d4..."   # 64-char hex string

    # List available backups:
    conda run -n algobot_env python scripts/restore_backup.py --list

Options:
    --backup    Path to the .zip.enc encrypted backup file
    --key       Path to the .key file
    --hex-key   Raw hex key string (alternative to --key file)
    --dest      Restore destination directory (default: current project root)
    --list      List all backups in D:\\ghost\\AlgoBot_Backups\\
    --verify    Verify backup integrity without extracting
"""

from __future__ import annotations

import argparse
import io
import sys
import zipfile
from datetime import datetime
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:
    sys.exit("ERROR: cryptography not installed.\n"
             "Run: conda run -n algobot_env pip install cryptography")

BACKUP_MAGIC = b"ALGB"
NONCE_SIZE   = 12
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_BACKUP_DIR = Path(r"D:\ghost\AlgoBot_Backups")


# ============================================================
# DECRYPT
# ============================================================

def load_key(key_path: Path = None, hex_key: str = None) -> bytes:
    """Load decryption key from file or hex string."""
    if hex_key:
        try:
            key = bytes.fromhex(hex_key.strip())
            assert len(key) == 32, f"Key must be 32 bytes, got {len(key)}"
            return key
        except Exception as e:
            sys.exit(f"ERROR: Invalid hex key: {e}")

    if not key_path or not key_path.exists():
        sys.exit(f"ERROR: Key file not found: {key_path}")

    content = key_path.read_text(encoding="utf-8")
    for line in content.splitlines():
        if line.startswith("KEY="):
            return bytes.fromhex(line.split("=", 1)[1].strip())
    sys.exit(f"ERROR: No KEY= line found in {key_path}")


def decrypt(blob: bytes, key: bytes) -> bytes:
    """
    Decrypt an AES-256-GCM encrypted blob.
    Verifies magic header and authentication tag.
    Returns raw zip bytes.
    """
    if blob[:4] != BACKUP_MAGIC:
        sys.exit("ERROR: Not a valid AlgoBot backup file (wrong magic header).")

    nonce      = blob[4:4 + NONCE_SIZE]
    ciphertext = blob[4 + NONCE_SIZE:]
    aesgcm     = AESGCM(key)

    try:
        return aesgcm.decrypt(nonce, ciphertext, None)
    except Exception:
        sys.exit(
            "ERROR: Decryption failed.\n"
            "  - Wrong key file / hex key\n"
            "  - Backup file may be corrupted or tampered with"
        )


# ============================================================
# RESTORE
# ============================================================

def restore_zip(zip_bytes: bytes, dest_dir: Path, overwrite: bool = False) -> int:
    """Extract zip bytes to dest_dir. Returns number of files extracted."""
    buf = io.BytesIO(zip_bytes)
    extracted = 0

    with zipfile.ZipFile(buf, mode="r") as zf:
        members = zf.infolist()
        print(f"  Archive contains {len(members)} files")

        for member in members:
            target = dest_dir / member.filename
            if target.exists() and not overwrite:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(member.filename))
            extracted += 1

    return extracted


# ============================================================
# LIST BACKUPS
# ============================================================

def list_backups(backup_dir: Path) -> None:
    if not backup_dir.exists():
        print(f"  No backup directory found at {backup_dir}")
        return

    enc_files = sorted(backup_dir.glob("*.zip.enc"))
    if not enc_files:
        print(f"  No backups found in {backup_dir}")
        return

    print(f"\n  Available backups in {backup_dir}:\n")
    print(f"  {'Filename':<45} {'Size':>10}  {'Key?':>5}")
    print(f"  {'─' * 45} {'─' * 10}  {'─' * 5}")

    for f in enc_files:
        size_mb  = f.stat().st_size / 1_048_576
        key_name = f.stem + ".key"
        key_path = backup_dir / "keys" / key_name
        key_ok   = "YES" if key_path.exists() else "NO"
        print(f"  {f.name:<45} {size_mb:>8.1f}MB  {key_ok:>5}")

    print()


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="AlgoBot Backup Restore")
    parser.add_argument("--backup",  type=str, help="Path to .zip.enc backup file")
    parser.add_argument("--key",     type=str, help="Path to .key file")
    parser.add_argument("--hex-key", type=str, help="Raw 64-char hex key string")
    parser.add_argument("--dest",    type=str, default=str(PROJECT_ROOT),
                        help="Restore destination (default: project root)")
    parser.add_argument("--list",    action="store_true",
                        help="List available backups")
    parser.add_argument("--verify",  action="store_true",
                        help="Verify integrity without extracting files")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing files during restore")
    args = parser.parse_args()

    print("\n" + "=" * 65)
    print("  AlgoBot -- Backup Restore")
    print("=" * 65)

    if args.list:
        list_backups(DEFAULT_BACKUP_DIR)
        return

    if not args.backup:
        print("  ERROR: --backup is required (or use --list to see available backups)")
        parser.print_help()
        return

    backup_path = Path(args.backup)
    key_path    = Path(args.key) if args.key else None
    dest_dir    = Path(args.dest)

    if not backup_path.exists():
        sys.exit(f"ERROR: Backup file not found: {backup_path}")

    # Load key
    key = load_key(key_path, args.hex_key)
    print(f"  Backup : {backup_path.name}")
    print(f"  Key    : {'(hex provided)' if args.hex_key else key_path}")
    print(f"  Dest   : {dest_dir}")

    # Decrypt
    print("\n[1/3] Decrypting...")
    blob      = backup_path.read_bytes()
    zip_bytes = decrypt(blob, key)
    print(f"  Decrypted: {len(zip_bytes) / 1_048_576:.1f} MB (integrity verified)")

    if args.verify:
        print("\n  VERIFY ONLY -- no files written")
        print("  Backup is valid and can be decrypted successfully.")
        print("=" * 65 + "\n")
        return

    # Restore
    print("\n[2/3] Extracting to destination...")
    if dest_dir.exists() and not args.overwrite:
        print(f"  WARNING: Destination exists. Only NEW files will be added.")
        print(f"  Use --overwrite to replace existing files.")

    dest_dir.mkdir(parents=True, exist_ok=True)
    n = restore_zip(zip_bytes, dest_dir, args.overwrite)

    print(f"\n[3/3] Restore complete: {n} files written to {dest_dir}")

    print("\n" + "=" * 65)
    print("  RESTORE COMPLETE")
    print("=" * 65)
    print(f"  Files restored : {n}")
    print(f"  Destination    : {dest_dir}")
    print(f"  Restored from  : {backup_path.name}")
    print(f"  Timestamp      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
