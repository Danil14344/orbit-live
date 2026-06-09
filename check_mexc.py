"""Check MEXC for freeze: connectivity, auth balance (free/locked), withdraw status."""
import asyncio
import os
import ccxt.async_support as ccxt
from dotenv import load_dotenv

load_dotenv()


async def main():
    cfg = {
        "enableRateLimit": True,
        "timeout": 20000,
        "apiKey": os.getenv("MEXC_API_KEY"),
        "secret": os.getenv("MEXC_SECRET"),
        "options": {"adjustForTimeDifference": True, "recvWindow": 60000},
    }
    ex = ccxt.mexc(cfg)
    try:
        try:
            await ex.load_time_difference()
            print(f"[sync] clock offset applied")
        except Exception as e:
            print(f"[sync] load_time_difference failed: {str(e)[:80]}")
        try:
            t = await ex.fetch_time()
            print(f"[public] fetch_time OK -> {t}")
        except Exception as e:
            print(f"[public] fetch_time FAIL: {type(e).__name__}: {str(e)[:120]}")

        try:
            await ex.load_markets()
            print(f"[public] load_markets OK ({len(ex.markets)} markets)")
        except Exception as e:
            print(f"[public] load_markets FAIL: {type(e).__name__}: {str(e)[:120]}")

        try:
            bal = await ex.fetch_balance()
            free = bal.get("free") or {}
            used = bal.get("used") or {}
            total = bal.get("total") or {}
            nz = sorted({k for k in (set(free)|set(used)|set(total)) if (total.get(k) or 0) > 0},
                        key=lambda k: -(total.get(k) or 0))
            print(f"[balance] auth OK, nonzero assets: {len(nz)}")
            for k in nz:
                f, u, tt = free.get(k) or 0, used.get(k) or 0, total.get(k) or 0
                flag = "  <-- LOCKED" if u > 0 and f == 0 else ""
                print(f"    {k}: free={f} locked={u} total={tt}{flag}")
        except Exception as e:
            print(f"[balance] FAIL: {type(e).__name__}: {str(e)[:200]}")

        # withdraw availability for the assets we hold + USDT
        try:
            cur = await ex.fetch_currencies()
            for code in ("USDT", "USDC", "BTC", "ETH"):
                info = (cur or {}).get(code)
                if info is not None:
                    print(f"[wallet] {code}: withdraw={info.get('withdraw')} deposit={info.get('deposit')}")
        except Exception as e:
            print(f"[wallet] FAIL: {type(e).__name__}: {str(e)[:150]}")
    finally:
        await ex.close()


asyncio.run(main())
