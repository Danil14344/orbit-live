"""Read-only: fetch bitget USDT deposit address on BEP20."""
import asyncio, os
import ccxt.async_support as ccxt
from dotenv import load_dotenv
load_dotenv()

async def main():
    ex=ccxt.bitget({
        "enableRateLimit":True,"timeout":20000,
        "apiKey":os.getenv("BITGET_API_KEY"),"secret":os.getenv("BITGET_SECRET"),
        "password":os.getenv("BITGET_PASSWORD"),
        "options":{"adjustForTimeDifference":True,"recvWindow":60000},
    })
    try:
        try: await ex.load_time_difference()
        except Exception: pass
        attempts=[
            {"chain":"BEP20"}, {"chain":"BSC"}, {"chain":"BEP20(BSC)"},
            {"network":"BSC"}, {"chain":"bep20"},
        ]
        for p in attempts:
            try:
                a=await ex.fetch_deposit_address("USDT", p)
                print(f"params={p} -> address={a.get('address')} tag/memo={a.get('tag')} info_net={a.get('network')}")
            except Exception as e:
                print(f"params={p} FAIL: {str(e)[:110]}")
    finally:
        await ex.close()

asyncio.run(main())
