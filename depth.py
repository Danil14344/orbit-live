"""Order book depth check — compute VWAP for target position size, filter shallow markets."""
import asyncio
import os

# Per-exchange spot taker fees (fraction). MEXC spot taker is 0.05% (maker 0%) —
# a flat 0.1% overcharged every mexc leg by 0.05pp and hid real edges.
# Override per venue via env: TAKER_FEE_MEXC=0.0005; default via TAKER_FEE.
DEFAULT_TAKER_FEE = float(os.getenv("TAKER_FEE", "0.001"))
TAKER_FEES = {"mexc": 0.0005}
for _ex in ("mexc", "bingx", "bitget", "bitmart", "kucoin", "gate", "okx", "htx"):
    _v = os.getenv(f"TAKER_FEE_{_ex.upper()}")
    if _v:
        TAKER_FEES[_ex] = float(_v)


def taker_fee_for(ex_id: str) -> float:
    return TAKER_FEES.get(ex_id, DEFAULT_TAKER_FEE)


def vwap_buy(asks, target_usd):
    """Walk asks ladder buying for target_usd worth. Returns (avg_price, base_filled, usd_filled, fully_filled)."""
    spent = 0.0
    base = 0.0
    for row in asks:
        price, amount = row[0], row[1]
        if price is None or amount is None or price <= 0 or amount <= 0:
            continue
        level_usd = price * amount
        if spent + level_usd >= target_usd:
            need_usd = target_usd - spent
            need_base = need_usd / price
            base += need_base
            spent += need_usd
            return spent / base, base, spent, True
        spent += level_usd
        base += amount
    if base == 0:
        return None, 0, 0, False
    return spent / base, base, spent, False


def vwap_sell(bids, target_base):
    """Walk bids ladder selling target_base of coin. Returns (avg_price, base_sold, usd_received, fully_filled)."""
    sold_base = 0.0
    received = 0.0
    for row in bids:
        price, amount = row[0], row[1]
        if price is None or amount is None or price <= 0 or amount <= 0:
            continue
        if sold_base + amount >= target_base:
            need_base = target_base - sold_base
            received += need_base * price
            sold_base += need_base
            return received / sold_base, sold_base, received, True
        sold_base += amount
        received += price * amount
    if sold_base == 0:
        return None, 0, 0, False
    return received / sold_base, sold_base, received, False


async def _fetch_ob(ex, symbol, limit=30):
    try:
        ob = await ex.fetch_order_book(symbol, limit=limit)
        return ex.id, ob
    except Exception as e:
        return ex.id, {"__error__": str(e)}


async def fetch_books_for_opps(exchanges_by_id, opps, limit=30,
                               book_provider=None, ws_max_age_sec=1.5):
    """For each opp, get order books for buy_ex and sell_ex.
    Returns dict: (symbol, ex_id) -> order_book.

    If `book_provider(ex_id, sym, max_age)` is given (e.g. the ws hot-set cache),
    use its fresh ladder for hot symbols and REST-fetch only the misses — removing
    a REST round-trip from the hot path."""
    books = {}
    tasks = []
    keys = []
    seen = set()
    for o in opps:
        for ex_id in (o["buy_ex"], o["sell_ex"]):
            key = (o["symbol"], ex_id)
            if key in seen:
                continue
            seen.add(key)
            if book_provider is not None:
                ob = book_provider(ex_id, o["symbol"], ws_max_age_sec)
                if ob and ob.get("bids") and ob.get("asks"):
                    books[key] = ob
                    continue
            ex = exchanges_by_id.get(ex_id)
            if ex is None:
                continue
            keys.append(key)
            tasks.append(_fetch_ob(ex, o["symbol"], limit))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for k, r in zip(keys, results):
        if isinstance(r, Exception):
            continue
        ex_id, ob = r
        if "__error__" in ob:
            continue
        books[k] = ob
    return books


def evaluate_depth(opp, books, target_usd, taker_fee=None):
    """Recompute net using VWAP for target_usd position.
    Returns updated dict with: vwap_ask, vwap_bid, real_net_pct, max_usd_achievable.
    None if depth insufficient or arbitrage disappears."""
    ob_buy = books.get((opp["symbol"], opp["buy_ex"]))
    ob_sell = books.get((opp["symbol"], opp["sell_ex"]))
    if not ob_buy or not ob_sell:
        return None
    asks = ob_buy.get("asks") or []
    bids = ob_sell.get("bids") or []
    if not asks or not bids:
        return None

    vwap_a, base_bought, _usd_b, full_a = vwap_buy(asks, target_usd)
    if vwap_a is None:
        return None
    vwap_b, _base_s, usd_received, full_b = vwap_sell(bids, base_bought)
    if vwap_b is None:
        return None

    # Recompute net with VWAP prices
    # buy at vwap_a (pay fee), sell at vwap_b (pay fee), withdraw fee already in opp
    gross_pct = (vwap_b - vwap_a) / vwap_a * 100
    fee_buy = taker_fee_for(opp["buy_ex"]) if taker_fee is None else taker_fee
    fee_sell = taker_fee_for(opp["sell_ex"]) if taker_fee is None else taker_fee
    net_after_trade = ((vwap_b * (1 - fee_sell)) - (vwap_a * (1 + fee_buy))) / vwap_a * 100
    wfee_pct = opp.get("wfee_pct") or 0
    real_net = net_after_trade - wfee_pct

    # Max achievable USD — bounded by both ladders
    max_usd = min(target_usd if full_a else target_usd * (base_bought * vwap_a / target_usd),
                  usd_received if full_b else usd_received)

    return {
        "vwap_ask": vwap_a,
        "vwap_bid": vwap_b,
        "real_gross_pct": gross_pct,
        "real_net_pct": real_net,
        "depth_full": full_a and full_b,
        "max_usd_achievable": max_usd,
    }
