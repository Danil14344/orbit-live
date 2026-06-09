"""Seed BingX spot inventory: market-buy ~$30 NEAR and ~$30 ULTIMA. REAL MONEY."""
import asyncio, os, math
import ccxt.async_support as ccxt
from dotenv import load_dotenv
load_dotenv()

TARGET_USD = 30.0
HARD_CAP_USD = 33.0  # refuse if computed spend exceeds this

def floor_to(x, step):
    if not step or step <= 0:
        return x
    return math.floor(x / step) * step

async def buy(ex, sym):
    m = ex.market(sym)
    t = await ex.fetch_ticker(sym)
    ask = t.get("ask") or t.get("last")
    step = m.get("precision", {}).get("amount")
    raw = TARGET_USD / ask
    amt = floor_to(raw, step) if (step and step < 1) else round(raw, 3)
    amt = float(ex.amount_to_precision(sym, amt))
    est = amt * ask
    print(f"{sym}: ask={ask} -> amount={amt} est_cost=${est:.2f}")
    if est > HARD_CAP_USD:
        print(f"  ABORT {sym}: est ${est:.2f} > cap ${HARD_CAP_USD}"); return
    o = await ex.create_order(sym, "market", "buy", amt)
    print(f"  ORDER {sym}: id={o.get('id')} status={o.get('status')} filled={o.get('filled')} avg={o.get('average')} cost={o.get('cost')}")

async def main():
    ex = ccxt.bingx({
        "enableRateLimit": True, "timeout": 20000,
        "apiKey": os.getenv("BINGX_API_KEY"), "secret": os.getenv("BINGX_SECRET"),
        "options": {"defaultType": "spot", "adjustForTimeDifference": True, "recvWindow": 60000},
    })
    try:
        await ex.load_markets()
        b0 = (await ex.fetch_balance()).get("free", {})
        print("before: USDT", b0.get("USDT"), "NEAR", b0.get("NEAR"), "ULTIMA", b0.get("ULTIMA"))
        for sym in ("NEAR/USDT", "ULTIMA/USDT"):
            try:
                await buy(ex, sym)
            except Exception as e:
                print(f"  FAIL {sym}: {type(e).__name__}: {str(e)[:160]}")
            await asyncio.sleep(1)
        b1 = (await ex.fetch_balance()).get("free", {})
        print("after:  USDT", b1.get("USDT"), "NEAR", b1.get("NEAR"), "ULTIMA", b1.get("ULTIMA"))
    finally:
        await ex.close()

asyncio.run(main())
