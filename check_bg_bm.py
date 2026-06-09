"""Check bitget + bitmart auth and USDT balance (with time sync)."""
import asyncio, os
import ccxt.async_support as ccxt
from dotenv import load_dotenv
load_dotenv()

def creds(name):
    u=name.upper(); c={}
    k=os.getenv(f"{u}_API_KEY"); s=os.getenv(f"{u}_SECRET"); p=os.getenv(f"{u}_PASSWORD"); uid=os.getenv(f"{u}_UID")
    if k and s: c["apiKey"]=k; c["secret"]=s
    if p: c["password"]=p
    if uid: c["uid"]=uid
    return c

async def one(name):
    cfg={"enableRateLimit":True,"timeout":20000,"options":{"adjustForTimeDifference":True,"recvWindow":60000}}
    cfg.update(creds(name))
    ex=getattr(ccxt,name)(cfg)
    try:
        try: await ex.load_time_difference()
        except Exception: pass
        await ex.load_markets()
        bal=await ex.fetch_balance()
        free=bal.get("free") or {}; total=bal.get("total") or {}
        nz=sorted({k for k in (set(free)|set(total)) if (total.get(k) or 0)>0}, key=lambda k:-(total.get(k) or 0))
        print(f"[{name}] auth OK | USDT free={free.get('USDT')} | nonzero: "+", ".join(f"{k}={total.get(k)}" for k in nz[:6]))
    except Exception as e:
        print(f"[{name}] FAIL: {type(e).__name__}: {str(e)[:140]}")
    finally:
        await ex.close()

async def main():
    for n in ("bitget","bitmart"): await one(n)
asyncio.run(main())
