"""Read-only: BingX spot specs+price for NEAR/USDT and ULTIMA/USDT + current USDT."""
import asyncio, os
import ccxt.async_support as ccxt
from dotenv import load_dotenv
load_dotenv()

async def main():
    ex = ccxt.bingx({
        "enableRateLimit": True, "timeout": 20000,
        "apiKey": os.getenv("BINGX_API_KEY"), "secret": os.getenv("BINGX_SECRET"),
        "options": {"defaultType": "spot", "adjustForTimeDifference": True, "recvWindow": 60000},
    })
    try:
        await ex.load_markets()
        bal = await ex.fetch_balance()
        print("spot USDT free =", (bal.get('free') or {}).get('USDT'))
        for sym in ("NEAR/USDT", "ULTIMA/USDT"):
            if sym not in ex.symbols:
                print(f"{sym}: NOT LISTED on bingx spot"); continue
            m = ex.market(sym)
            t = await ex.fetch_ticker(sym)
            print(f"{sym}: ask={t.get('ask')} bid={t.get('bid')} last={t.get('last')} "
                  f"| amount.min={m.get('limits',{}).get('amount',{}).get('min')} "
                  f"cost.min={m.get('limits',{}).get('cost',{}).get('min')} "
                  f"prec.amount={m.get('precision',{}).get('amount')}")
    finally:
        await ex.close()

asyncio.run(main())
