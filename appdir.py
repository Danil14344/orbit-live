"""Single source of truth for the app's on-disk home directory.

In a PyInstaller frozen build each subprocess (`eyecrypt-bot.exe --mode X`)
unpacks to its OWN temporary `_MEIPASS` folder, so `Path(__file__).parent`
differs per process and points into temp — files written there are invisible
to sibling processes and are wiped on exit. Always resolve runtime files
(logs, heartbeat, .env, .device_id, trades.jsonl, state) against this stable
base instead: the folder the .exe lives in (frozen) or the project dir (dev).
"""
import sys
from pathlib import Path


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR = base_dir()
