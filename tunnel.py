"""Cloudflare quick-tunnel helper for the local dashboard (Mini App host).

Quick tunnels (trycloudflare.com) get a NEW random URL on every restart, so the
public address can't be hard-coded. This module:
  - launches `cloudflared tunnel --url http://localhost:<port>` (if the binary
    is available), logging to a known file;
  - parses the assigned https://*.trycloudflare.com URL out of that log;
  - writes it into .env as TELEGRAM_MINIAPP_URL so the Telegram bot picks it up.

The Telegram bot also re-reads the log periodically and refreshes its Mini App
menu button when the URL changes.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from logsetup import get_logger

log = get_logger("tunnel")

ROOT = Path(__file__).parent
DASHBOARD_PORT = 8765
TUNNEL_LOG = ROOT / "dashboard_tunnel.log"
# Named-tunnel config (your own domain). If this file exists, it takes priority
# over the ephemeral quick tunnel — the public URL is then fixed to your domain.
NAMED_CONFIG = ROOT / "cloudflared-config.yml"
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# Log files the URL may appear in, newest-wins. Dashboard tunnel first; the
# Supervisor-managed variant writes to logs/tunnel.stdout.log.
DEFAULT_LOG_CANDIDATES = [
    TUNNEL_LOG,
    ROOT / "logs" / "tunnel.stdout.log",
]


def find_cloudflared() -> str | None:
    """Locate the cloudflared binary on PATH, via env, or common install dirs."""
    env_path = os.getenv("CLOUDFLARED_PATH")
    if env_path and Path(env_path).exists():
        return env_path
    found = shutil.which("cloudflared")
    if found:
        return found
    candidates = [
        ROOT / "cloudflared.exe",
        Path(os.getenv("LOCALAPPDATA", "")) / "cloudflared" / "cloudflared.exe",
        Path("C:/Program Files (x86)/cloudflared/cloudflared.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def tunnel_command(port: int = DASHBOARD_PORT) -> list[str] | None:
    """Command to launch a quick (ephemeral) tunnel for the dashboard, or None."""
    cf = find_cloudflared()
    if not cf:
        return None
    return [cf, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]


def named_tunnel_command() -> list[str] | None:
    """Command to run a persistent named tunnel from cloudflared-config.yml
    (your own domain), or None if the binary or config is missing."""
    cf = find_cloudflared()
    if not cf or not NAMED_CONFIG.exists():
        return None
    return [cf, "tunnel", "--no-autoupdate", "--config", str(NAMED_CONFIG), "run"]


def best_tunnel_command() -> tuple[list[str] | None, str]:
    """Prefer the named tunnel (fixed domain) over the quick tunnel.
    Returns (cmd, kind) where kind is 'named', 'quick', or 'none'."""
    named = named_tunnel_command()
    if named:
        return named, "named"
    quick = tunnel_command()
    if quick:
        return quick, "quick"
    return None, "none"


def read_tunnel_url(*log_paths) -> str | None:
    """Return the most recently logged trycloudflare URL, or None."""
    paths = [Path(p) for p in (log_paths or DEFAULT_LOG_CANDIDATES)]
    best_url, best_mtime = None, -1.0
    for p in paths:
        try:
            if not p.exists():
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            matches = _URL_RE.findall(text)
            if matches:
                mtime = p.stat().st_mtime
                if mtime > best_mtime:
                    best_mtime, best_url = mtime, matches[-1]
        except Exception as e:
            log.debug(f"read {p} failed: {e}")
    return best_url


def update_env_var(key: str, value: str, env_path=None) -> bool:
    """Set/replace KEY=value in .env, preserving comments and order. Returns True if changed."""
    p = Path(env_path) if env_path else (ROOT / ".env")
    lines = []
    seen = False
    changed = False
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#") and line.split("=", 1)[0].strip() == key:
                seen = True
                if line.split("=", 1)[1].strip() != value:
                    changed = True
                lines.append(f"{key}={value}")
            else:
                lines.append(line)
    if not seen:
        lines.append(f"{key}={value}")
        changed = True
    if changed:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def sync_miniapp_url(log_paths=None, env_path=None) -> str | None:
    """Read the current tunnel URL and persist it to .env. Returns the URL."""
    url = read_tunnel_url(*(log_paths or [])) if log_paths else read_tunnel_url()
    if url:
        if update_env_var("TELEGRAM_MINIAPP_URL", url, env_path):
            log.info(f"TELEGRAM_MINIAPP_URL updated → {url}")
    return url


if __name__ == "__main__":
    # Standalone: launch the dashboard tunnel and stream URL into .env.
    cmd = tunnel_command()
    if not cmd:
        print("cloudflared not found. Install it or set CLOUDFLARED_PATH.", file=sys.stderr)
        sys.exit(1)
    print(f"launching: {' '.join(cmd)}")
    with open(TUNNEL_LOG, "w", encoding="utf-8") as f:
        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=ROOT)
        import time
        for _ in range(30):
            time.sleep(1)
            u = sync_miniapp_url()
            if u:
                print(f"tunnel up: {u}")
                break
        proc.wait()
