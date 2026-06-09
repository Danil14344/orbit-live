"""Check which exchanges support watch_tickers via ccxt.pro WS."""
import asyncio
import ccxt.pro as ccxtpro

EXCHANGES = ["mexc", "gate", "kucoin", "bitget", "htx", "bingx", "bitmart"]


async def probe(name):
    try:
        klass = getattr(ccxtpro, name)
        ex = klass({"enableRateLimit": True})
        has_wt = ex.has.get("watchTickers")
        has_wob = ex.has.get("watchOrderBook")
        has_wobs = ex.has.get("watchOrderBookForSymbols")
        await ex.close()
        print(f"  {name:8} watchTickers={has_wt}  watchOrderBook={has_wob}  watchOrderBookForSymbols={has_wobs}")
    except Exception as e:
        print(f"  {name:8} FAIL: {e}")


async def main():
    print(f"ccxt.pro version: {ccxtpro.__version__ if hasattr(ccxtpro, '__version__') else 'n/a'}")
    print(f"Available exchanges (pro): {len(ccxtpro.exchanges)}")
    for n in EXCHANGES:
        await probe(n)

asyncio.run(main())
