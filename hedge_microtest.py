"""Micro hedge test on BingX: open 1-contract NEAR short, verify, close. Real money (~$2.4)."""
import asyncio, os, json
import ccxt.async_support as ccxt
from dotenv import load_dotenv
load_dotenv()

SYM = "NEAR/USDT:USDT"
QTY = 1.0  # 1 contract = 1 NEAR, the exchange minimum

async def main():
    ex = ccxt.bingx({
        "enableRateLimit": True, "timeout": 20000,
        "apiKey": os.getenv("BINGX_API_KEY"), "secret": os.getenv("BINGX_SECRET"),
        "options": {"defaultType": "swap", "adjustForTimeDifference": True, "recvWindow": 60000},
    })
    try:
        await ex.load_markets()
        b0 = (await ex.fetch_balance()).get('total', {}).get('USDT')
        print(f"[0] swap USDT before = {b0}")

        # mirror the bot: 1x leverage, one-way mode (best-effort)
        try:
            await ex.set_position_mode(False)  # one-way
            print("[setup] position mode -> one-way")
        except Exception as e:
            print(f"[setup] set_position_mode skipped: {str(e)[:80]}")
        for sd in ("LONG", "SHORT", "BOTH"):
            try:
                await ex.set_leverage(1, SYM, {"side": sd});
            except Exception:
                pass
        print("[setup] leverage set attempts done (1x)")

        # OPEN short: market sell 1 contract
        print(f"[open] market SELL {QTY} {SYM} ...")
        o = await ex.create_order(SYM, "market", "sell", QTY)
        print(f"[open] id={o.get('id')} status={o.get('status')} filled={o.get('filled')} avg={o.get('average')} fee={o.get('fee')}")

        await asyncio.sleep(2)
        pos = await ex.fetch_positions([SYM])
        pos = [p for p in pos if p.get('contracts')]
        for p in pos:
            print(f"[pos] side={p.get('side')} contracts={p.get('contracts')} entry={p.get('entryPrice')} notional={p.get('notional')} lev={p.get('leverage')} liq={p.get('liquidationPrice')}")
        if not pos:
            print("[pos] WARNING: no open position detected after order!")

        # CLOSE: market buy 1 contract, reduceOnly
        print(f"[close] market BUY {QTY} {SYM} reduceOnly ...")
        c = await ex.create_order(SYM, "market", "buy", QTY, None, {"reduceOnly": True})
        print(f"[close] id={c.get('id')} status={c.get('status')} filled={c.get('filled')} avg={c.get('average')} fee={c.get('fee')}")

        await asyncio.sleep(2)
        pos2 = await ex.fetch_positions([SYM])
        pos2 = [p for p in pos2 if p.get('contracts')]
        print(f"[verify] open positions after close: {pos2 if pos2 else 'FLAT (ok)'}")
        b1 = (await ex.fetch_balance()).get('total', {}).get('USDT')
        print(f"[1] swap USDT after = {b1}  | delta = {None if (b0 is None or b1 is None) else round(b1-b0,5)} (≈ -fees)")
    finally:
        await ex.close()

asyncio.run(main())
