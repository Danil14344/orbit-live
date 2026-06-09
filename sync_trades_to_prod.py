"""Read trades.jsonl and push new rows to production backend.
Run periodically (e.g. every 5 min) to keep /v1/public/performance fresh.

Usage:
    py -3 sync_trades_to_prod.py [--once]

Tracks last-uploaded ts in `.last_sync_ts` to avoid duplicates.
"""
import json
import os
import sys
import time
from pathlib import Path

import httpx

API_URL = os.getenv("EYECRYPT_API", "https://api.eyecryptbot.com")
TOKEN   = os.getenv("EYECRYPT_INTERNAL_TOKEN", "2YevP7ZrhlR6BWssWGR1xdTjBHwVTwtTspyExqbWIzU")
TRADES_LOG = Path(__file__).resolve().parent / "trades.jsonl"
STATE_FILE = Path(__file__).resolve().parent / ".last_sync_ts"
BATCH_SIZE = 200
INTERVAL_SEC = 300   # 5 min


def load_last_ts() -> float:
    if STATE_FILE.exists():
        try:
            return float(STATE_FILE.read_text().strip())
        except Exception:
            return 0.0
    return 0.0


def save_last_ts(ts: float) -> None:
    STATE_FILE.write_text(str(ts))


def read_new_trades(since_ts: float) -> list[dict]:
    out = []
    if not TRADES_LOG.exists():
        return out
    with open(TRADES_LOG, "r", encoding="utf-8") as f:
        for line in f:
            try:
                t = json.loads(line)
            except Exception:
                continue
            if t.get("status") != "ok":
                continue
            if t.get("ts", 0) <= since_ts:
                continue
            out.append({
                "ts": float(t["ts"]),
                "symbol": t.get("symbol", ""),
                "buy_ex": t.get("buy_ex", ""),
                "sell_ex": t.get("sell_ex", ""),
                "base_qty": float(t.get("base_filled", 0)),
                "pnl_usd": float(t.get("actual_pnl_usd", 0)),
                "net_pct": float(t.get("actual_net_pct", 0)),
                "status": "ok",
                "latency_ms": float(t.get("exec_latency_ms", 0)),
            })
    return out


def push_batch(trades: list[dict]) -> dict:
    r = httpx.post(
        f"{API_URL}/v1/internal/upload-trades",
        headers={"X-Internal-Token": TOKEN, "Content-Type": "application/json"},
        json={"trades": trades, "house_user_id": 1},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def sync_once() -> int:
    last_ts = load_last_ts()
    new_trades = read_new_trades(last_ts)
    if not new_trades:
        print(f"[{time.strftime('%H:%M:%S')}] no new trades (last_ts={last_ts:.0f})")
        return 0
    total = 0
    # push in batches
    for i in range(0, len(new_trades), BATCH_SIZE):
        batch = new_trades[i : i + BATCH_SIZE]
        try:
            res = push_batch(batch)
            total += res.get("inserted", 0)
            print(f"[{time.strftime('%H:%M:%S')}] pushed {res.get('inserted', 0)} (total in DB: {res.get('total_in_db', '?')})")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] batch FAILED: {e}")
            return total
    # update last_ts to the newest one
    save_last_ts(max(t["ts"] for t in new_trades))
    return total


def main():
    once = "--once" in sys.argv
    print(f"sync_trades → {API_URL}  (interval={INTERVAL_SEC}s, batch={BATCH_SIZE})")
    if once:
        sync_once()
        return
    while True:
        try:
            sync_once()
        except KeyboardInterrupt:
            print("stopped"); return
        except Exception as e:
            print(f"loop error: {e}")
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
