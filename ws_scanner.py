"""WebSocket-based arbitrage scanner.
Pushes ticker updates via ccxt.pro for supported exchanges, REST fallback for others.
"""
import asyncio
import os
import tarfile
import time
from collections import defaultdict

from appdir import BASE_DIR

import ccxt.pro as ccxtpro
from ccxt.base.errors import RateLimitExceeded, ExchangeNotAvailable, DDoSProtection
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.table import Table

load_dotenv()

from logsetup import init_logging, get_logger
init_logging("scanner")
log = get_logger("scanner.main")

from currencies import load_all_currencies, can_transfer, best_withdraw_fee, contracts_match
from depth import fetch_books_for_opps, evaluate_depth, taker_fee_for
from executor import Executor, ExecConfig, Mode
from inventory import InventoryGuard, GuardConfig
from hedge import HedgeManager, HedgeConfig, hedge_watcher
from balances import RealBalanceCache, VirtualPortfolio
from bidir import BidirectionalTracker
from ws_hotset import HotSetManager


EXCHANGES = ["mexc", "bitget", "bingx"]  # funded live venues; bitget<->mexc is the money route (HOME). full list: mexc,kucoin,bitget,bingx,bitmart
# NOTE: HTX (Huobi) removed — under sanctions, no longer a supported venue.
# WS strategy:
#   "all"      = watchTickers() with no args, returns all symbols (kucoin)
#   "list"     = watchTickers([symbols]) requires explicit list (bitget, bitmart)
#   "rest"     = REST poll only (mexc, bingx — spot watchTickers not supported)
# kucoin was "all" (persistent watch_tickers WS) but at ~1s RTT the socket keeps
# dropping with "ping-pong keepalive missing" / close 1006. REST polling is
# stateless and tolerant of latency, so kucoin now polls over REST like mexc/bingx.
WS_MODE = {
    "bitget": "list", "bitmart": "list",
    "mexc": "rest", "bingx": "rest", "kucoin": "rest",
}
UNIVERSE_MIN_VOL = 200_000          # symbols to subscribe must exceed this on at least 1 ex
UNIVERSE_MAX_SIZE = 300             # global cap
PER_EXCHANGE_SUB_CAP = {            # per-exchange subscription limits (WS protocol limits)
    "bitmart": 25,
}

QUOTE = "USDT"
MIN_QUOTE_VOLUME = 300_000
TARGET_POSITION_USD = float(os.getenv("POSITION_USD", "30"))  # match depth-eval size to actual trade size (small-depo: $200 over-rejected thin books)
SUSPICIOUS_SPREAD_PCT = 5.0
HARD_MAX_SPREAD_PCT = 200.0
SCAN_INTERVAL_SEC = float(os.getenv("SCAN_INTERVAL_SEC", "1.5"))
REST_POLL_SEC = float(os.getenv("REST_POLL_SEC", "3"))  # data freshness; lower = fresher (less leg-risk), more API calls
DEPTH_CHECK_N = 12
TICKER_TTL_SEC = 5              # ignore stale tickers older than 5s (was 15) — stale-tick safety
STALE_PER_SIDE_SEC = 4          # in find_opps each side ticker must be fresher than this
TOP_N = 25

# Executor — defaults to PAPER mode (set MODE env var to 'live' to enable real trading)
EXEC_MODE = Mode.LIVE if os.getenv("MODE", "paper").lower() == "live" else Mode.PAPER
EXEC_POSITION_USD = float(os.getenv("POSITION_USD", "30"))
EXEC_MIN_NET_PCT = float(os.getenv("MIN_NET_PCT", "0.15"))

console = Console()


def _creds(name):
    upper = name.upper()
    cfg = {}
    # Accept both naming schemes: the operator's hand-written .env uses {EX}_SECRET /
    # {EX}_PASSWORD, while the desktop dashboard saves {EX}_API_SECRET /
    # {EX}_API_PASSPHRASE. Without this fallback, keys entered in the dashboard were
    # silently ignored and customer LIVE trading ran with no secret.
    k = os.getenv(f"{upper}_API_KEY")
    s = os.getenv(f"{upper}_SECRET") or os.getenv(f"{upper}_API_SECRET")
    p = os.getenv(f"{upper}_PASSWORD") or os.getenv(f"{upper}_API_PASSPHRASE")
    uid = os.getenv(f"{upper}_UID")
    if k and s:
        cfg["apiKey"] = k
        cfg["secret"] = s
    if p:
        cfg["password"] = p
    if uid:
        cfg["uid"] = uid
    return cfg


# Per-exchange option overrides. kucoin signs every request with KC-API-TIMESTAMP
# and is strict about it; under high RTT (~1s here) ccxt's load_time_difference
# over-estimates the clock offset and adjustForTimeDifference then applies a bogus
# correction, intermittently producing "Invalid KC-API-TIMESTAMP". The real local
# clock is accurate (~30ms), so kucoin is safer using raw local time.
_EX_OPTION_OVERRIDES = {
    "kucoin": {"adjustForTimeDifference": False},
}


async def make_exchange(name):
    klass = getattr(ccxtpro, name)
    options = {
        "defaultType": "spot",
        "adjustForTimeDifference": True,
        "recvWindow": 60000,
    }
    options.update(_EX_OPTION_OVERRIDES.get(name, {}))
    cfg = {
        "enableRateLimit": True,
        "newUpdates": False,
        "options": options,
    }
    cfg.update(_creds(name))
    return klass(cfg)


async def _safe_load_markets_retry(ex, attempts=3):
    last = None
    for i in range(attempts):
        try:
            await ex.load_markets()
            return True
        except Exception as e:
            last = e
            await asyncio.sleep(1 + i)
    console.print(f"  [yellow]load_markets fail {ex.id}: {str(last)[:80]}[/yellow]")
    return False


async def time_sync_loop(exchanges, interval_sec=300):
    """Periodic loadTimeDifference() to keep ccxt's clock offset fresh.
    Critical for MEXC/Binance/etc that reject requests outside recvWindow."""
    sync_log = get_logger("timesync")
    while True:
        for ex in exchanges:
            try:
                if hasattr(ex, "load_time_difference"):
                    diff = await ex.load_time_difference()
                    sync_log.debug(f"{ex.id}: clock offset = {diff}ms")
            except Exception as e:
                sync_log.warning(f"{ex.id}: time sync failed: {type(e).__name__}: {str(e)[:120]}")
        await asyncio.sleep(interval_sec)


class TickerHub:
    """Shared state of latest top-of-book per exchange + volatility tracking per symbol."""

    def __init__(self):
        self.tickers = defaultdict(dict)
        self.stats = defaultdict(lambda: {"updates": 0, "last_ts": 0, "error": ""})
        # symbol -> deque of (ts, mid_price) over last 60s for volatility est
        self.price_history = defaultdict(lambda: [])
        self.volatility_pct: dict[str, float] = {}  # symbol -> recent volatility %

    def update(self, ex_id, sym, bid, ask, qv, source="poll"):
        if not sym.endswith("/" + QUOTE):
            return
        if not bid or not ask or bid <= 0 or ask <= 0:
            return
        now = time.time()
        prev = self.tickers[ex_id].get(sym)
        # ws book updates pass qv=None — carry forward last known volume so the
        # snapshot() volume filter (qv < MIN_QUOTE_VOLUME) doesn't drop an
        # otherwise-fresh ws quote that simply lacks a quoteVolume field.
        if qv is None:
            qv = prev.get("qv", 0) if prev else 0
        self.tickers[ex_id][sym] = {"bid": bid, "ask": ask, "qv": qv or 0, "ts": now, "source": source}
        st = self.stats[ex_id]
        st["updates"] += 1
        st["last_ts"] = now
        # Update volatility for this symbol
        mid = (bid + ask) / 2
        hist = self.price_history[sym]
        hist.append((now, mid))
        cutoff = now - 60
        while hist and hist[0][0] < cutoff:
            hist.pop(0)
        if len(hist) > 5:
            prices = [p for _, p in hist]
            high, low = max(prices), min(prices)
            self.volatility_pct[sym] = (high - low) / low * 100 if low > 0 else 0

    def update_book(self, ex_id, sym, bid, ask):
        """Top-of-book from a ws watch_order_book stream (hot-set feeder).
        quoteVolume is carried forward from the last poll-based ticker."""
        self.update(ex_id, sym, bid, ask, None, source="ws")

    def ws_quote(self, ex_id, sym, max_age_sec):
        """Return (bid, ask) if a fresh ws-sourced quote exists, else None.
        Used by the executor pre-exec step to skip a REST order-book fetch."""
        t = self.tickers.get(ex_id, {}).get(sym)
        if not t or t.get("source") != "ws":
            return None
        if time.time() - t["ts"] > max_age_sec:
            return None
        return t["bid"], t["ask"]

    def momentum_pct(self, sym: str, lookback_sec: float = 45.0) -> float | None:
        """Return price change % over last lookback_sec. Negative = falling. None = not enough data."""
        hist = self.price_history.get(sym)
        if not hist or len(hist) < 3:
            return None
        now = time.time()
        cutoff = now - lookback_sec
        old = next((p for ts, p in hist if ts >= cutoff), None)
        last = hist[-1][1]
        if old is None or old <= 0:
            return None
        return (last - old) / old * 100

    def snapshot(self):
        """Return current book (filtered by TTL and min volume).
        Each row: (ex_id, bid, ask, quote_vol, ticker_age_sec)
        """
        now = time.time()
        book = defaultdict(list)
        for ex_id, sym_map in self.tickers.items():
            for sym, t in sym_map.items():
                age = now - t["ts"]
                if age > TICKER_TTL_SEC:
                    continue
                if t["qv"] < MIN_QUOTE_VOLUME:
                    continue
                book[sym].append((ex_id, t["bid"], t["ask"], t["qv"], age))
        return book


async def run_ws_all(ex, hub):
    feeder_log = get_logger(f"feeder.{ex.id}")
    while True:
        try:
            tickers = await ex.watch_tickers()
            for sym, t in tickers.items():
                hub.update(ex.id, sym, t.get("bid"), t.get("ask"), t.get("quoteVolume"))
            hub.stats[ex.id]["error"] = ""
        except Exception as e:
            hub.stats[ex.id]["error"] = str(e)[:60]
            feeder_log.warning(f"watch_tickers error: {type(e).__name__}: {str(e)[:200]}")
            await asyncio.sleep(2)


async def run_ws_list(ex, hub, symbols):
    feeder_log = get_logger(f"feeder.{ex.id}")
    if not symbols:
        hub.stats[ex.id]["error"] = "empty universe"
        feeder_log.error("empty universe — no symbols to subscribe")
        return
    feeder_log.info(f"subscribing to {len(symbols)} symbols via WS")
    while True:
        try:
            tickers = await ex.watch_tickers(symbols)
            for sym, t in tickers.items():
                hub.update(ex.id, sym, t.get("bid"), t.get("ask"), t.get("quoteVolume"))
            hub.stats[ex.id]["error"] = ""
        except Exception as e:
            hub.stats[ex.id]["error"] = str(e)[:60]
            feeder_log.warning(f"watch_tickers error: {type(e).__name__}: {str(e)[:200]}")
            await asyncio.sleep(2)


async def run_rest(ex, hub):
    feeder_log = get_logger(f"feeder.{ex.id}")
    feeder_log.info(f"REST poll every {REST_POLL_SEC}s")
    rate_streak = 0
    while True:
        try:
            tickers = await ex.fetch_tickers()
            for sym, t in tickers.items():
                hub.update(ex.id, sym, t.get("bid"), t.get("ask"), t.get("quoteVolume"))
            hub.stats[ex.id]["error"] = ""
            rate_streak = 0
            sleep = REST_POLL_SEC
        except (RateLimitExceeded, DDoSProtection) as e:
            # Server-side throttling (e.g. htx "request limit"). Back off exponentially
            # so we stop hammering the endpoint; otherwise it stays blocked.
            rate_streak += 1
            sleep = min(REST_POLL_SEC * (2 ** rate_streak), 30)
            hub.stats[ex.id]["error"] = str(e)[:60]
            feeder_log.warning(f"rate limited, backing off {sleep}s: {type(e).__name__}: {str(e)[:120]}")
        except ExchangeNotAvailable as e:
            # Transient outage/maintenance — retry soon (don't let quotes go stale > TTL)
            # but a touch slower than the happy path to avoid a tight error loop.
            rate_streak = 0
            sleep = REST_POLL_SEC
            hub.stats[ex.id]["error"] = str(e)[:60]
            feeder_log.warning(f"exchange unavailable: {type(e).__name__}: {str(e)[:120]}")
        except Exception as e:
            rate_streak = 0
            sleep = REST_POLL_SEC
            hub.stats[ex.id]["error"] = str(e)[:60]
            feeder_log.warning(f"fetch_tickers error: {type(e).__name__}: {str(e)[:200]}")
        await asyncio.sleep(sleep)


async def build_universe(exchanges):
    """Initial REST fetch_tickers across all exchanges → list of symbols
    that exist on >=2 exchanges with reasonable volume on at least one."""
    console.print("[bold]Building universe (initial REST scan)...[/bold]")
    results = await asyncio.gather(
        *(_safe_fetch_tickers(ex) for ex in exchanges), return_exceptions=True
    )
    sym_count = defaultdict(int)
    sym_max_vol = defaultdict(float)
    for ex, res in zip(exchanges, results):
        if isinstance(res, Exception) or not res:
            continue
        for sym, t in res.items():
            if not sym.endswith("/" + QUOTE):
                continue
            qv = t.get("quoteVolume") or 0
            if qv < 1000:
                continue
            sym_count[sym] += 1
            if qv > sym_max_vol[sym]:
                sym_max_vol[sym] = qv
    universe = [
        s for s, c in sym_count.items()
        if c >= 2 and sym_max_vol[s] >= UNIVERSE_MIN_VOL
    ]
    universe.sort(key=lambda s: sym_max_vol[s], reverse=True)
    universe = universe[:UNIVERSE_MAX_SIZE]
    console.print(f"  universe size: {len(universe)} pairs")
    return universe


async def _safe_fetch_tickers(ex):
    try:
        return await ex.fetch_tickers()
    except Exception as e:
        console.print(f"  [yellow]universe fetch fail {ex.id}: {str(e)[:60]}[/yellow]")
        return {}


async def _safe_load_markets(ex):
    try:
        await ex.load_markets()
    except Exception as e:
        console.print(f"  [yellow]load_markets fail {ex.id}: {str(e)[:60]}[/yellow]")


def find_opportunities(book, currencies_map):
    opps = []
    rejected = defaultdict(int)
    for sym, rows in book.items():
        # rows: (ex_id, bid, ask, qv, age)
        # Filter stale tickers per side
        fresh = [r for r in rows if len(r) < 5 or r[4] <= STALE_PER_SIDE_SEC]
        if len(fresh) < 2:
            if len(rows) >= 2:
                rejected["stale_ticker"] += 1
            continue
        base = sym.split("/")[0]
        buy = min(fresh, key=lambda r: r[2])
        sell = max(fresh, key=lambda r: r[1])
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

        ok, reason, common = can_transfer(currencies_map, buy[0], sell[0], base)
        if ok is False:
            rejected[reason] += 1
            continue

        verified = None
        if gross >= SUSPICIOUS_SPREAD_PCT:
            if ok is None:
                rejected["suspicious_no_data"] += 1
                continue
            matched, _ev = contracts_match(common)
            if matched is False:
                rejected["fake_diff_contract"] += 1
                continue
            if matched is None:
                rejected["suspicious_no_contract"] += 1
                continue
            verified = True

        wfee_base, wnet = (None, None)
        if ok and common:
            wfee_base, wnet = best_withdraw_fee(common)
        wfee_quote = wfee_base * ask if wfee_base is not None else None
        wfee_pct = (wfee_quote / TARGET_POSITION_USD * 100) if wfee_quote is not None else None

        net_after_trade = ((bid * (1 - taker_fee_for(sell[0]))) - (ask * (1 + taker_fee_for(buy[0])))) / ask * 100
        net = net_after_trade - (wfee_pct or 0)
        if net <= 0:
            rejected["negative_after_fees"] += 1
            continue

        opps.append({
            "symbol": sym, "buy_ex": buy[0], "buy_ask": ask,
            "sell_ex": sell[0], "sell_bid": bid,
            "gross": gross, "net": net, "min_vol": min(buy[3], sell[3]),
            "wfee_pct": wfee_pct, "network": wnet,
            "verified": verified,
        })
    opps.sort(key=lambda o: o["net"], reverse=True)
    return opps, rejected


def render(opps, hub: TickerHub, rejected, scan_elapsed, depth_elapsed, executor):
    age_now = time.time()
    stats_str = " ".join(
        f"{ex_id}={s['updates']}({age_now - s['last_ts']:.0f}s)" + (f"!{s['error'][:20]}" if s['error'] else "")
        for ex_id, s in hub.stats.items()
    )
    exec_color = "green" if executor.cfg.mode == Mode.PAPER else "red bold"
    guard_text = executor.guard.status_text() if executor.guard else ""
    bidir_text = ""
    if executor.bidir:
        bs = executor.bidir.stats()
        bidir_text = f"bidir {bs['bidirectional']}/{bs['total_pairs']}"
    cap_text = ""
    if executor.virtual_portfolio:
        cap_text = executor.virtual_portfolio.status_text(hub)
    elif executor.balance_cache:
        cap_text = executor.balance_cache.status_text()
    title = (
        f"[{exec_color}]{executor.status_text()}[/{exec_color}] | "
        f"[cyan]{guard_text} | {bidir_text} | {cap_text}[/cyan] | "
        f"scan {scan_elapsed*1000:.0f}ms depth {depth_elapsed*1000:.0f}ms | "
        f"opps={len(opps)}"
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
            real_str = f"[bold]{o['real_net_pct']:.3f}[/bold]"
            depth_mark = "[green]OK[/green]" if o.get("depth_full") else "[yellow]thin[/yellow]"
            max_str = f"{o['max_usd_achievable']:,.0f}"
        else:
            real_str = "[dim]?[/dim]"
            depth_mark = "[dim]?[/dim]"
            max_str = "-"
        table.add_row(
            o["symbol"], o["buy_ex"], o["sell_ex"],
            f"{o['net']:.3f}", real_str, wfee_str, max_str,
            o.get("network") or "-", vrfy, depth_mark,
        )
    return table


async def scanner_loop(hub, ex_by_id, currencies_map, executor: Executor, live, hotset=None):
    log.info("scanner_loop started")
    while True:
        try:
            await _scanner_iter(hub, ex_by_id, currencies_map, executor, live, hotset)
        except Exception as e:
            log.exception(f"scanner_loop iteration crashed: {e}")
            await asyncio.sleep(2)


async def _scanner_iter(hub, ex_by_id, currencies_map, executor: Executor, live, hotset=None):
        t0 = time.time()
        book = hub.snapshot()
        opps, rejected = find_opportunities(book, currencies_map)
        if opps:
            executor.last_opp_ts = time.time()   # liveness signal for health monitor
        scan_t = time.time() - t0

        depth_t0 = time.time()
        top = opps[:DEPTH_CHECK_N]
        # Keep the current best-spread candidates warm on ws so their quotes (and the
        # executor's pre-exec check) read real-time top-of-book instead of 3s REST.
        if hotset is not None and top:
            hotset.set_hot({o["symbol"] for o in top})
        verified = []
        if top:
            try:
                books = await fetch_books_for_opps(
                    ex_by_id, top, limit=30,
                    book_provider=(hotset.get_book if hotset is not None else None),
                )
                for o in top:
                    d = evaluate_depth(o, books, TARGET_POSITION_USD)
                    if d is None:
                        rejected["no_depth_data"] += 1
                        continue
                    if d["real_net_pct"] <= 0:
                        rejected["depth_killed_net"] += 1
                        continue
                    o.update(d)
                    verified.append(o)
                verified.sort(key=lambda x: x["real_net_pct"], reverse=True)
                opps = verified + opps[DEPTH_CHECK_N:]
            except Exception as e:
                rejected[f"depth_err:{str(e)[:20]}"] += 1
                log.warning(f"depth check batch failed: {type(e).__name__}: {str(e)[:200]}")
        depth_t = time.time() - depth_t0

        # Hand top verified opps to executor (concurrent — non-blocking)
        seen_ts = time.time()
        for o in verified[:executor.cfg.max_concurrent]:
            o["__seen_ts"] = seen_ts
            o["__hub"] = hub
            asyncio.create_task(_run_consider(executor, o))

        live.update(render(opps, hub, rejected, scan_t, depth_t, executor))
        try:
            with open(BASE_DIR / "scanner_heartbeat.txt", "w") as _hb:
                _hb.write(str(time.time()))
        except Exception:
            pass
        await asyncio.sleep(SCAN_INTERVAL_SEC)


async def _run_consider(executor, opp):
    try:
        await executor.consider(opp)
    except Exception as e:
        log.exception(f"consider() crashed for {opp.get('symbol')} {opp.get('buy_ex')}->{opp.get('sell_ex')}: {e}")


async def state_backup_loop(interval_sec=3600, keep=72):
    """In-process rotating snapshot of runtime state (guards against bad writes /
    corruption). Runs as long as the bot runs — state isn't changing while it's down.
    Snapshots into BASE_DIR/state_backups, keeping the newest `keep` (~3 days hourly)."""
    bl = get_logger("backup")
    dst = BASE_DIR / "state_backups"
    files = ["orbit.db", "executor_state.json", "inventory_state.json", "hedge_state.json",
             "virtual_portfolio.json", "balances_snapshot.json", "trades.jsonl",
             "stops.jsonl", "scanner_heartbeat.txt", ".env"]
    await asyncio.sleep(120)   # let state settle after boot, then snapshot + hourly
    while True:
        try:
            dst.mkdir(exist_ok=True)
            path = dst / f"state_{time.strftime('%Y%m%d_%H%M%S')}.tgz"
            with tarfile.open(path, "w:gz") as tar:
                for fn in files:
                    fp = BASE_DIR / fn
                    if fp.exists():
                        tar.add(fp, arcname=fn)
            snaps = sorted(dst.glob("state_*.tgz"))
            for old in snaps[:-keep]:
                try:
                    old.unlink()
                except Exception:
                    pass
            bl.info(f"state snapshot {path.name} ({min(len(snaps), keep)} kept)")
        except Exception as e:
            bl.warning(f"backup failed: {str(e)[:80]}")
        await asyncio.sleep(interval_sec)


async def health_monitor(executor, interval_sec=600):
    """Alert (telegram) on silent degradation: bot alive but no windows for
    HEALTH_NO_OPP_ALERT_H, or windows seen but no fills for HEALTH_NO_TRADE_ALERT_H.
    One alert per episode + a recovery notice. No-op without a telegram notifier."""
    hl = get_logger("health")
    no_opp_h = float(os.getenv("HEALTH_NO_OPP_ALERT_H", "2"))
    no_trade_h = float(os.getenv("HEALTH_NO_TRADE_ALERT_H", "8"))
    alerted = {"opp": False, "trade": False}
    while True:
        await asyncio.sleep(interval_sec)
        try:
            now = time.time()
            tg = executor.notifier
            opp_age_h = (now - executor.last_opp_ts) / 3600
            fill_age_h = (now - executor.last_fill_ts) / 3600
            # no windows at all — strong signal something broke (feeds/universe)
            if opp_age_h >= no_opp_h:
                hl.warning(f"no windows for {opp_age_h:.1f}h")
                if not alerted["opp"] and tg is not None:
                    await tg.broadcast(f"⚠️ Бот жив, но окон нет уже {opp_age_h:.1f}ч — проверь фиды/биржи.")
                    alerted["opp"] = True
            else:
                if alerted["opp"] and tg is not None:
                    await tg.broadcast("✅ Окна снова появляются — норма.")
                alerted["opp"] = False
            # windows exist but no fills — softer (thresholds/inventory), only warn if there ARE windows
            if opp_age_h < no_opp_h and fill_age_h >= no_trade_h:
                hl.warning(f"no fills for {fill_age_h:.1f}h (windows present)")
                if not alerted["trade"] and tg is not None:
                    await tg.broadcast(f"⚠️ Окна есть, но сделок нет уже {fill_age_h:.1f}ч — порог/инвентарь?")
                    alerted["trade"] = True
            elif fill_age_h < no_trade_h:
                if alerted["trade"] and tg is not None:
                    await tg.broadcast("✅ Сделки пошли снова.")
                alerted["trade"] = False
        except Exception as e:
            hl.warning(f"health check failed: {str(e)[:80]}")


async def hedge_reconcile_loop(executor, hedge, interval_sec=60):
    """Keep the perp short matched to the actual held SPOT inventory of each
    hedgeable whitelisted token (delta-neutral). This hedges rebalancer-seeded
    inventory (e.g. VELVET) that never passed through the executor. Single source
    of truth = real balances; respects hedge.cfg.dry_run (logs intended shorts)."""
    from executor import WHITELIST_TOKENS
    hl = get_logger("hedge")
    if hedge is None or not getattr(hedge.cfg, "enabled", False):
        hl.info("hedge reconcile loop: hedge disabled — not running")
        return
    hl.info(f"hedge reconcile loop started (interval={interval_sec}s, dry_run={hedge.cfg.dry_run})")
    await asyncio.sleep(45)   # let balances + hub marks warm up before first action
    while True:
        try:
            bc = executor.balance_cache
            exids = list(executor.ex_by_id.keys())
            now = time.time()
            # Only act on a WARM cache — a cold/stale cache reads 0 held and would
            # spuriously close real hedges. Require a recent refresh for every venue.
            cache_warm = bc is not None and all((now - bc.last_update.get(e, 0)) < 90 for e in exids)
            if cache_warm:
                # Check every hedgeable token we hold (whitelist) OR already short.
                tokens = set(WHITELIST_TOKENS) | {t for t, s in hedge.shorts.items() if s["qty"] > 0}
                for token in tokens:
                    if not hedge.can_hedge(token) or token.upper() in hedge.exclude:
                        continue
                    mark = hedge._best_mark(token)
                    if mark <= 0:
                        continue  # can't price it now — NEVER close/adjust a hedge blind
                    total = sum((bc.available(e, token) or 0) for e in exids)
                    # target short = held spot (or 0 if only dust remains)
                    target = total if (total * mark) >= hedge.cfg.min_hedge_qty_usd else 0.0
                    await hedge.adjust(token, target, mark)
        except Exception as e:
            hl.warning(f"hedge reconcile failed: {type(e).__name__}: {str(e)[:90]}")
        await asyncio.sleep(interval_sec)


async def leg_risk_logger(executor, interval_sec=900):
    """Periodically log the leg-risk telemetry summary (outcome mix, slippage,
    ws pre-exec hit rate) so tuning is data-driven."""
    rl = get_logger("legrisk")
    while True:
        await asyncio.sleep(interval_sec)
        try:
            rl.info(executor.leg_risk_summary())
        except Exception as e:
            rl.warning(f"summary failed: {str(e)[:80]}")


async def env_watch_loop(interval_sec: float = 3.0):
    """Self-restart when .env settings change.

    The dashboard writes license key / mode / API keys into .env. The scanner reads env
    only at process start, so without this the user would have to manually restart for
    changes to take effect ("after entering the license key nothing happens"). We compare
    a signature of the relevant settings and exit on change — the watchdog respawns us
    with the new env.

    PAUSED is deliberately excluded: it's a dashboard display toggle the scanner doesn't
    read at startup, so toggling pause must NOT trigger a (slow) restart.
    """
    env_path = BASE_DIR / ".env"

    def _sig():
        try:
            out = {}
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition("=")
                    k = k.strip()
                    if k == "PAUSED":
                        continue
                    out[k] = v.strip()
            return tuple(sorted(out.items()))
        except Exception:
            return None

    last = _sig()
    while True:
        await asyncio.sleep(interval_sec)
        cur = _sig()
        if cur is None:
            continue
        if last is not None and cur != last:
            log.info(".env settings changed — restarting scanner to apply")
            console.print("[yellow].env changed — restarting to apply new settings…[/yellow]")
            os._exit(3)   # watchdog respawns with the updated environment
        last = cur


async def main():
    # Eye Crypt license check.
    #   PAPER mode → soft check: never exits, so simulation always starts even with no /
    #                placeholder / expired key (fixes the crash-loop where paper never ran).
    #   LIVE  mode → hard check: a valid paid license is required to trade real money.
    global EXEC_MODE
    from license import verify_or_exit, is_placeholder_license
    _lic = os.getenv("EYECRYPT_LICENSE", "")
    if _lic == "":
        # No license configured at all → operator / source run. No backend check — this
        # is how the owner's own live bot runs (unchanged from before licensing existed).
        console.print(f"[dim]No EYECRYPT_LICENSE set — running unlicensed ({EXEC_MODE.value.upper()})[/dim]")
    elif is_placeholder_license(_lic):
        # Fresh customer who hasn't pasted a real key yet. Never trade real money and never
        # crash-loop: fall back to PAPER (simulation) until a key is entered in the cabinet.
        if EXEC_MODE == Mode.LIVE:
            console.print("[yellow]LIVE needs a license key — running PAPER until you set one in the cabinet[/yellow]")
            EXEC_MODE = Mode.PAPER
        else:
            console.print("[yellow]No license set — PAPER (simulation) only[/yellow]")
    else:
        # Real key present: verify against backend. Soft check (never sys.exit) so an
        # expired/invalid sub downgrades to PAPER instead of crash-looping the scanner.
        _tier = verify_or_exit(hard=False)
        if _tier:
            console.print(f"[bold green]Eye Crypt license OK — tier {_tier}[/bold green]")
        elif EXEC_MODE == Mode.LIVE:
            console.print("[yellow]License invalid/expired — LIVE disabled, running PAPER[/yellow]")
            EXEC_MODE = Mode.PAPER
        else:
            console.print("[yellow]No valid license — PAPER (simulation) only[/yellow]")
    console.print("[bold]Building exchanges (ccxt.pro)...[/bold]")
    exchanges = []
    for name in EXCHANGES:
        try:
            ex = await make_exchange(name)
            exchanges.append(ex)
            tag = "[green]auth[/green]" if ex.apiKey else "[dim]public[/dim]"
            mode_tag = WS_MODE.get(name, "rest")
            mode = "[cyan]WS[/cyan]" if mode_tag in ("all", "list") else "[yellow]REST[/yellow]"
            console.print(f"  ok  {name} {tag} {mode}")
        except Exception as e:
            console.print(f"  fail {name}: {e}")

    # CRITICAL: load_time_difference BEFORE any auth-requiring call (kucoin signs everything,
    # rejects requests with ±5s drift). Must run before fetch_tickers / load_currencies.
    console.print("[bold]Initial time sync (loadTimeDifference)...[/bold]")
    async def _ts(ex):
        try:
            if hasattr(ex, "load_time_difference"):
                d = await ex.load_time_difference()
                console.print(f"  {ex.id}: clock offset {d}ms")
        except Exception as e:
            console.print(f"  [yellow]{ex.id}: time sync fail {str(e)[:80]}[/yellow]")
    await asyncio.gather(*(_ts(ex) for ex in exchanges), return_exceptions=True)

    console.print("[bold]Loading currency metadata...[/bold]")
    currencies_map = await load_all_currencies(exchanges)
    for ex_id, c in currencies_map.items():
        console.print(f"  {ex_id}: {len(c)} currencies")

    universe = await build_universe(exchanges)

    # Load markets so we can filter universe per-exchange (avoid sending unknown symbols)
    console.print("[bold]Loading markets per exchange...[/bold]")
    await asyncio.gather(*(_safe_load_markets_retry(ex) for ex in exchanges), return_exceptions=True)

    hub = TickerHub()
    ex_by_id = {ex.id: ex for ex in exchanges}

    feeders = []
    for ex in exchanges:
        mode = WS_MODE.get(ex.id, "rest")
        if mode == "all":
            feeders.append(asyncio.create_task(run_ws_all(ex, hub)))
        elif mode == "list":
            ex_symbols = set(ex.symbols or [])
            ex_universe = [s for s in universe if s in ex_symbols]
            cap = PER_EXCHANGE_SUB_CAP.get(ex.id)
            if cap:
                ex_universe = ex_universe[:cap]
            console.print(f"  {ex.id}: subscribing to {len(ex_universe)}/{len(universe)} symbols")
            feeders.append(asyncio.create_task(run_ws_list(ex, hub, ex_universe)))
        else:
            feeders.append(asyncio.create_task(run_rest(ex, hub)))

    # ws hot-set order-book feeder: keeps a small set of actively-traded symbols on
    # real-time ws (watch_order_book) so their quotes are ~100-400ms fresh instead of
    # the 3s REST poll — cutting leg-risk. mexc/bingx only (bitget already bulk ws).
    hotset = None
    if os.getenv("WS_HOTSET_ENABLED", "1").lower() in ("1", "true", "yes"):
        hotset = HotSetManager(ex_by_id, hub)
        feeders.append(asyncio.create_task(hotset.run()))
        console.print(f"[cyan]ws hot-set feeder: {hotset.exchanges} (warm_ttl={hotset.warm_ttl_sec:.0f}s)[/cyan]")

    executor = Executor(ex_by_id, ExecConfig(
        mode=EXEC_MODE,
        position_size_usd=EXEC_POSITION_USD,
        # Wire POSITION_USD to the actual sizing floor/ceiling (these drive trade size,
        # not position_size_usd). Fixed size = POSITION_USD per trade.
        min_position_usd=EXEC_POSITION_USD,
        max_position_usd=EXEC_POSITION_USD,
        min_real_net_pct=EXEC_MIN_NET_PCT,
        # Skew/depth tuning (env-overridable). Defaults relaxed from the original
        # high-leg-risk era: parallel IOC legs + ws hot-set already cut leg-risk, so
        # the volatility cushion and inventory-skew were over-tightening (they were
        # inflating VELVET's effective threshold to ~1% and starving the rebalancer-
        # seeded positions). require_depth_full was also too strict for thin microcaps.
        volatility_factor=float(os.getenv("VOLATILITY_FACTOR", "0.05")),
        skew_max_tighten_pct=float(os.getenv("INV_SKEW_MAX_PCT", "0.1")),
        require_depth_full=os.getenv("REQUIRE_DEPTH_FULL", "0").lower() in ("1", "true", "yes"),
        # Adaptive IOC buffer (fix one-legged mexc sell misses on volatile microcaps)
        ioc_buffer_net_frac=float(os.getenv("IOC_BUFFER_NET_FRAC", "0.25")),
        ioc_buffer_min_pct=float(os.getenv("IOC_BUFFER_MIN_PCT", "0.15")),
        ioc_buffer_max_pct=float(os.getenv("IOC_BUFFER_MAX_PCT", "0.60")),
        journal_path="trades.jsonl",
        state_path="executor_state.json",
    ))
    # Futures client for the delta-neutral hedge (live only). 1x leverage, dry-run by
    # default — set HEDGE_DRY_RUN=0 in .env to actually place orders after a smoke test.
    hedge_dry_run = os.getenv("HEDGE_DRY_RUN", "1").lower() not in ("0", "false", "no")
    hedge_cfg = HedgeConfig(dry_run=hedge_dry_run)
    futures_client = None
    if EXEC_MODE == Mode.LIVE:
        try:
            fx = getattr(ccxtpro, hedge_cfg.futures_exchange)
            fcfg = {"enableRateLimit": True, "options": {"defaultType": "swap", "adjustForTimeDifference": True}}
            fcfg.update(_creds(hedge_cfg.futures_exchange))
            futures_client = fx(fcfg)
            console.print(f"[cyan]Hedge futures client: {hedge_cfg.futures_exchange} (dry_run={hedge_dry_run})[/cyan]")
        except Exception as e:
            console.print(f"[red]Hedge futures client init failed: {e}[/red]")
    hedge = HedgeManager(hub, ex_by_id, hedge_cfg, mode_is_live=(EXEC_MODE == Mode.LIVE),
                         futures_client=futures_client)
    guard = InventoryGuard(hub, ex_by_id, GuardConfig(), executor=executor, hedge=hedge)
    bidir = BidirectionalTracker()
    executor.guard = guard
    executor.bidir = bidir
    executor.hedge = hedge
    # Prepare live futures hedge: load perps, force 1x + one-way. No-op in paper.
    await hedge.setup()
    feeders.append(asyncio.create_task(guard.watch()))
    feeders.append(asyncio.create_task(hedge_watcher(hedge, interval_sec=60.0)))
    console.print(f"[cyan]HedgeManager: enabled (funding 0.01%/8h)[/cyan]")

    if EXEC_MODE == Mode.PAPER:
        executor.virtual_portfolio = VirtualPortfolio(
            list(ex_by_id.keys()), total_usd=executor.cfg.total_capital_usd,
            token_seed_usd=300.0,    # $300 of each whitelist token per exchange
        )
        console.print(f"[cyan]VirtualPortfolio: ${executor.cfg.total_capital_usd} USDT + $300/token/ex (whitelist only)[/cyan]")
        # Even in paper mode, if any exchange has API keys, publish a read-only real-balance
        # snapshot so the dashboard's API-keys tab can show the user their actual per-exchange
        # USDT balance + grand total. fetch_balance is read-only — safe in simulation.
        authed = {ex_id: ex for ex_id, ex in ex_by_id.items() if getattr(ex, "apiKey", None)}
        if authed:
            paper_bal_cache = RealBalanceCache(authed, refresh_sec=60, hub=hub)
            feeders.append(asyncio.create_task(paper_bal_cache.watch()))
            console.print(f"[cyan]Balance snapshot (read-only): {list(authed.keys())} every 60s[/cyan]")
    else:
        executor.balance_cache = RealBalanceCache(ex_by_id, refresh_sec=30, hub=hub)
        feeders.append(asyncio.create_task(executor.balance_cache.watch()))
        console.print(f"[cyan]RealBalanceCache: refresh every 30s[/cyan]")
        # Auto-rebalancer: keeps live positioned in the tokens producing windows on the
        # live route. Dormant unless REBALANCE_ENABLED=1; dry-run unless REBALANCE_DRY_RUN=0.
        from rebalancer import rebalance_watcher, _enabled as _rebal_enabled, _dry_run as _rebal_dry
        feeders.append(asyncio.create_task(rebalance_watcher(executor, hedge, hub, ex_by_id)))
        console.print(f"[cyan]Rebalancer: enabled={_rebal_enabled()} dry_run={_rebal_dry()}[/cyan]")

    mode_color = "green" if EXEC_MODE == Mode.PAPER else "red bold"
    console.print(f"[{mode_color}]Executor: {EXEC_MODE.value.upper()} | adaptive ${executor.cfg.min_position_usd}-${executor.cfg.max_position_usd} | min_net={EXEC_MIN_NET_PCT}%[/{mode_color}]")
    console.print(f"[cyan]InventoryGuard: stop {guard.cfg.default_threshold_pct}%, cooldown {guard.cfg.cooldown_after_stop_sec}s[/cyan]")
    console.print(f"[cyan]Capital: total=${executor.cfg.total_capital_usd}, reserve={executor.cfg.reserve_pct*100:.0f}%, max/token=${executor.cfg.max_position_per_token_usd}[/cyan]")

    feeders.append(asyncio.create_task(env_watch_loop()))
    feeders.append(asyncio.create_task(time_sync_loop(exchanges, interval_sec=300)))
    feeders.append(asyncio.create_task(leg_risk_logger(executor, interval_sec=float(os.getenv("LEGRISK_LOG_SEC", "900")))))
    feeders.append(asyncio.create_task(health_monitor(executor, interval_sec=600)))
    feeders.append(asyncio.create_task(state_backup_loop(interval_sec=3600, keep=72)))
    feeders.append(asyncio.create_task(hedge_reconcile_loop(executor, hedge, interval_sec=60)))

    # Telegram control & monitoring bot (optional — enabled via TELEGRAM_BOT_TOKEN)
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        from telegram_bot import TelegramBot
        tg = TelegramBot(executor=executor, hub=hub, ex_by_id=ex_by_id)
        executor.notifier = tg
        feeders.append(asyncio.create_task(tg.run()))
        console.print("[cyan]Telegram bot started[/cyan]")

    # Eye Crypt: report status to backend if a real license is set (skip placeholder)
    if not is_placeholder_license(os.getenv("EYECRYPT_LICENSE", "")):
        from license import status_reporter
        feeders.append(asyncio.create_task(status_reporter(executor=executor, guard=guard, hedge=hedge)))
        console.print("[cyan]Eye Crypt status reporter started (ping every 5min)[/cyan]")

    console.print(f"[bold green]Feeders started ({len(feeders)} tasks, incl. time sync). Waiting 5s for initial data...[/bold green]")
    await asyncio.sleep(5)

    # Seed virtual portfolio with token inventory now that tickers are populated
    if EXEC_MODE == Mode.PAPER and executor.virtual_portfolio is not None:
        from executor import WHITELIST_TOKENS as _wl
        executor.virtual_portfolio.seed_token_inventory(hub, universe, whitelist=_wl)

    try:
        with Live(console=console, refresh_per_second=2, screen=False) as live:
            await scanner_loop(hub, ex_by_id, currencies_map, executor, live, hotset)
    finally:
        for t in feeders:
            t.cancel()
        await asyncio.gather(*(ex.close() for ex in exchanges), return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
