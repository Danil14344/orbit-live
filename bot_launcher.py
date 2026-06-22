"""Single-executable entry point for the bot (used by PyInstaller).
- Auto-creates .env from defaults if missing
- Opens browser to dashboard
- Starts watchdog (which spawns scanner + dashboard)
"""
import os
import sys
import time
import webbrowser
from pathlib import Path


def _resource_path(rel):
    """Path to resource (works both in dev and PyInstaller bundle)."""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / rel
    return Path(__file__).parent / rel


def main():
    # Resolve working directory — for PyInstaller bundled exe, use the folder
    # the .exe lives in (so user's .env + state files persist next to it)
    here = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    os.chdir(here)

    env_path = here / ".env"
    if not env_path.exists():
        env_path.write_text(
            "EYECRYPT_LICENSE=paste-your-license-here\n"
            "EYECRYPT_API=https://api.eyecryptbot.com\n"
            "MODE=paper\n",
            encoding="utf-8",
        )
        print("=" * 60)
        print("  WELCOME TO EYE CRYPT BOT")
        print("=" * 60)
        print(f"  A new .env file was created at: {env_path}")
        print(f"  Please paste your license key from your cabinet.")
        print(f"  Then re-run this program.")
        print("=" * 60)
        try:
            os.startfile(str(env_path))  # opens in default editor on Windows
        except Exception:
            pass
        input("Press Enter to exit...")
        return 1

    # Open dashboard in browser shortly after startup
    def _open_browser():
        time.sleep(8)
        try:
            webbrowser.open("http://localhost:8765")
        except Exception:
            pass

    import threading
    threading.Thread(target=_open_browser, daemon=True).start()

    print("Starting Eye Crypt bot...")
    print("Dashboard will open at http://localhost:8765")
    print("Close this window to stop the bot.")
    print("")

    # Run watchdog directly in this process
    import watchdog
    watchdog.main()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
