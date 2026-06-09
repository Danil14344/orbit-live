"""Eye Crypt Bot — in-place updater.

Downloads a new exe from the URL given by the dashboard, then writes a small
.bat helper that waits for the running app to exit, swaps the file, and re-launches.

Called by dashboard.py when user clicks "Update now".
"""
import hashlib
import os
import sys
import time
import shutil
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent


def download(url: str, dest: Path) -> str:
    """Download `url` to `dest`, returning the hex sha256 of the bytes written."""
    h = hashlib.sha256()
    with httpx.stream("GET", url, timeout=120, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
                    h.update(chunk)
    return h.hexdigest()


def apply_update(download_url: str, current_exe: Path | None = None,
                 expected_sha256: str | None = None) -> Path:
    """Download new exe into tmp and write updater.bat that swaps it on next launch.
    If `expected_sha256` is given, the download must match it or the update aborts.
    Returns the path to the helper batch file."""
    is_frozen = getattr(sys, "frozen", False)
    target = current_exe or Path(sys.executable if is_frozen else (ROOT / "eyecrypt-bot.exe"))

    tmp_dir = Path(tempfile.mkdtemp(prefix="eyecrypt_upd_"))
    new_exe = tmp_dir / "eyecrypt-bot.new.exe"
    got_sha = download(download_url, new_exe)
    if expected_sha256:
        if got_sha.lower() != expected_sha256.strip().lower():
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            finally:
                pass
            raise ValueError(
                f"update sha256 mismatch: expected {expected_sha256[:12]}…, got {got_sha[:12]}…"
            )

    swap_bat = tmp_dir / "swap.bat"
    log_path = tmp_dir / "swap.log"
    exe_name = target.name  # e.g. "eyecrypt-bot.exe"
    # The running .exe spawns several processes (watchdog + scanner + dashboard),
    # ALL of which lock the binary. We must kill every instance before the swap,
    # otherwise move fails and the watchdog respawns the old version. taskkill /F
    # also kills the watchdog so nothing restarts mid-swap. The move is retried in
    # case the OS is slow to release the lock after the kill.
    swap_bat.write_text(
        "@echo off\r\n"
        f'echo [%date% %time%] swap start, killing {exe_name} ^> "{log_path}"\r\n'
        f'taskkill /F /IM "{exe_name}" >> "{log_path}" 2>&1\r\n'
        "set /a tries=0\r\n"
        ":retry\r\n"
        "set /a tries+=1\r\n"
        "timeout /t 2 /nobreak > nul\r\n"
        f'move /Y "{new_exe}" "{target}" >> "{log_path}" 2>&1\r\n'
        f'if exist "{new_exe}" (\r\n'
        "  if %tries% lss 15 goto retry\r\n"
        f'  echo [%date% %time%] FAILED after %tries% tries ^>^> "{log_path}"\r\n'
        "  exit /b 1\r\n"
        ")\r\n"
        f'echo [%date% %time%] swap ok after %tries% tries, relaunching ^>^> "{log_path}"\r\n'
        f'start "" "{target}"\r\n',
        encoding="utf-8",
    )
    return swap_bat


if __name__ == "__main__":
    # Manual usage: py -3 updater.py <url>
    if len(sys.argv) < 2:
        print("usage: py -3 updater.py <download_url>")
        sys.exit(2)
    bat = apply_update(sys.argv[1])
    print(f"Update prepared. Launching swap helper: {bat}")
    os.startfile(str(bat))
