"""Check BingX: connectivity (public), then auth balance, isolating the wallet endpoint."""
import asyncio
import os
import ccxt.async_support as ccxt
from dotenv import load_dotenv

load_dotenv()


async def main():
    cfg = {
        "enableRateLimit": True,
        "timeout": 20000,
        "apiKey": os.getenv("BINGX_API_KEY"),
        "secret": os.getenv("BINGX_SECRET"),
    }
    ex = ccxt.bingx(cfg)
    # don't auto-fetch currencies (that hits the slow wallet endpoint)
    ex.options["fetchCurrencies"] = False
    try:
        # 1) public connectivity
        try:
            t = await ex.fetch_time()
            print(f"[public] fetch_time OK -> {t}")
        except Exception as e:
            print(f"[public] fetch_time FAIL: {type(e).__name__}: {str(e)[:120]}")

        # 2) load markets (public, no wallet endpoint now)
        try:
            await ex.load_markets()
            print(f"[public] load_markets OK ({len(ex.markets)} markets)")
        except Exception as e:
            print(f"[public] load_markets FAIL: {type(e).__name__}: {str(e)[:120]}")

        # 3) auth: spot balance
        for acct in ("spot", "swap"):
            try:
                bal = await ex.fetch_balance({"type": acct})
                free = bal.get("free") or {}
                used = bal.get("used") or {}
                total = bal.get("total") or {}
                nz = sorted({k for k in (set(free)|set(used)|set(total)) if (total.get(k) or 0) > 0},
                            key=lambda k: -(total.get(k) or 0))
                print(f"[{acct}] auth OK, nonzero assets: {len(nz)}")
                for k in nz:
                    f, u, tt = free.get(k) or 0, used.get(k) or 0, total.get(k) or 0
                    flag = "  <-- LOCKED" if u > 0 and f == 0 else ""
                    print(f"    {k}: free={f} locked={u} total={tt}{flag}")
            except Exception as e:
                print(f"[{acct}] FAIL: {type(e).__name__}: {str(e)[:150]}")

        # 4) wallet/withdraw status (the endpoint that froze) — short timeout
        ex.timeout = 12000
        try:
            cur = await ex.fetch_currencies()
            blocked = [c for c, i in (cur or {}).items() if i.get("withdraw") is False]
            print(f"[wallet] currencies={len(cur or {})} withdraw-disabled={len(blocked)}")
        except Exception as e:
            print(f"[wallet] FAIL: {type(e).__name__}: {str(e)[:150]}")
    finally:
        await ex.close()


asyncio.run(main())
