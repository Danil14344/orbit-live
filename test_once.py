import asyncio
import sys
sys.path.insert(0, ".")
from scanner import make_exchange, fetch_all, build_book, find_opportunities, EXCHANGES
from currencies import load_all_currencies

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
        for ex_id, c in currencies_map.items():
            sample_with_nets = sum(1 for v in c.values() if v.get("networks"))
            print(f"  {ex_id}: {len(c)} currencies ({sample_with_nets} with networks)")

        print("\nFetching tickers...")
        results = await fetch_all(exchanges)
        for ex_id, t in results:
            if "__error__" in t:
                print(f"  ERR {ex_id}: {t['__error__'][:80]}")
            else:
                print(f"  OK  {ex_id}: {len(t)} tickers")

        book, errors = build_book(results)
        opps, rejected = find_opportunities(book, currencies_map)
        print(f"\nCommon symbols (>=2 ex): {sum(1 for v in book.values() if len(v)>=2)}")
        print(f"Final opportunities: {len(opps)}")
        print(f"Rejected: {dict(rejected)}")
        print(f"\nTop 20:")
        for o in opps[:20]:
            v = "✓" if o["verified"] else " "
            wf = f"{o['wfee_pct']:.3f}%" if o['wfee_pct'] is not None else "—"
            print(f"  {v} {o['symbol']:18} {o['buy_ex']:8}@{o['buy_ask']:.6g} -> {o['sell_ex']:8}@{o['sell_bid']:.6g}  gross={o['gross']:.3f}%  wfee={wf:>8}  net={o['net']:.3f}%  net={o.get('network') or '—'}")
    finally:
        await asyncio.gather(*(ex.close() for ex in exchanges), return_exceptions=True)

asyncio.run(main())
