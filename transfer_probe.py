"""Read-only: find cheapest common USDT network between BingX (withdraw) and bitget (deposit)."""
import asyncio, os
import ccxt.async_support as ccxt
from dotenv import load_dotenv
load_dotenv()

def creds(name):
    u=name.upper(); c={}
    k=os.getenv(f"{u}_API_KEY"); s=os.getenv(f"{u}_SECRET"); p=os.getenv(f"{u}_PASSWORD")
    if k and s: c["apiKey"]=k; c["secret"]=s
    if p: c["password"]=p
    return c

async def usdt_networks(name):
    cfg={"enableRateLimit":True,"timeout":20000,"options":{"adjustForTimeDifference":True,"recvWindow":60000}}
    cfg.update(creds(name))
    ex=getattr(ccxt,name)(cfg)
    out={}
    try:
        try: await ex.load_time_difference()
        except Exception: pass
        cur=await ex.fetch_currencies()
        u=cur.get("USDT") or {}
        nets=u.get("networks") or {}
        for net,info in nets.items():
            out[net]={
                "withdraw":info.get("withdraw"),
                "deposit":info.get("deposit"),
                "fee":info.get("fee"),
            }
    except Exception as e:
        print(f"[{name}] fetch_currencies FAIL: {type(e).__name__}: {str(e)[:120]}")
    finally:
        await ex.close()
    return out

async def main():
    bx=await usdt_networks("bingx")
    bg=await usdt_networks("bitget")
    print("=== BingX USDT networks (withdraw side) ===")
    for n,i in sorted(bx.items()): print(f"  {n}: withdraw={i['withdraw']} fee={i['fee']}")
    print("=== bitget USDT networks (deposit side) ===")
    for n,i in sorted(bg.items()): print(f"  {n}: deposit={i['deposit']} fee={i['fee']}")
    common=[n for n in bx if n in bg and bx[n].get("withdraw") and bg[n].get("deposit")]
    print("=== COMMON (bingx-withdraw & bitget-deposit enabled) ===")
    ranked=sorted(common, key=lambda n: (bx[n].get("fee") if bx[n].get("fee") is not None else 9e9))
    for n in ranked:
        print(f"  {n}: bingx_withdraw_fee={bx[n].get('fee')}")
    if ranked:
        print(f">>> CHEAPEST: {ranked[0]} (fee {bx[ranked[0]].get('fee')} USDT)")

asyncio.run(main())
