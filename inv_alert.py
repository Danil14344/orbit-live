"""Inventory alert — warns when a leg is starved (can't buy/sell a full position).

Reads balances_snapshot.json (written live by the bot), values whitelisted-token
inventory at public prices, and flags per-exchange:
  - BUY starved : USDT on that exchange < position size
  - SELL starved: token inventory value on that exchange < position size
ALERT ONLY — no orders, no transfers.

Run: py inv_alert.py
"""
import json, os, time, asyncio
import ccxt.async_support as ccxt
from dotenv import load_dotenv
load_dotenv()

SNAP = "balances_snapshot.json"
ACTIVE = {e.strip().lower() for e in os.getenv("ACTIVE_EXCHANGES", "mexc,bingx").split(",") if e.strip()}
WL = [t.strip().upper() for t in os.getenv("WHITELIST", "").split(",") if t.strip()]
POS = float(os.getenv("POSITION_USD", "30"))
STALE_SEC = 300


async def prices(tokens):
    ex = ccxt.bingx({"enableRateLimit": True, "timeout": 15000,
                     "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    out = {}
    try:
        await ex.load_markets()
        for t in tokens:
            sym = f"{t}/USDT"
            if sym in ex.symbols:
                try:
                    tk = await ex.fetch_ticker(sym)
                    out[t] = tk.get("last") or tk.get("bid")
                except Exception:
                    out[t] = None
    finally:
        await ex.close()
    return out


def main():
    if not os.path.exists(SNAP):
        print("no balances_snapshot.json (bot not in live or not yet written)"); return
    snap = json.load(open(SNAP))
    age = time.time() - snap.get("ts", 0)
    stale = age > STALE_SEC
    print(f"# snapshot age={age:.0f}s {'[STALE]' if stale else ''}  ACTIVE={sorted(ACTIVE)}  WL={WL}  pos=${POS}")
    if not WL:
        print("WHITELIST empty — nothing to check"); return

    px = asyncio.run(prices(WL))
    exs = snap.get("exchanges", {})
    warns = []

    for exid in sorted(ACTIVE):
        bal = (exs.get(exid) or {}).get("balances", {}) or {}
        usdt = bal.get("USDT", 0) or 0
        buy_ok = usdt >= POS
        line = [f"{exid}: USDT={usdt:.2f} buy={'OK' if buy_ok else 'STARVED'}"]
        if not buy_ok:
            warns.append(f"{exid} BUY starved (USDT ${usdt:.2f} < ${POS})")
        for tok in WL:
            qty = bal.get(tok, 0) or 0
            p = px.get(tok)
            val = qty * p if (qty and p) else 0
            sell_ok = val >= POS
            line.append(f"{tok}={val:.1f}$({'OK' if sell_ok else 'low'})")
            if not sell_ok:
                warns.append(f"{exid} SELL starved for {tok} (inv ${val:.2f} < ${POS})")
        print("  " + "  ".join(line))

    print("\n" + ("ALERTS:" if warns else "no starvation — all legs funded"))
    for w in warns:
        print("  ! " + w)
    if stale:
        print("  ! snapshot is STALE — bot may not be writing balances (live mode only)")


if __name__ == "__main__":
    main()
