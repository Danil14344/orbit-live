"""Full balance + open-position dump across live venues. Read-only."""
import asyncio, os
import ccxt.async_support as ccxt
from dotenv import load_dotenv
load_dotenv()

VENUES = ["mexc", "bitget", "bingx", "htx"]

def creds(name):
    u = name.upper(); c = {}
    k = os.getenv(f"{u}_API_KEY"); s = os.getenv(f"{u}_SECRET")
    p = os.getenv(f"{u}_PASSWORD"); uid = os.getenv(f"{u}_UID")
    if k and s: c["apiKey"] = k; c["secret"] = s
    if p: c["password"] = p
    if uid: c["uid"] = uid
    return c

async def dump(name):
    print(f"\n===== {name.upper()} =====")
    cfg = {"enableRateLimit": True, "timeout": 20000}
    cfg.update(creds(name))
    if not cfg.get("apiKey"):
        print("  no key in .env"); return
    ex = getattr(ccxt, name)(cfg)
    try:
        bal = await ex.fetch_balance()
        tot = bal.get("total", {})
        nz = {k: v for k, v in tot.items() if v and v > 0}
        if nz:
            print("  balances (total):")
            for k, v in sorted(nz.items(), key=lambda x: -x[1]):
                print(f"    {k:8} {v:.8f}")
        else:
            print("  balances: (all zero)")
    except Exception as e:
        print(f"  balance FAIL: {str(e)[:120]}")
    # open perp positions (the hedge)
    for opt in ({"type": "swap"}, {}):
        try:
            ex.options.update(opt)
            pos = await ex.fetch_positions()
            live = [p for p in pos if p.get("contracts")]
            if live:
                print("  open positions:")
                for p in live:
                    print(f"    {p.get('symbol')} {p.get('side')} contracts={p.get('contracts')} "
                          f"notional={p.get('notional')} uPnL={p.get('unrealizedPnl')}")
            break
        except Exception:
            continue
    await ex.close()

async def main():
    for v in VENUES:
        try: await dump(v)
        except Exception as e: print(f"{v}: ERR {str(e)[:100]}")

asyncio.run(main())
