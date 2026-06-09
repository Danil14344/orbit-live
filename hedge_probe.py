"""Read-only discovery for NEAR perp on BingX: market limits, price, swap balance."""
import asyncio, os
import ccxt.async_support as ccxt
from dotenv import load_dotenv
load_dotenv()

async def main():
    ex = ccxt.bingx({
        "enableRateLimit": True, "timeout": 20000,
        "apiKey": os.getenv("BINGX_API_KEY"), "secret": os.getenv("BINGX_SECRET"),
        "options": {"defaultType": "swap", "adjustForTimeDifference": True, "recvWindow": 60000},
    })
    try:
        await ex.load_markets()
        # find NEAR linear perp
        cands = [s for s in ex.symbols if s.startswith("NEAR/USDT")]
        print("NEAR perp candidates:", cands)
        sym = "NEAR/USDT:USDT" if "NEAR/USDT:USDT" in ex.symbols else (cands[0] if cands else None)
        if not sym:
            print("NO NEAR perp on bingx"); return
        m = ex.market(sym)
        print(f"symbol={sym} type={m.get('type')} linear={m.get('linear')} contractSize={m.get('contractSize')}")
        print(f"limits.amount={m.get('limits',{}).get('amount')}")
        print(f"limits.cost={m.get('limits',{}).get('cost')}")
        print(f"precision={m.get('precision')}")
        t = await ex.fetch_ticker(sym)
        print(f"price last={t.get('last')} bid={t.get('bid')} ask={t.get('ask')}")
        bal = await ex.fetch_balance()
        usdt = (bal.get('total') or {}).get('USDT')
        free = (bal.get('free') or {}).get('USDT')
        print(f"swap USDT total={usdt} free={free}")
        # any open positions already?
        try:
            pos = await ex.fetch_positions([sym])
            opn = [p for p in pos if p.get('contracts')]
            print(f"open NEAR positions: {opn if opn else 'none'}")
        except Exception as e:
            print(f"fetch_positions: {str(e)[:100]}")
    finally:
        await ex.close()

asyncio.run(main())
