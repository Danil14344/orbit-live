import asyncio
import sys
sys.path.insert(0, ".")
from scanner import (
    make_exchange, fetch_all, build_book, find_opportunities,
    EXCHANGES, TAKER_FEE, TARGET_POSITION_USD, DEPTH_CHECK_N,
)
from currencies import load_all_currencies
from depth import fetch_books_for_opps, evaluate_depth


async def main():
    exchanges = []
    for name in EXCHANGES:
        try:
            ex = await make_exchange(name)
            exchanges.append(ex)
        except Exception as e:
            print(f"fail {name}: {e}")
    try:
        print("Loading currencies...")
        currencies_map = await load_all_currencies(exchanges)

        print("Fetching tickers...")
        results = await fetch_all(exchanges)
        book, errors = build_book(results)
        opps, rejected = find_opportunities(book, currencies_map)

        print(f"\nInitial: {len(opps)} opps after fee + antifake filters")
        print(f"\nTop 15 BEFORE depth check:")
        for o in opps[:15]:
            wf = f"{o['wfee_pct']:.3f}%" if o['wfee_pct'] is not None else "-"
            print(f"  {o['symbol']:18} {o['buy_ex']:8}->{o['sell_ex']:8}  net(top)={o['net']:.3f}%  wfee={wf}")

        print(f"\nFetching order books for top {DEPTH_CHECK_N}...")
        ex_by_id = {ex.id: ex for ex in exchanges}
        books = await fetch_books_for_opps(ex_by_id, opps[:DEPTH_CHECK_N])
        print(f"Got {len(books)} order books")

        print(f"\nAfter depth check (target ${TARGET_POSITION_USD}):")
        kept = []
        for o in opps[:DEPTH_CHECK_N]:
            d = evaluate_depth(o, books, TARGET_POSITION_USD, TAKER_FEE)
            if d is None:
                print(f"  X {o['symbol']:18} {o['buy_ex']:8}->{o['sell_ex']:8}  no depth data")
                continue
            if d["real_net_pct"] <= 0:
                print(f"  - {o['symbol']:18} {o['buy_ex']:8}->{o['sell_ex']:8}  top={o['net']:.3f}% real={d['real_net_pct']:.3f}% (killed by slippage)")
                continue
            o.update(d)
            kept.append(o)
            full = "full" if d["depth_full"] else "thin"
            print(f"  V {o['symbol']:18} {o['buy_ex']:8}->{o['sell_ex']:8}  top={o['net']:.3f}% real={d['real_net_pct']:.3f}%  max=${d['max_usd_achievable']:,.0f} ({full})")

        kept.sort(key=lambda x: x['real_net_pct'], reverse=True)
        print(f"\n=== {len(kept)} REAL opps after full validation ===")
        for o in kept:
            full = "full" if o['depth_full'] else "thin"
            print(f"  {o['symbol']:18} {o['buy_ex']:8}->{o['sell_ex']:8}  real_net={o['real_net_pct']:.3f}%  max=${o['max_usd_achievable']:,.0f} ({full})")
    finally:
        await asyncio.gather(*(ex.close() for ex in exchanges), return_exceptions=True)


asyncio.run(main())
