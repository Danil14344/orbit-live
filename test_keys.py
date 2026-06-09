"""Quick check that API keys work — fetches balance (or just account info)."""
import asyncio
import os
import ccxt.async_support as ccxt
from dotenv import load_dotenv

load_dotenv()

EXCHANGES = ["mexc", "bitget", "htx", "kucoin", "bitmart", "bingx"]


def _creds(name):
    upper = name.upper()
    cfg = {}
    key = os.getenv(f"{upper}_API_KEY")
    sec = os.getenv(f"{upper}_SECRET")
    pwd = os.getenv(f"{upper}_PASSWORD")
    uid = os.getenv(f"{upper}_UID")
    if key and sec:
        cfg["apiKey"] = key
        cfg["secret"] = sec
    if pwd:
        cfg["password"] = pwd
    if uid:
        cfg["uid"] = uid
    return cfg


async def test_one(name):
    klass = getattr(ccxt, name)
    cfg = {"enableRateLimit": True, "timeout": 15000}
    cfg.update(_creds(name))
    ex = klass(cfg)
    try:
        # 1) public — must work always
        await ex.load_markets()

        # 2) auth — fetch_balance requires valid signature
        bal = await ex.fetch_balance()
        usdt = bal.get("USDT", {})
        free = usdt.get("free", 0) or 0
        print(f"  [OK]   {name}: auth works | USDT free = {free}")

        # 3) currencies (some exchanges need auth for this)
        try:
            cur = await ex.fetch_currencies()
            print(f"         currencies: {len(cur) if cur else 0}")
        except Exception as e:
            print(f"         currencies: FAIL ({str(e)[:60]})")
    except Exception as e:
        print(f"  [FAIL] {name}: {str(e)[:120]}")
    finally:
        await ex.close()


async def main():
    for n in EXCHANGES:
        await test_one(n)


asyncio.run(main())
