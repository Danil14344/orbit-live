"""ws hot-set order-book feeder.

Per-symbol `watch_order_book` subscriptions kept warm for a dynamic *hot set* of
symbols (those currently showing arb spreads / recently traded). Feeds fresh
top-of-book into the shared TickerHub, overriding the slower REST poll for those
symbols. This cuts leg-risk: hot symbols get ~100-400ms-fresh quotes instead of
the 3s REST cadence.

Why per-symbol: bulk ws (`watch_tickers`) is unsupported for mexc/bingx spot, and
`watch_order_book` is per-symbol. So we only subscribe the small hot set, never
the whole universe. bitget already runs on bulk ws (watch_tickers list mode), so
it's excluded here by default.

mexc has a ~8s warmup before the first ws message on a fresh subscription, so we
keep subs warm (hysteresis: `warm_ttl_sec`) instead of subscribing on-demand at
trade time — otherwise the first trade on a new symbol would race the warmup.
"""
import asyncio
import time

from logsetup import get_logger

log = get_logger("ws_hotset")

# Exchanges driven by per-symbol ws order book. bitget is also on bulk ws
# (watch_tickers, top-of-book only) — we add it here too so its full depth ladder
# is available to depth-eval on the HOME route (bitget<->mexc).
DEFAULT_WS_OB_EXCHANGES = ["mexc", "bingx", "bitget"]


class HotSetManager:
    def __init__(self, ex_by_id, hub, exchanges=None, ob_limit=20,
                 warm_ttl_sec=45.0, max_symbols=30, reconcile_sec=2.0):
        self.ex_by_id = ex_by_id
        self.hub = hub
        self.exchanges = [e for e in (exchanges or DEFAULT_WS_OB_EXCHANGES) if e in ex_by_id]
        self.ob_limit = ob_limit            # depth kept per symbol (enough for VWAP at trade size)
        self.warm_ttl_sec = warm_ttl_sec    # keep a sub warm this long after it leaves hot set
        self.max_symbols = max_symbols
        self.reconcile_sec = reconcile_sec
        self._subs: dict[tuple, asyncio.Task] = {}      # (ex_id, sym) -> watch task
        self._last_hot: dict[tuple, float] = {}          # (ex_id, sym) -> last time marked hot
        self._books: dict[tuple, dict] = {}              # (ex_id, sym) -> {bids, asks, ts}
        self.updates = 0                                  # diagnostics

    def set_hot(self, symbols):
        """Mark these symbols hot now (call each scan iteration). Non-blocking —
        records timestamps; the reconcile loop applies subscription changes."""
        now = time.time()
        for sym in list(symbols)[: self.max_symbols]:
            for ex_id in self.exchanges:
                ex = self.ex_by_id.get(ex_id)
                if ex is None or sym not in getattr(ex, "markets", {}):
                    continue
                self._last_hot[(ex_id, sym)] = now

    def active(self):
        """Currently subscribed (ex_id, sym) keys — for diagnostics/status."""
        return sorted(self._subs.keys())

    def get_book(self, ex_id, sym, max_age_sec=1.5):
        """Return a cached full ws order book {bids, asks} for (ex_id, sym) if
        fresh enough, else None. Used by depth-eval to skip a REST fetch."""
        b = self._books.get((ex_id, sym))
        if not b or time.time() - b["ts"] > max_age_sec:
            return None
        return b

    async def run(self):
        log.info(f"hot-set manager started: exchanges={self.exchanges} "
                 f"warm_ttl={self.warm_ttl_sec}s ob_limit={self.ob_limit}")
        try:
            while True:
                try:
                    self._reconcile()
                except Exception as e:
                    log.warning(f"reconcile error: {type(e).__name__}: {str(e)[:120]}")
                await asyncio.sleep(self.reconcile_sec)
        except asyncio.CancelledError:
            for task in self._subs.values():
                task.cancel()
            raise

    def _reconcile(self):
        now = time.time()
        # subscribe hot keys not yet subscribed
        for key, t in list(self._last_hot.items()):
            if now - t <= self.warm_ttl_sec and key not in self._subs:
                self._subs[key] = asyncio.create_task(self._watch(*key))
                log.info(f"subscribe {key[0]} {key[1]} (hot set size now {len(self._subs)})")
        # drop subs cold beyond ttl
        for key in list(self._subs.keys()):
            if now - self._last_hot.get(key, 0) > self.warm_ttl_sec:
                self._subs.pop(key).cancel()
                self._last_hot.pop(key, None)
                self._books.pop(key, None)
                log.info(f"unsubscribe {key[0]} {key[1]} (cold > {self.warm_ttl_sec}s)")

    async def _watch(self, ex_id, sym):
        ex = self.ex_by_id[ex_id]
        backoff = 1.0
        try:
            while True:
                try:
                    ob = await ex.watch_order_book(sym, self.ob_limit)
                    bids, asks = ob.get("bids"), ob.get("asks")
                    if bids and asks and bids[0] and asks[0]:
                        self.hub.update_book(ex_id, sym, bids[0][0], asks[0][0])
                        # cache the full ladder for depth-eval (copy refs; ccxt reuses
                        # the same dict across updates when newUpdates=False)
                        self._books[(ex_id, sym)] = {
                            "bids": list(bids), "asks": list(asks), "ts": time.time(),
                        }
                        self.updates += 1
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug(f"{ex_id} {sym} watch err: {type(e).__name__}: {str(e)[:80]}")
                    await asyncio.sleep(min(backoff, 15))
                    backoff *= 2
        except asyncio.CancelledError:
            # best-effort unsubscribe so the ws connection sheds the topic
            try:
                if hasattr(ex, "un_watch_order_book"):
                    await ex.un_watch_order_book(sym)
            except Exception:
                pass
            raise
