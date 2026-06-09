"""Eye Crypt — unified entry point.

Single executable (after PyInstaller) that dispatches to one of several modes:

  --mode app        (default) Launch desktop window + watchdog + dashboard
  --mode dashboard  Run only the HTTP dashboard on :8765
  --mode scanner    Run the WebSocket scanner
  --mode watchdog   Run the supervisor (which spawns dashboard + scanner)

The watchdog and the launcher both use sys.executable + --mode flag to spawn
children, so the same .exe is reused — no need to bundle Python scripts.
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path


def _here() -> Path:
    """Working directory the app should treat as its home."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _ensure_env():
    here = _here()
    env_path = here / ".env"
    if not env_path.exists():
        env_path.write_text(
            "EYECRYPT_LICENSE=paste-your-license-here\n"
            "EYECRYPT_API=https://api.eyecryptbot.com\n"
            "MODE=paper\n",
            encoding="utf-8",
        )
        try:
            os.startfile(str(env_path))
        except Exception:
            pass


def _chdir_here():
    os.chdir(_here())


def main():
    parser = argparse.ArgumentParser(prog="eyecrypt-bot")
    parser.add_argument("--mode", default="app",
                        choices=["app", "dashboard", "scanner", "watchdog"])
    args, _ = parser.parse_known_args()

    _chdir_here()
    _ensure_env()

    if args.mode == "app":
        import eyecrypt_app
        eyecrypt_app.main()
    elif args.mode == "dashboard":
        import dashboard
        import uvicorn
        # Bind loopback only: the dashboard is unauthenticated and exposes the
        # license key + bot controls. A LAN-reachable bind (0.0.0.0) would leak
        # those to anyone on the same network. The optional cloudflared tunnel
        # connects to localhost, so remote Mini App access still works.
        uvicorn.run(dashboard.app, host="127.0.0.1", port=8765, log_level="warning")
    elif args.mode == "scanner":
        import ws_scanner
        asyncio.run(ws_scanner.main())
    elif args.mode == "watchdog":
        import watchdog
        watchdog.main()


if __name__ == "__main__":
    main()
