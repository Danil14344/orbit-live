"""Run WS feeders for 20 sec, then dump stats and sample data."""
import asyncio
import sys
sys.path.insert(0, ".")
from ws_scanner import (
    make_exchange, TickerHub, run_ws_all, run_ws_list, run_rest,
    EXCHANGES, WS_MODE, find_opportunities, build_universe, _safe_load_markets,
    PER_EXCHANGE_SUB_CAP,
)
from currencies import load_all_currencies


async def main():
    exchanges = []
    for n in EXCHANGES:
        try:
            ex = await make_exchange(n)
            exchanges.append(ex)
        except Exception as e:
            print(f"fail {n}: {e}")

    print("Loading currencies...")
    currencies_map = await load_all_currencies(exchanges)

    universe = await build_universe(exchanges)
    await asyncio.gather(*(_safe_load_markets(ex) for ex in exchanges), return_exceptions=True)

    hub = TickerHub()
    feeders = []
    for ex in exchanges:
        mode = WS_MODE.get(ex.id, "rest")
        if mode == "all":
            feeders.append(asyncio.create_task(run_ws_all(ex, hub)))
        elif mode == "list":
            ex_syms = set(ex.symbols or [])
            ex_uni = [s for s in universe if s in ex_syms]
            cap = PER_EXCHANGE_SUB_CAP.get(ex.id)
            if cap:
                ex_uni = ex_uni[:cap]
            print(f"  {ex.id}: subscribing {len(ex_uni)}/{len(universe)}")
            feeders.append(asyncio.create_task(run_ws_list(ex, hub, ex_uni)))
        else:
            feeders.append(asyncio.create_task(run_rest(ex, hub)))

    print("Running feeders 20s...")
    await asyncio.sleep(20)

    print("\nStats per exchange:")
    for ex_id, s in hub.stats.items():
        print(f"  {ex_id:8} updates={s['updates']:6} symbols={len(hub.tickers[ex_id]):5} err='{s['error']}'")

    book = hub.snapshot()
    multi = sum(1 for v in book.values() if len(v) >= 2)
    print(f"\nTotal symbols with >=2 ex in snapshot: {multi}")

    opps, rejected = find_opportunities(book, currencies_map)
    print(f"Net-positive opps: {len(opps)}")
    print(f"Rejected: {dict(rejected)}")
    print("\nTop 10:")
    for o in opps[:10]:
        v = "V" if o.get("verified") else " "
        wf = f"{o['wfee_pct']:.3f}%" if o['wfee_pct'] is not None else "-"
        print(f"  {v} {o['symbol']:18} {o['buy_ex']:8}->{o['sell_ex']:8}  net={o['net']:.3f}%  wfee={wf}")

    for t in feeders:
        t.cancel()
    await asyncio.gather(*(ex.close() for ex in exchanges), return_exceptions=True)


asyncio.run(main())
