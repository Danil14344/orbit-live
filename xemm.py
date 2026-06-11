"""XEMM — cross-exchange market making.

Passive limit (maker) orders on the MAKER exchange (mexc: 0% maker fee), priced
off the TAKER exchange's top-of-book plus a profit margin. When a maker order
fills, immediately hedge with an aggressive IOC on the taker exchange.

Why: the taker-taker arb only fires when books actually cross (rare, fee floor
0.15%). XEMM earns the same spread without waiting for a crossed book — any
persistent price gap > taker fee + margin is harvestable, and the maker leg is
free on mexc.

Both directions, inventory-permitting:
  - maker SELL on mexc (needs token on mexc)  -> hedge BUY on taker ex (needs USDT there)
  - maker BUY  on mexc (needs USDT on mexc)   -> hedge SELL on taker ex (needs token there)

Dry-run by default (XEMM_DRY_RUN=1): logs intended quotes/replacements, places
nothing. Flip XEMM_DRY_RUN=0 to trade.

Env knobs:
  XEMM_ENABLED=0|1        master switch (default 0)
  XEMM_DRY_RUN=1|0        default 1
  XEMM_MAKER_EX=mexc      maker venue (0% maker fee)
  XEMM_TAKER_EX=bingx     hedge venue
  XEMM_ORDER_USD          per-quote size (default POSITION_USD, min $5)
  XEMM_MIN_PROFIT_PCT     margin over taker cost per fill (default 0.25)
  XEMM_REPRICE_PCT        replace order when target moved this much (default 0.08)
  XEMM_POLL_SEC           loop tick (default 2)
  XEMM_TOKENS             comma list; default = live whitelist (rebalancer-managed)
  XEMM_MAX_TOKEN_USD      cap on net token exposure drift before quoting pauses (default 60)
"""
import asyncio
import json
import logging
import os
import time

from depth import taker_fee_for

log = logging.getLogger("xemm")

JOURNAL = "xemm_trades.jsonl"


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _enabled():
    return os.getenv("XEMM_ENABLED", "0").lower() in ("1", "true", "yes")


def _dry():
    return os.getenv("XEMM_DRY_RUN", "1").lower() not in ("0", "false", "no")


class XemmBot:
    def __init__(self, executor, hub, ex_by_id):
        self.executor = executor
        self.hub = hub
        self.ex_by_id = ex_by_id
        self.maker_id = os.getenv("XEMM_MAKER_EX", "mexc")
        self.taker_id = os.getenv("XEMM_TAKER_EX", "bingx")
        self.order_usd = max(5.0, _f("XEMM_ORDER_USD", os.getenv("POSITION_USD", "25")))
        self.min_profit_pct = _f("XEMM_MIN_PROFIT_PCT", "0.25")
        self.reprice_pct = _f("XEMM_REPRICE_PCT", "0.08")
        self.poll_sec = _f("XEMM_POLL_SEC", "2")
        self.max_token_usd = _f("XEMM_MAX_TOKEN_USD", "60")
        # symbol -> side -> {id, price, qty, filled_seen}
        self.orders: dict[str, dict[str, dict]] = {}
        self.fills = 0
        self.hedges = 0
        self.pnl_usd = 0.0
        self.errors = 0

    # ---------- data ----------
    def _tokens(self):
        env = os.getenv("XEMM_TOKENS", "")
        if env.strip():
            return [t.strip().upper() for t in env.split(",") if t.strip()]
        from executor import WHITELIST_TOKENS, BANNED_TOKENS
        return [t for t in sorted(WHITELIST_TOKENS) if t not in BANNED_TOKENS]

    async def _quote(self, ex_id, sym, max_age=5.0):
        """(bid, ask) from hub if fresh, else REST fetch_ticker."""
        t = self.hub.tickers.get(ex_id, {}).get(sym)
        if t and time.time() - t.get("ts", 0) <= max_age and t.get("bid") and t.get("ask"):
            return t["bid"], t["ask"]
        try:
            ex = self.ex_by_id[ex_id]
            tk = await asyncio.wait_for(ex.fetch_ticker(sym), timeout=5)
            bid, ask = tk.get("bid"), tk.get("ask")
            if bid and ask:
                return bid, ask
        except Exception as e:
            log.debug(f"[XEMM] quote {ex_id} {sym}: {e}")
        return None

    def _avail(self, ex_id, asset):
        bc = self.executor.balance_cache
        if bc is None:
            return 0.0
        return bc.available(ex_id, asset) or 0.0

    # ---------- order management ----------
    async def _cancel(self, sym, side):
        o = self.orders.get(sym, {}).pop(side, None)
        if not o or o.get("dry"):
            return
        try:
            await asyncio.wait_for(
                self.ex_by_id[self.maker_id].cancel_order(o["id"], sym), timeout=10)
        except Exception as e:
            log.warning(f"[XEMM] cancel {side} {sym} failed: {e}")

    async def cancel_all(self):
        for sym in list(self.orders):
            for side in list(self.orders.get(sym, {})):
                await self._cancel(sym, side)

    async def _place(self, sym, side, price, qty):
        ex = self.ex_by_id[self.maker_id]
        price = float(ex.price_to_precision(sym, price))
        qty = float(ex.amount_to_precision(sym, qty))
        if qty <= 0:
            return
        if _dry():
            self.orders.setdefault(sym, {})[side] = {
                "id": f"dry-{side}", "price": price, "qty": qty, "filled_seen": 0.0, "dry": True}
            log.info(f"[XEMM] DRY place maker {side.upper()} {sym}@{self.maker_id} "
                     f"qty={qty} px={price}")
            return
        try:
            res = await asyncio.wait_for(
                ex.create_order(sym, "limit", side, qty, price), timeout=10)
            self.orders.setdefault(sym, {})[side] = {
                "id": res["id"], "price": price, "qty": qty, "filled_seen": 0.0}
            log.info(f"[XEMM] placed maker {side.upper()} {sym} qty={qty} px={price} id={res['id']}")
        except Exception as e:
            self.errors += 1
            log.warning(f"[XEMM] place {side} {sym} failed: {e}")

    async def _hedge(self, sym, maker_side, qty, maker_price):
        """Maker filled qty -> opposite aggressive IOC on taker ex."""
        ex = self.ex_by_id[self.taker_id]
        token = sym.split("/")[0]
        q = await self._quote(self.taker_id, sym, max_age=3.0)
        if q is None:
            log.error(f"[XEMM] no taker quote for hedge {sym} — will retry next tick")
            return False
        bid, ask = q
        hedge_side = "sell" if maker_side == "buy" else "buy"
        # aggressive IOC: cross the book with a 0.3% buffer
        px = bid * 0.997 if hedge_side == "sell" else ask * 1.003
        px = float(ex.price_to_precision(sym, px))
        qty_p = float(ex.amount_to_precision(sym, qty))
        if _dry():
            log.info(f"[XEMM] DRY hedge {hedge_side.upper()} {sym}@{self.taker_id} qty={qty_p} px~{px}")
            return True
        try:
            res = await asyncio.wait_for(
                ex.create_order(sym, "limit", hedge_side, qty_p, px, {"timeInForce": "IOC"}),
                timeout=15)
            await asyncio.sleep(0.5)
            try:
                od = await asyncio.wait_for(ex.fetch_order(res["id"], sym), timeout=10)
                hfill = float(od.get("filled") or 0)
                hpx = float(od.get("average") or od.get("price") or px)
            except Exception:
                hfill, hpx = qty_p, px
            self.hedges += 1
            # pnl: maker buy low / hedge sell high (or reverse), taker fee on hedge leg only
            tf = taker_fee_for(self.taker_id)
            if maker_side == "buy":
                pnl = (hpx * (1 - tf) - maker_price) * hfill
            else:
                pnl = (maker_price - hpx * (1 + tf)) * hfill
            self.pnl_usd += pnl
            g = self.executor.guard
            if g is not None:
                g.on_fill(self.maker_id, token, maker_side, qty, maker_price)
                if hfill > 0:
                    g.on_fill(self.taker_id, token, hedge_side, hfill, hpx)
            rec = {"ts": time.time(), "symbol": sym, "maker_side": maker_side,
                   "maker_px": maker_price, "qty": qty, "hedge_filled": hfill,
                   "hedge_px": hpx, "pnl_usd": pnl}
            with open(JOURNAL, "a") as f:
                f.write(json.dumps(rec) + "\n")
            log.info(f"[XEMM] HEDGED {sym}: maker {maker_side} {qty}@{maker_price} -> "
                     f"{hedge_side} {hfill}@{hpx} pnl=${pnl:.4f} (total ${self.pnl_usd:.2f})")
            n = getattr(self.executor, "notifier", None)
            if n is not None:
                try:
                    await n.broadcast(
                        f"XEMM fill {sym}: {maker_side} {qty:.4f}@{maker_price:.6g} -> "
                        f"hedge {hedge_side} {hfill:.4f}@{hpx:.6g} pnl=${pnl:.3f}")
                except Exception:
                    pass
            if hfill < qty_p * 0.95:
                log.warning(f"[XEMM] hedge partial {hfill}/{qty_p} {sym} — residual carried by guard")
            return True
        except Exception as e:
            self.errors += 1
            log.error(f"[XEMM] hedge {hedge_side} {sym} FAILED: {e} — guard carries exposure")
            return False

    async def _check_fill(self, sym, side):
        """Poll maker order; hedge any new filled qty. Returns True if order gone."""
        o = self.orders.get(sym, {}).get(side)
        if not o:
            return True
        if o.get("dry"):
            return False
        ex = self.ex_by_id[self.maker_id]
        try:
            od = await asyncio.wait_for(ex.fetch_order(o["id"], sym), timeout=10)
        except Exception as e:
            log.debug(f"[XEMM] fetch_order {sym} {side}: {e}")
            return False
        filled = float(od.get("filled") or 0)
        new = filled - o["filled_seen"]
        if new * o["price"] >= 1.0:   # hedge in >= $1 chunks
            self.fills += 1
            ok = await self._hedge(sym, side, new, float(od.get("average") or o["price"]))
            if ok:
                o["filled_seen"] = filled
        status = (od.get("status") or "").lower()
        if status in ("closed", "canceled", "cancelled", "expired", "rejected"):
            # hedge any tail missed above $1 threshold
            tail = filled - o["filled_seen"]
            if tail * o["price"] >= 0.5:
                await self._hedge(sym, side, tail, float(od.get("average") or o["price"]))
            self.orders.get(sym, {}).pop(side, None)
            return True
        return False

    # ---------- quoting ----------
    async def _tick_symbol(self, token):
        sym = f"{token}/USDT"
        mk, tk = self.ex_by_id.get(self.maker_id), self.ex_by_id.get(self.taker_id)
        if mk is None or tk is None or sym not in (mk.markets or {}) or sym not in (tk.markets or {}):
            return
        # poll existing orders first (fills -> hedges)
        for side in ("buy", "sell"):
            await self._check_fill(sym, side)

        qt = await self._quote(self.taker_id, sym)
        qm = await self._quote(self.maker_id, sym)
        if qt is None or qm is None:
            return
        t_bid, t_ask = qt
        m_bid, m_ask = qm
        tf = taker_fee_for(self.taker_id)
        margin = (self.min_profit_pct / 100) + tf

        # maker SELL on mexc, hedge BUY on taker at t_ask. If the profitable price
        # would cross mexc's own bid (mexc rich vs taker), clamp just above the bid —
        # still maker, margin only grows.
        sell_px = max(t_ask * (1 + margin), m_bid * 1.0002)
        # maker BUY on mexc, hedge SELL on taker at t_bid; clamp just below mexc ask.
        buy_px = min(t_bid * (1 - margin), m_ask * 0.9998)

        tok_mk = self._avail(self.maker_id, token)
        usdt_mk = self._avail(self.maker_id, "USDT")
        tok_tk = self._avail(self.taker_id, token)
        usdt_tk = self._avail(self.taker_id, "USDT")
        # The exchange locks balance under our own resting maker orders — add it
        # back, otherwise the next tick sees ~0 available and cancels its own quote.
        cur_sell = self.orders.get(sym, {}).get("sell")
        cur_buy = self.orders.get(sym, {}).get("buy")
        if cur_sell and not cur_sell.get("dry"):
            tok_mk += cur_sell["qty"] - cur_sell["filled_seen"]
        if cur_buy and not cur_buy.get("dry"):
            usdt_mk += (cur_buy["qty"] - cur_buy["filled_seen"]) * cur_buy["price"]

        plans = []
        # sell side: need token on maker + USDT on taker for the hedge buy
        qty = min(self.order_usd / sell_px, tok_mk)
        if qty * sell_px >= 5 and usdt_tk >= qty * t_ask * 1.01:
            plans.append(("sell", sell_px, qty))
        # buy side: need USDT on maker + token on taker for the hedge sell.
        # Adapt to what's actually there (like the sell side) instead of
        # requiring the full order size — small quote beats no quote.
        qty = min(self.order_usd, usdt_mk * 0.99, tok_tk * buy_px) / buy_px
        if qty * buy_px >= 5:
            plans.append(("buy", buy_px, qty))

        planned_sides = {p[0] for p in plans}
        for side in ("buy", "sell"):
            cur = self.orders.get(sym, {}).get(side)
            plan = next((p for p in plans if p[0] == side), None)
            if plan is None:
                if cur:
                    await self._cancel(sym, side)
                continue
            _, px, qty = plan
            if cur and abs(cur["price"] - px) / px * 100 < self.reprice_pct:
                continue  # close enough, keep resting order (preserves queue position)
            if cur:
                await self._cancel(sym, side)
            await self._place(sym, side, px, qty)
        if planned_sides:
            log.debug(f"[XEMM] {sym} taker {t_bid}/{t_ask} maker {m_bid}/{m_ask} plans={plans}")

    async def run(self):
        log.info(f"[XEMM] start maker={self.maker_id} taker={self.taker_id} "
                 f"order=${self.order_usd} min_profit={self.min_profit_pct}% "
                 f"dry_run={_dry()}")
        last_summary = time.time()
        try:
            while True:
                try:
                    if _enabled():
                        for token in self._tokens():
                            await self._tick_symbol(token)
                    else:
                        await self.cancel_all()
                except Exception as e:
                    self.errors += 1
                    log.error(f"[XEMM] tick error: {e}")
                if time.time() - last_summary > 900:
                    last_summary = time.time()
                    log.info(f"[XEMM] summary: fills={self.fills} hedges={self.hedges} "
                             f"pnl=${self.pnl_usd:.2f} errors={self.errors} "
                             f"open={ {s: list(v) for s, v in self.orders.items() if v} }")
                await asyncio.sleep(self.poll_sec)
        finally:
            log.warning("[XEMM] loop exiting — cancelling open maker orders")
            await self.cancel_all()


async def xemm_watcher(executor, hub, ex_by_id):
    """Entry point for ws_scanner. No-op spin when disabled (env hot-reload friendly)."""
    bot = XemmBot(executor, hub, ex_by_id)
    while not _enabled():
        await asyncio.sleep(30)
    await bot.run()
