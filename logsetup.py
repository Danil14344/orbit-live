"""Central logging setup. All modules: `from logsetup import get_logger; log = get_logger(__name__)`.

Each process writes to its own rotating file under logs/, and to stderr at INFO+.
Errors include full traceback. Exceptions in tasks/handlers won't be silently swallowed.
"""
import logging
import logging.handlers
import os
import sys
from pathlib import Path

from appdir import BASE_DIR

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)-22s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def init_logging(process_name: str, level: str = "INFO"):
    """Call once per process. Sets up root logger with file + stderr handlers."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # File: rotating, 10MB × 5 files
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{process_name}.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    # File level configurable via LOG_FILE_LEVEL (default INFO). DEBUG floods the
    # file with httpcore/httpx plumbing and rotates 10MB every few hours.
    _file_level = os.getenv("LOG_FILE_LEVEL", "INFO")
    file_handler.setLevel(getattr(logging, _file_level.upper(), logging.INFO))
    root.addHandler(file_handler)

    # Stderr: INFO+ (won't break rich.Live which uses stdout)
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(stream_handler)

    # Tame noisy libraries
    # NB: do NOT silence "telegram" — that is this app's OWN logger name
    # (get_logger("telegram")), not a third-party lib. The bot talks to the
    # Telegram API directly over httpx, so the real noise is httpcore/httpx.
    for noisy in (
        "ccxt", "ccxt.pro", "aiohttp", "urllib3", "uvicorn.access",
        "httpcore", "httpx", "httpcore.http11", "httpcore.connection",
        "hpack", "websockets", "asyncio",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
