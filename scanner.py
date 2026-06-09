import asyncio
import os
import time
from collections import defaultdict

import ccxt.async_support as ccxt
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.table import Table

load_dotenv()

from currencies import (
    load_all_currencies,
    can_transfer,
    best_withdraw_fee,
    contracts_match,
)
from depth import fetch_books_for_opps, evaluate_depth

EXCHANGES = ["mexc", "gate", "kucoin", "bitget", "htx", "bingx", "bitmart"]
QUOTE = "USDT"
MIN_QUOTE_VOLUME = 100_000     # 24h volume in USDT, both sides must exceed
TAKER_FEE = 0.001              # 0.1% per side, refine per-exchange later
POSITION_SIZE_USD = 1000       # to convert withdraw fee to %
SUSPICIOUS_SPREAD_PCT = 5.0    # spreads >= this require contract verification
HARD_MAX_SPREAD_PCT = 200.0    # spreads above this are always dropped (sanity)
REFRESH_SEC = 5
TOP_N = 30
DEPTH_CHECK_N = 15           # how many top candidates get order-book verified per cycle
TARGET_POSITION_USD = 200    # what size we test depth against

console = Console()


def _creds(name: str) -> dict:
    upper = name.upper()
    cfg = {}
    key = os.getenv(f"{upper}_API_KEY")
    sec = os.getenv(f"{upper}_SECRET")
    pwd = os.getenv(f"{upper}_PASSWORD")
    if key and sec:
        cfg["apiKey"] = key
        cfg["secret"] = sec
    if pwd:
        cfg["password"] = pwd
    return cfg


async def make_exchange(name: str):
    klass = getattr(ccxt, name)
    cfg = {"enableRateLimit": True, "timeout": 15000}
    cfg.update(_creds(name))
    return klass(cfg)


async def fetch_one(ex):
    try:
        tickers = await ex.fetch_tickers()
        return ex.id, tickers
    except Exception as e:
        return ex.id, {"__error__": str(e)}


async def fetch_all(exchanges):
    return await asyncio.gather(*(fetch_one(ex) for ex in exchanges))


def build_book(results):
    book = defaultdict(list)
    errors = {}
    for ex_id, tickers in results:
        if "__error__" in tickers:
            errors[ex_id] = tickers["__error__"]
            continue
        for sym, t in tickers.items():
            if not sym.endswith("/" + QUOTE):
                continue
            bid = t.get("bid")
            ask = t.get("ask")
            qv = t.get("quoteVolume")
            if not bid or not ask or bid <= 0 or ask <= 0:
                continue
            if qv is None or qv < MIN_QUOTE_VOLUME:
                continue
            book[sym].append((ex_id, bid, ask, qv))
    return book, errors


def find_opportunities(book, currencies_map):
    opps = []
    rejected = defaultdict(int)
    for sym, rows in book.items():
        if len(rows) < 2:
            continue
        base = sym.split("/")[0]
        buy = min(rows, key=lambda r: r[2])   # lowest ask
        sell = max(rows, key=lambda r: r[1])  # highest bid
        if buy[0] == sell[0]:
            continue
        ask = buy[2]
        bid = sell[1]
        gross = (bid - ask) / ask * 100
        if gross <= 0:
            continue
        if gross > HARD_MAX_SPREAD_PCT:
            rejected["insane_spread"] += 1
            continue

        # Transfer feasibility
        ok, reason, common = can_transfer(currencies_map, buy[0], sell[0], base)
        if ok is False:
            rejected[reason] += 1
            continue

        # Anti-fake: high spreads require matching contract addresses
        verified = None
        verify_note = ""
        if gross >= SUSPICIOUS_SPREAD_PCT:
            if ok is None:  # no currency data at all
                rejected["suspicious_no_data"] += 1
                continue
            matched, ev = contracts_match(common)
            if matched is False:
                rejected["fake_diff_contract"] += 1
                continue
            if matched is None:
                # No contract data — too risky to trust at high spread
                rejected["suspicious_no_contract"] += 1
                continue
            verified = True
            verify_note = ev

        # Withdraw fee
        wfee_base, wnet = (None, None)
        if ok and common:
            wfee_base, wnet = best_withdraw_fee(common)
        wfee_quote = wfee_base * ask if wfee_base is not None else None
        wfee_pct = (wfee_quote / POSITION_SIZE_USD * 100) if wfee_quote is not None else None

        # Net = trade fees both sides + withdraw fee on base
        net_after_trade = ((bid * (1 - TAKER_FEE)) - (ask * (1 + TAKER_FEE))) / ask * 100
        net = net_after_trade - (wfee_pct or 0)
        if net <= 0:
            rejected["negative_after_fees"] += 1
            continue

        opps.append({
            "symbol": sym,
            "buy_ex": buy[0],
            "buy_ask": ask,
            "sell_ex": sell[0],
            "sell_bid": bid,
            "gross": gross,
            "net": net,
            "min_vol": min(buy[3], sell[3]),
            "wfee_pct": wfee_pct,
            "network": wnet,
            "verified": verified,
            "verify_note": verify_note,
        })
    opps.sort(key=lambda o: o["net"], reverse=True)
    return opps, rejected


def render_table(opps, errors, rejected, elapsed):
    title = (
        f"Arbitrage scanner — top {TOP_N} | "
        f"taker {TAKER_FEE*100:.2f}%×2 | pos ${TARGET_POSITION_USD} | "
        f"refresh {elapsed:.1f}s | opps={len(opps)} rej={dict(rejected)}"
    )
    table = Table(title=title, expand=True)
    table.add_column("Symbol", style="cyan", no_wrap=True)
    table.add_column("Buy", style="green")
    table.add_column("Sell", style="magenta")
    table.add_column("Top%", justify="right", style="dim")
    table.add_column("Real%", justify="right", style="bold yellow")
    table.add_column("WdrFee%", justify="right")
    table.add_column("Max $", justify="right")
    table.add_column("Net", style="dim")
    table.add_column("Vrfy", justify="center")
    table.add_column("Depth", justify="center")

    for o in opps[:TOP_N]:
        vrfy = "[green]V[/green]" if o.get("verified") else ""
        wfee_str = f"{o['wfee_pct']:.3f}" if o.get("wfee_pct") is not None else "-"
        if "real_net_pct" in o:
            real_str = f"{o['real_net_pct']:.3f}"
            depth_mark = "[green]OK[/green]" if o.get("depth_full") else "[yellow]thin[/yellow]"
            max_str = f"{o['max_usd_achievable']:,.0f}"
        else:
            real_str = "-"
            depth_mark = "[dim]?[/dim]"
            max_str = "-"
        table.add_row(
            o["symbol"],
            o["buy_ex"],
            o["sell_ex"],
            f"{o['net']:.3f}",
            real_str,
            wfee_str,
            max_str,
            o.get("network") or "-",
            vrfy,
            depth_mark,
        )
    if errors:
        err_str = " | ".join(f"{k}: {str(v)[:40]}" for k, v in errors.items())
        table.caption = f"[red]errors:[/red] {err_str}"
    return table


async def main():
    console.print("[bold]Initializing exchanges...[/bold]")
    exchanges = []
    for name in EXCHANGES:
        try:
            ex = await make_exchange(name)
            exchanges.append(ex)
            tag = "[green]auth[/green]" if ex.apiKey else "[dim]public[/dim]"
            console.print(f"  ok  {name} {tag}")
        except Exception as e:
            console.print(f"  fail {name}: {e}")

    console.print("[bold]Loading currency metadata (networks, fees, deposit/withdraw status)...[/bold]")
    currencies_map = await load_all_currencies(exchanges)
    for ex_id, c in currencies_map.items():
        console.print(f"  {ex_id}: {len(c)} currencies")

    ex_by_id = {ex.id: ex for ex in exchanges}
    try:
        with Live(console=console, refresh_per_second=2, screen=False) as live:
            while True:
                t0 = time.time()
                results = await fetch_all(exchanges)
                book, errors = build_book(results)
                opps, rejected = find_opportunities(book, currencies_map)

                # Depth verification on top candidates
                top_candidates = opps[:DEPTH_CHECK_N]
                if top_candidates:
                    try:
                        books = await fetch_books_for_opps(ex_by_id, top_candidates, limit=30)
                        verified_opps = []
                        for o in top_candidates:
                            d = evaluate_depth(o, books, TARGET_POSITION_USD, TAKER_FEE)
                            if d is None:
                                rejected["no_depth_data"] += 1
                                continue
                            if d["real_net_pct"] <= 0:
                                rejected["depth_killed_net"] += 1
                                continue
                            o.update(d)
                            verified_opps.append(o)
                        verified_opps.sort(key=lambda x: x["real_net_pct"], reverse=True)
                        # keep verified at top, remainder unchecked below
                        opps = verified_opps + opps[DEPTH_CHECK_N:]
                    except Exception as e:
                        errors["depth"] = str(e)

                elapsed = time.time() - t0
                live.update(render_table(opps, errors, rejected, elapsed))
                await asyncio.sleep(max(0, REFRESH_SEC - elapsed))
    finally:
        await asyncio.gather(*(ex.close() for ex in exchanges), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
