"""Health watchdog — restarts ws_scanner.py and dashboard.py if they die.

Run: python watchdog.py
Logs to logs/watchdog.log.
Stops bots cleanly on Ctrl+C.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from appdir import BASE_DIR
from logsetup import init_logging, get_logger

init_logging("watchdog")
log = get_logger("watchdog")

ROOT = BASE_DIR
PYTHON = sys.executable
_FROZEN = getattr(sys, "frozen", False)


def _child_cmd(mode: str) -> list[str]:
    """Same exe in frozen mode (--mode X), python + script in dev."""
    if _FROZEN:
        return [PYTHON, "--mode", mode]
    script = {"scanner": "ws_scanner.py", "dashboard": "dashboard.py"}[mode]
    return [PYTHON, str(ROOT / script)]


PROCESSES = {
    "scanner": _child_cmd("scanner"),
    "dashboard": _child_cmd("dashboard"),
}

# Mini App needs a public HTTPS endpoint for the dashboard — launch a Cloudflare
# quick tunnel if cloudflared is available. DISABLED by default: the tunnel would
# expose the unauthenticated localhost dashboard to the public internet. Opt in with
# ENABLE_TUNNEL=1 only if you actually use the Mini App remotely.
if os.getenv("ENABLE_TUNNEL", "0").lower() in ("1", "true", "yes"):
    try:
        import tunnel as _tunnel
        _tunnel_cmd, _tunnel_kind = _tunnel.best_tunnel_command()
        if _tunnel_cmd:
            PROCESSES["tunnel"] = _tunnel_cmd
            if _tunnel_kind == "named":
                log.info("cloudflared + cloudflared-config.yml found — named tunnel (own domain) supervised")
            else:
                log.info("cloudflared found — ephemeral quick tunnel supervised")
        else:
            log.info("cloudflared not found — Mini App tunnel disabled")
    except Exception as _e:
        log.debug(f"tunnel setup skipped: {_e}")
else:
    log.info("tunnel disabled (ENABLE_TUNNEL not set) — dashboard stays local-only")

CHECK_INTERVAL = 5
RESTART_BACKOFF = [5, 15, 30, 60, 120, 300]  # seconds, capped
HEARTBEAT_FILE = ROOT / "scanner_heartbeat.txt"
HEARTBEAT_MAX_AGE = 90   # seconds — restart scanner if heartbeat older
LIVENESS_CHECK_INTERVAL = 60  # check heartbeat once per minute


class Supervisor:
    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.fail_counts: dict[str, int] = {n: 0 for n in PROCESSES}
        self.last_restart_ts: dict[str, float] = {n: 0 for n in PROCESSES}
        self.restart_not_before: dict[str, float] = {n: 0 for n in PROCESSES}
        self.last_liveness_check = 0.0
        self.stopping = False

    def check_liveness(self):
        """Detect hung scanner — process alive but no heartbeat updates."""
        if time.time() - self.last_liveness_check < LIVENESS_CHECK_INTERVAL:
            return
        self.last_liveness_check = time.time()
        proc = self.procs.get("scanner")
        if proc is None or proc.poll() is not None:
            return
        # Give scanner grace after restart
        if time.time() - self.last_restart_ts.get("scanner", 0) < 60:
            return
        try:
            hb = HEARTBEAT_FILE.stat().st_mtime
        except FileNotFoundError:
            log.warning("scanner heartbeat missing; killing for restart")
            try: proc.kill()
            except Exception: pass
            return
        age = time.time() - hb
        if age > HEARTBEAT_MAX_AGE:
            log.error(f"scanner heartbeat stale ({age:.0f}s > {HEARTBEAT_MAX_AGE}s); killing for restart")
            try: proc.kill()
            except Exception: pass
        else:
            log.info(f"liveness ok: scanner heartbeat {age:.0f}s old")

    def start(self, name):
        cmd = PROCESSES[name]
        log_path = ROOT / "logs" / f"{name}.stdout.log"
        log_path.parent.mkdir(exist_ok=True)
        f = open(log_path, "ab")
        # CREATE_NO_WINDOW: children write to log files, so suppress the console
        # window that would otherwise flash on every (re)start. Keep NEW_PROCESS_GROUP
        # for CTRL_BREAK delivery; stop() falls back to terminate/kill if that fails.
        _flags = 0
        if os.name == "nt":
            _flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(
            cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
            creationflags=_flags,
        )
        self.procs[name] = proc
        self.last_restart_ts[name] = time.time()
        log.info(f"started {name} pid={proc.pid}")

    def stop(self, name):
        proc = self.procs.get(name)
        if proc and proc.poll() is None:
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                log.info(f"stopped {name}")
            except Exception as e:
                log.exception(f"stop {name} failed: {e}")

    def check_all(self):
        for name in PROCESSES:
            proc = self.procs.get(name)
            if proc is None:
                self.start(name)
                continue
            if proc.poll() is not None:
                # Process is dead. Apply a non-blocking backoff: schedule the next
                # allowed restart time and move on, so a long backoff on one child
                # doesn't freeze supervision of the others (or the liveness check).
                now = time.time()
                if self.restart_not_before[name] == 0:
                    exit_code = proc.returncode
                    self.fail_counts[name] += 1
                    idx = min(self.fail_counts[name] - 1, len(RESTART_BACKOFF) - 1)
                    wait = RESTART_BACKOFF[idx]
                    log.error(f"{name} died (exit={exit_code}, fail#{self.fail_counts[name]}). Restart in {wait}s")
                    self.restart_not_before[name] = now + wait
                    continue
                if now < self.restart_not_before[name]:
                    continue
                if self.stopping:
                    return
                self.restart_not_before[name] = 0
                self.start(name)
            else:
                # Healthy run resets fail count after 5 min uptime
                if time.time() - self.last_restart_ts[name] > 300:
                    if self.fail_counts[name] > 0:
                        log.info(f"{name} stable >5min, resetting fail counter")
                        self.fail_counts[name] = 0

    def shutdown(self):
        self.stopping = True
        log.warning("shutdown initiated")
        for name in list(self.procs.keys()):
            self.stop(name)


def main():
    sup = Supervisor()

    def handle_sig(sig, frame):
        log.warning(f"signal {sig} received")
        sup.shutdown()
        sys.exit(0)

    if os.name != "nt":
        signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGINT, handle_sig)

    log.info(f"watchdog starting; supervising: {list(PROCESSES.keys())}")
    try:
        while True:
            sup.check_all()
            sup.check_liveness()
            time.sleep(CHECK_INTERVAL)
    except KeyboardInterrupt:
        sup.shutdown()


if __name__ == "__main__":
    main()
