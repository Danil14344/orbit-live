"""Eye Crypt license client.

Bot startup: verify_or_exit() — checks JWT against backend, returns tier.
Background: status_reporter() — pings every 5 min with current PnL/trades.
Auto-exits bot if subscription lapses.
"""
import asyncio
import hashlib
import os
import sys
import time
import json
import uuid
from pathlib import Path
import httpx

from appdir import BASE_DIR
from logsetup import get_logger
log = get_logger("license")

API_BASE = os.getenv("EYECRYPT_API", "https://api.eyecryptbot.com")
LICENSE = os.getenv("EYECRYPT_LICENSE", "")
PING_INTERVAL_SEC = 300         # 5 min
REVERIFY_INTERVAL_SEC = 3600    # re-check subscription every hour

# Placeholder values written into a fresh .env (eyecrypt_main._ensure_env). These must
# be treated as "no license" — otherwise the bot tries to verify the placeholder, gets
# rejected, and (in hard mode) exits in a crash-loop, so paper trading never starts.
_PLACEHOLDERS = {
    "", "paste-your-license-here", "paste-your-license",
    "your-license-here", "your-license", "changeme",
}


def is_placeholder_license(value: str | None) -> bool:
    return (value or "").strip() in _PLACEHOLDERS

_current = {"tier": 0, "valid": False, "expires_at": None}
_DEVICE_ID_FILE = BASE_DIR / ".device_id"


def _machine_fingerprint() -> str:
    """Stable per-machine identifier — Windows MachineGuid + first MAC.
    Falls back gracefully on non-Windows or restricted environments."""
    parts: list[str] = []
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as k:
                guid, _ = winreg.QueryValueEx(k, "MachineGuid")
                parts.append(str(guid))
        except Exception:
            pass
    try:
        parts.append(f"{uuid.getnode():012x}")
    except Exception:
        pass
    if not parts:
        parts.append(uuid.uuid4().hex)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def _device_id() -> str:
    """Read cached device_id or compute and persist a fresh one."""
    try:
        if _DEVICE_ID_FILE.exists():
            v = _DEVICE_ID_FILE.read_text(encoding="utf-8").strip()
            if v:
                return v
    except Exception:
        pass
    v = _machine_fingerprint()
    try:
        _DEVICE_ID_FILE.write_text(v, encoding="utf-8")
    except Exception as e:
        log.warning(f"could not persist device_id: {e}")
    return v


def _hdr():
    return {"Authorization": f"Bearer {LICENSE}", "X-Device-Id": _device_id()}


def current_tier() -> int:
    return _current["tier"] if _current["valid"] else 0


def verify_or_exit(hard: bool = True) -> int:
    """Synchronous startup license check.

    hard=True  (live mode): exits the process if the license is missing/invalid.
    hard=False (paper mode): never exits — logs a warning and returns tier 0 so paper
               (simulation) trading always starts, even with no/placeholder/expired key.
    """
    def _fail(msg: str) -> int:
        log.error(msg)
        if hard:
            sys.exit(1)
        log.warning("running in PAPER mode without a valid license — simulation only")
        return 0

    if is_placeholder_license(LICENSE):
        return _fail("EYECRYPT_LICENSE not set — get your key from eyecryptbot.com/cabinet")
    try:
        r = httpx.get(f"{API_BASE}/v1/verify", headers=_hdr(), timeout=10)
    except Exception as e:
        return _fail(f"license server unreachable: {e}")
    if r.status_code == 403:
        try:
            d = r.json().get("detail", {})
            if isinstance(d, dict) and d.get("error") == "device_mismatch":
                return _fail("license is already activated on another device. Reset the "
                             "binding in your cabinet (eyecryptbot.com/cabinet) then restart.")
        except Exception:
            pass
        return _fail(f"license rejected (403): {r.text}")
    if r.status_code != 200:
        return _fail(f"license rejected ({r.status_code}): {r.text}")
    data = r.json()
    if not data.get("valid"):
        return _fail(f"subscription not active. Expired: {data.get('expires_at')}. "
                     "Renew at https://eyecryptbot.com/pricing")
    _current.update(tier=data["tier"], valid=True, expires_at=data["expires_at"])
    log.info(f"license OK — tier {data['tier']}, expires {data['expires_at']}")
    return data["tier"]


_last_uploaded_ts = 0.0


def _read_recent_trades(since_ts: float, max_n: int = 200) -> list[dict]:
    """Read trades from local trades.jsonl that happened after since_ts."""
    path = str(BASE_DIR / "trades.jsonl")
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts", 0) <= since_ts:
                    continue
                out.append({
                    "ts": float(r["ts"]),
                    "symbol": r.get("symbol", "?"),
                    "buy_ex": r.get("buy_ex", "?"),
                    "sell_ex": r.get("sell_ex", "?"),
                    "base_qty": float(r.get("base_filled") or 0),
                    "pnl_usd": float(r.get("actual_pnl_usd") or 0),
                    "net_pct": float(r.get("actual_net_pct") or 0),
                    "status": r.get("status", "ok"),
                    "latency_ms": float(r.get("exec_latency_ms") or 0),
                })
    except Exception as e:
        log.warning(f"read trades.jsonl failed: {e}")
    return out[-max_n:]


async def _upload_trades():
    global _last_uploaded_ts
    recent = _read_recent_trades(_last_uploaded_ts, max_n=200)
    if not recent:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as cli:
            r = await cli.post(f"{API_BASE}/v1/bot/upload-trades",
                               headers=_hdr(), json={"trades": recent})
        if r.status_code == 200:
            _last_uploaded_ts = max(t["ts"] for t in recent)
        else:
            log.debug(f"trade upload {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log.debug(f"trade upload failed: {e}")


async def status_reporter(executor=None, guard=None, hedge=None):
    """Background task — reports bot status to backend every PING_INTERVAL_SEC.
    Also re-verifies subscription hourly; on expiry → exits."""
    last_reverify = time.time()
    log.info(f"status reporter started (ping {PING_INTERVAL_SEC}s, reverify {REVERIFY_INTERVAL_SEC}s)")
    while True:
        try:
            extra = {}
            if hedge is not None:
                extra["hedge"] = {
                    "realized": getattr(hedge, "realized_pnl_usd", 0),
                    "funding": getattr(hedge, "funding_paid_usd", 0),
                }
            payload = {
                "is_online": True,
                "pnl_usd": float(getattr(executor, "daily_pnl_usd", 0) or 0),
                "trades_count": int(getattr(executor, "total_trades", 0) or 0),
                "stops_count": int(getattr(guard, "stops_triggered", 0) or 0),
                "extra_json": json.dumps(extra) if extra else None,
            }
            async with httpx.AsyncClient(timeout=10) as cli:
                r = await cli.post(f"{API_BASE}/v1/bot/ping", headers=_hdr(), json=payload)
            if r.status_code == 403:
                log.error("subscription expired during runtime — shutting down")
                os._exit(2)
            elif r.status_code != 200:
                log.warning(f"ping {r.status_code}: {r.text[:100]}")
            else:
                # Also upload recent trades for cabinet visibility
                await _upload_trades()
        except Exception as e:
            log.warning(f"ping failed: {e}")

        # Periodic re-verify
        if time.time() - last_reverify > REVERIFY_INTERVAL_SEC:
            try:
                async with httpx.AsyncClient(timeout=10) as cli:
                    rv = await cli.get(f"{API_BASE}/v1/verify", headers=_hdr())
                if rv.status_code == 200 and rv.json().get("valid"):
                    _current.update(tier=rv.json()["tier"], valid=True,
                                    expires_at=rv.json()["expires_at"])
                else:
                    log.error("re-verify failed — shutting down")
                    os._exit(2)
            except Exception as e:
                log.warning(f"reverify error: {e}")
            last_reverify = time.time()

        await asyncio.sleep(PING_INTERVAL_SEC)
