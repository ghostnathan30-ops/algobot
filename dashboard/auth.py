"""
AlgoBot -- Dashboard Authentication
=====================================
JWT-based auth with bcrypt password hashing.
Credentials stored in dashboard/config/auth.json (never in code).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt as _bcrypt
from jose import JWTError, jwt

CONFIG_FILE = Path(__file__).parent / "config" / "auth.json"

TOKEN_COOKIE  = "algobot_token"
TOKEN_EXPIRE_H = 10    # session lasts 10 hours
ALGORITHM     = "HS256"

# ── Load credentials ──────────────────────────────────────────────────────────

def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def auth_configured() -> bool:
    cfg = _load_config()
    return bool(cfg.get("username") and cfg.get("password_hash") and cfg.get("secret_key"))


def _secret_key() -> str:
    return _load_config().get("secret_key", "INSECURE_FALLBACK_KEY_CHANGE_ME")


# ── Password verification ─────────────────────────────────────────────────────

def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def check_credentials(username: str, password: str) -> bool:
    cfg = _load_config()
    if not cfg:
        return False
    if username != cfg.get("username"):
        return False
    return verify_password(password, cfg.get("password_hash", ""))


# ── JWT tokens ────────────────────────────────────────────────────────────────

def create_token(username: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_H)
    payload = {"sub": username, "exp": exp}
    return jwt.encode(payload, _secret_key(), algorithm=ALGORITHM)


def verify_token(token: str) -> Optional[str]:
    """Return username if token is valid, else None."""
    try:
        payload = jwt.decode(token, _secret_key(), algorithms=[ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None
