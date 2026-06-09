"""Probe API key permissions safely without placing real orders.

Strategy: call cancel_order with a fake order id.
- Trade permission missing → "permission denied" / "invalid api key"
- Trade permission present → "order not found" / "invalid order id"
"""
import asyncio
import os
import ccxt.async_support as ccxt
from dotenv import load_dotenv

load_dotenv()

EXCHANGES = ["mexc", "bitget", "htx"]


def _creds(name):
    upper = name.upper()
    cfg = {}
    k = os.getenv(f"{upper}_API_KEY")
    s = os.getenv(f"{upper}_SECRET")
    p = os.getenv(f"{upper}_PASSWORD")
    if k and s:
        cfg["apiKey"] = k
        cfg["secret"] = s
    if p:
        cfg["password"] = p
    return cfg


def classify_error(msg):
    m = msg.lower()
    if any(s in m for s in ["permission", "denied", "not allowed", "unauthorized",
                              "insufficient privilege", "no permission", "missing scope",
                              "api key not enabled"]):
        return "NO_TRADE_PERMISSION"
    if any(s in m for s in ["order not found", "order does not exist", "no such order",
                              "unknown order", "invalid order", "orderid", "ordernotexist",
                              "70014", "30041", "70060"]):
        return "TRADE_OK_no_such_order"
    return "AMBIGUOUS"


async def probe(name):
    klass = getattr(ccxt, name)
    cfg = {"enableRateLimit": True, "options": {"adjustForTimeDifference": True, "recvWindow": 60000}}
    cfg.update(_creds(name))
    ex = klass(cfg)

    print(f"\n=== {name.upper()} ===")
    try:
        await ex.load_markets()
    except Exception as e:
        print(f"  load_markets WARN: {str(e)[:80]}")

    # READ test
    try:
        await ex.fetch_balance()
        print(f"  READ:  OK")
    except Exception as e:
        print(f"  READ:  FAIL {str(e)[:80]}")
        await ex.close(); return

    # TRADE test via fake cancel_order
    fake_id = "999999999999999999"
    try:
        await ex.cancel_order(fake_id, "BTC/USDT")
        print(f"  TRADE: OK (unexpectedly cancelled fake id?!)")
    except ccxt.OrderNotFound as e:
        print(f"  TRADE: OK (exception type=OrderNotFound -> auth+trade verified)")
    except ccxt.PermissionDenied:
        print(f"  TRADE: NO_TRADE_PERMISSION")
    except Exception as e:
        msg = str(e).encode('ascii', 'replace').decode('ascii')
        verdict = classify_error(msg)
        print(f"  TRADE: {verdict}")
        print(f"         exception type: {type(e).__name__}")
        print(f"         msg: {msg[:200]}")

    await ex.close()


async def main():
    for n in EXCHANGES:
        await probe(n)


asyncio.run(main())
