"""Eye Crypt Bot — desktop launcher.

Starts the bot subprocess (watchdog) and the dashboard (HTTP :8765),
then opens a native window pointing to localhost:8765 via pywebview.
On window close → all child processes are killed cleanly.

Run:  py -3 eyecrypt_app.py
Build:  Build-Exe.bat  (PyInstaller)
"""
import os
import sys
import time
import socket
import subprocess
import threading
import signal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "Eye Crypt Bot"
APP_VERSION = "1.0.8"   # keep in sync with dashboard.py VERSION
DASHBOARD_PORT = 8765
DASHBOARD_URL = f"http://127.0.0.1:{DASHBOARD_PORT}"


def _is_port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _spawn_mode(mode: str) -> subprocess.Popen | None:
    """Spawn ourselves in a child mode (frozen) or python+script (dev)."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--mode", mode]
    else:
        script = {"dashboard": "dashboard.py", "watchdog": "watchdog.py"}.get(mode)
        if not script or not (ROOT / script).exists():
            return None
        cmd = [sys.executable, "-u", str(ROOT / script)]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(
        cmd, cwd=str(ROOT), creationflags=creationflags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


_children: list[subprocess.Popen] = []


def start_background_services():
    """Start dashboard + watchdog subprocesses. Idempotent (skips if port busy)."""
    if not _is_port_open(DASHBOARD_PORT):
        p = _spawn_mode("dashboard")
        if p: _children.append(p)
    for _ in range(40):
        if _is_port_open(DASHBOARD_PORT):
            break
        time.sleep(0.25)
    p = _spawn_mode("watchdog")
    if p: _children.append(p)


def shutdown_services():
    for p in _children:
        try:
            if os.name == "nt":
                p.send_signal(signal.CTRL_BREAK_EVENT)   # may fail silently — fine
            p.terminate()
        except Exception:
            pass
    for p in _children:
        try:
            p.wait(timeout=3)
        except Exception:
            try: p.kill()
            except Exception: pass


# ────────────────────────── system tray (optional) ──────────────────────────

def make_tray_icon(window):
    """System tray icon — minimize-to-tray instead of closing.
    Falls back gracefully if pystray/Pillow not available."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    # Generate simple violet icon at runtime (no external file needed)
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(139, 92, 246, 255))
    d.text((19, 14), "E", fill=(255, 255, 255), font=None)

    def on_show(icon, _item): window.show()
    def on_quit(icon, _item):
        icon.stop(); shutdown_services()
        try: window.destroy()
        except Exception: pass
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open", on_show, default=True),
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon(APP_NAME, img, APP_NAME, menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


# ────────────────────────── main ──────────────────────────

def main():
    start_background_services()

    # Try PyWebView (native window); fall back to default browser
    try:
        import webview   # type: ignore
    except ImportError:
        print("[launcher] pywebview not installed, opening default browser")
        import webbrowser
        webbrowser.open(DASHBOARD_URL)
        # Keep process alive so children stay
        try:
            while True: time.sleep(60)
        except KeyboardInterrupt:
            pass
        shutdown_services()
        return

    window = webview.create_window(
        title=APP_NAME + f" v{APP_VERSION}",
        url=DASHBOARD_URL,
        width=1280, height=820,
        min_size=(900, 600),
        background_color="#07060d",
        text_select=True,
    )
    tray = make_tray_icon(window)
    if tray:
        window.events.closing += lambda: (window.hide(), False)[1]   # hide instead of close

    try:
        webview.start(debug=False)
    finally:
        shutdown_services()


if __name__ == "__main__":
    main()
