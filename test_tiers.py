"""Run bot in a single tier mode for N seconds, then collect what happened."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
TIER = int(sys.argv[1]) if len(sys.argv) > 1 else 3
DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 180  # 3 min default

# Snapshot file sizes before run
def lines(path):
    p = ROOT / path
    if not p.exists():
        return 0
    return sum(1 for _ in open(p))

before = {
    "trades": lines("trades.jsonl"),
    "stops": lines("stops.jsonl"),
    "scanner_log": lines("tier_scanner_opps.jsonl"),
}

env = os.environ.copy()
env["TIER"] = str(TIER)
env["MODE"] = "paper"

print(f"=== TIER {TIER} TEST — running scanner for {DURATION}s ===")
print(f"   trade size cap: " + ("$50 (tier 2)" if TIER == 2 else "$30 (default)" if TIER == 3 else "no trades (scanner only)"))
print(f"   filter: " + ("none — full universe" if TIER == 1 else "majors only (BTC/ETH/SOL/...)" if TIER == 2 else "whitelist (WARD/SLVON/IRYS)"))

proc = subprocess.Popen(
    [sys.executable, str(ROOT / "ws_scanner.py")],
    cwd=ROOT, env=env,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
)
try:
    time.sleep(DURATION)
finally:
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

after = {
    "trades": lines("trades.jsonl"),
    "stops": lines("stops.jsonl"),
    "scanner_log": lines("tier_scanner_opps.jsonl"),
}

new_trades = after["trades"] - before["trades"]
new_stops = after["stops"] - before["stops"]
new_scan = after["scanner_log"] - before["scanner_log"]

print(f"\n=== TIER {TIER} RESULTS ({DURATION}s) ===")
print(f"  New trades executed: {new_trades}")
print(f"  New stops triggered: {new_stops}")
print(f"  New spreads detected (tier 1 only): {new_scan}")

if new_trades > 0:
    with open(ROOT / "trades.jsonl") as f:
        all_t = [json.loads(l) for l in f]
    new = all_t[-new_trades:]
    pnl = sum(t["actual_pnl_usd"] for t in new)
    print(f"  Trade PnL: ${pnl:+.2f}")
    print(f"  Avg per trade: ${pnl/new_trades:+.2f}")
    print(f"  Symbols: {set(t['symbol'] for t in new)}")

if new_scan > 0:
    with open(ROOT / "tier_scanner_opps.jsonl") as f:
        all_s = [json.loads(l) for l in f]
    new = all_s[-new_scan:]
    pot_pnl = sum(s["would_pnl"] for s in new)
    print(f"  Would-have-pnl if traded: ${pot_pnl:+.2f}")
    print(f"  Top symbols: {sorted({s['symbol'] for s in new})[:10]}")
