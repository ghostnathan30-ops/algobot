"""
AlgoBot — Unified Logging Module
=================================
Module:  src/utils/logger.py
Phase:   1 — Data Infrastructure
Purpose: Provides a single, consistent logging interface for the entire
         AlgoBot codebase. All modules import get_logger() from here.

Design decisions:
  - Uses loguru (superior to stdlib logging for simplicity and features)
  - Writes to both console (INFO+) and rotating file (DEBUG+)
  - Automatically sanitizes credentials from all log messages
  - One configuration point — change behavior here, affects all modules

Usage:
    from src.utils.logger import get_logger
    log = get_logger(__name__)
    log.info("Starting data download for ES")
    log.debug("ATR calculated: {atr:.2f}", atr=35.4)
    log.warning("Missing bars detected in GC data: 3 gaps found")
    log.error("FRED API key not configured")
    log.critical("Daily loss limit breached — emergency stop triggered")
"""

import re
import sys
from pathlib import Path

from loguru import logger


# ── Constants ─────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent.parent
LOG_DIR      = PROJECT_ROOT / "logs"
LOG_FILE     = LOG_DIR / "algobot.log"

# Format strings
CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{extra[module]:<25}</cyan> | "
    "<level>{message}</level>"
)

FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
    "{level: <8} | "
    "{extra[module]:<25} | "
    "{message}"
)

# Credential patterns to redact from all log output
# These patterns match common accidental credential logging
_REDACT_PATTERNS = [
    (r'(?i)(api[_-]?key\s*[=:]\s*)\S+',         r'\1REDACTED'),
    (r'(?i)(password\s*[=:]\s*)\S+',              r'\1REDACTED'),
    (r'(?i)(token\s*[=:]\s*)\S+',                 r'\1REDACTED'),
    (r'(?i)(secret\s*[=:]\s*)\S+',                r'\1REDACTED'),
    (r'(?i)(authorization\s*[=:]\s*)\S+',         r'\1REDACTED'),
    (r'(?i)(bearer\s+)[A-Za-z0-9\-_.~+/]+=*',    r'\1REDACTED'),
]


# ── Internal state ────────────────────────────────────────────────────────────

_configured = False   # Ensures loguru is only configured once per process


# ── Sanitization ──────────────────────────────────────────────────────────────

def sanitize_message(message: str) -> str:
    """
    Remove any accidentally included credentials from a log message string.

    Applies all patterns in _REDACT_PATTERNS. This is a last-resort safety net;
    credentials should never be passed to the logger in the first place.

    Args:
        message: Raw log message string.

    Returns:
        Message with any matched credential patterns replaced with REDACTED.

    Example:
        sanitize_message("Connecting with api_key=abc123xyz")
        → "Connecting with api_key=REDACTED"
    """
    for pattern, replacement in _REDACT_PATTERNS:
        message = re.sub(pattern, replacement, message)
    return message


def _sanitizing_sink(message):
    """
    Custom loguru sink that sanitizes messages before writing to console.
    This wraps the standard stderr sink with credential redaction.
    """
    record  = message.record
    raw_msg = record["message"]
    sanitized = sanitize_message(raw_msg)
    record["message"] = sanitized
    sys.stderr.write(message)


# ── Configuration ─────────────────────────────────────────────────────────────

def _configure_logger() -> None:
    """
    Configure loguru with console and file sinks.
    Called automatically on first get_logger() call.
    Only runs once per process (guarded by _configured flag).
    """
    global _configured
    if _configured:
        return

    # Ensure log directory exists
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Remove loguru's default handler (plain stderr, no formatting)
    logger.remove()

    # ── Console sink ──────────────────────────────────────────────────────────
    # Shows INFO and above. Colored. Sanitized via custom sink.
    logger.add(
        sys.stdout,
        format=CONSOLE_FORMAT,
        level="INFO",
        colorize=True,
        backtrace=True,
        diagnose=False,  # False in prod: prevents variable values from leaking
        filter=lambda record: record["level"].no >= logger.level("INFO").no,
    )

    # ── File sink ─────────────────────────────────────────────────────────────
    # Shows DEBUG and above (everything). Rotating. No color codes in file.
    # Rotation: new file when current exceeds 10 MB. Keep 7 rotations.
    logger.add(
        str(LOG_FILE),
        format=FILE_FORMAT,
        level="DEBUG",
        rotation="10 MB",
        retention=7,
        compression="gz",
        encoding="utf-8",
        backtrace=True,
        diagnose=False,
        enqueue=True,    # Thread-safe writes from multiple bot threads
        filter=lambda record: sanitize_message(record["message"]) or True,
    )

    _configured = True


# ── Public API ────────────────────────────────────────────────────────────────

def get_logger(name: str):
    """
    Get a configured logger instance bound to a module name.

    This is the ONLY function other modules should import from this file.
    Call it once per module at the top of the file.

    Args:
        name: Module identifier string. Use __name__ for automatic naming.
              Examples: "src.utils.data_downloader", "src.strategy.tma_signal"

    Returns:
        A loguru logger instance with the 'module' context bound.
        All messages from this logger will be tagged with the given name.

    Example:
        from src.utils.logger import get_logger
        log = get_logger(__name__)
        log.info("Data download started")
        log.debug("Downloaded {n} bars for {market}", n=5000, market="ES")
        log.error("Connection failed: {err}", err=str(e))
    """
    _configure_logger()

    # Shorten long module paths for cleaner log output
    # "src.utils.data_downloader" → "data_downloader"
    short_name = name.split(".")[-1] if "." in name else name

    return logger.bind(module=short_name)


# ── Convenience loggers ───────────────────────────────────────────────────────

def get_trade_logger():
    """
    Get a logger specifically for trade execution events.
    These always go to a separate trades.log file in addition to main log.
    Used exclusively by src/live/order_manager.py.
    """
    _configure_logger()

    trades_log = LOG_DIR / "trades.log"
    logger.add(
        str(trades_log),
        format=FILE_FORMAT,
        level="INFO",
        rotation="1 week",
        retention=12,
        compression="gz",
        filter=lambda record: record["extra"].get("trade_event", False),
    )

    return logger.bind(module="TRADE", trade_event=True)
