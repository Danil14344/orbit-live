"""Auto-rebalancer — keeps the LIVE bot positioned in the tokens that are actually
producing arb windows on the live route (mexc<->bingx), by converting USDT<->token
inventory automatically.

Why this exists: simultaneous spot-spot arb needs the token pre-held on the SELL
exchange (executor.allowed() rejects "no {token} on {sell_ex}"). You can't synthesize
sell-side inventory mid-trade. So to trade a token you must hold it beforehand. This
task watches the PAPER instance (which scans all tokens) to learn which tokens have
recurring mexc<->bingx windows, then seeds a small inventory of the top ones on both
exchanges and updates the live WHITELIST in-process (no restart).

SAFETY: does nothing unless REBALANCE_ENABLED=1. Defaults to REBALANCE_DRY_RUN=1
(logs intended buys/sells/whitelist changes, places no orders). Flip DRY_RUN to 0
only after watching the plan in the logs. LIVE mode only.

Guardrails:
  - only tokens with >= REBALANCE_MIN_WINDOWS distinct windows in the lookback
  - top REBALANCE_MAX_TOKENS only (small depo => concentrate)
  - per-token cap REBALANCE_PER_TOKEN_USD on EACH exchange
  - keep >= REBALANCE_USDT_RESERVE_PCT of capital in USDT (buy-leg fuel)
  - liquidity check: skip tokens whose book can't fill the position within slip cap
  - IOC-limit orders with REBALANCE_MAX_SLIP_PCT cap
  - drop hysteresis: a held token is only sold after it stays off-target for
    REBALANCE_DROP_GRACE consecutive cycles
"""
import asyncio
import json
import os
import re
import statistics
import time
from collections import defaultdict

from appdir import BASE_DIR
from logsetup import get_logger
from depth import vwap_buy, vwap_sell

log = get_logger("rebalancer")

LIVE_ROUTE = {"mexc", "bitget", "bingx"}


def _f(name, default):
    return float(os.getenv(name, default))


def _i(name, default):
    return int(float(os.getenv(name, default)))


def _enabled():
    return os.getenv("REBALANCE_ENABLED", "0").lower() in ("1", "true", "yes")


def _dry_run():
    return os.getenv("REBALANCE_DRY_RUN", "1").lower() not in ("0", "false", "no")


def _paper_trades_path():
    # sibling orbit_paper dir holds the all-token scan data
    return os.path.join(os.path.dirname(str(BASE_DIR)), "orbit_paper", "trades.jsonl")


def analyze_paper(path, lookback_h, min_windows, window_gap_sec=600):
    """Return ranked list of (token, {windows, trades, pnl, avg_net}) for tokens
    that produced recurring windows on the live route within lookback."""
    cutoff = time.time() - lookback_h * 3600
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if (r.get("status") == "ok" and r.get("ts", 0) >= cutoff
                        and r.get("buy_ex") in LIVE_ROUTE and r.get("sell_ex") in LIVE_ROUTE):
                    rows.append(r)
    except FileNotFoundError:
        log.warning(f"paper trades not found: {path}")
        return []

    by_tok = defaultdict(list)
    for r in rows:
        by_tok[r["symbol"].split("/")[0]].append(r)

    out = []
    for tok, rs in by_tok.items():
        ts = sorted(x["ts"] for x in rs)
        windows = 1
        for a, b in zip(ts, ts[1:]):
            if b - a > window_gap_sec:
                windows += 1
        nets = [x.get("actual_net_pct", 0) for x in rs]
        pnl = sum(x.get("actual_pnl_usd", 0) for x in rs)
        if windows >= min_windows:
            out.append((tok, {"windows": windows, "trades": len(rs),
                              "pnl": pnl, "avg_net": sum(nets) / len(nets) if nets else 0}))
    # rank: more windows first, then pnl
    out.sort(key=lambda x: (x[1]["windows"], x[1]["pnl"]), reverse=True)
    return out


def analyze_shadow(path, lookback_h, min_windows, min_net_pct=0.0, window_gap_sec=600,
                   max_net_pct=5.0):
    """Like analyze_paper but reads the LIVE instance's own shadow opportunity log
    (tier1_shadow.jsonl / shadow_opps.jsonl) — every detected window on the live
    route, not just executed trades. Use on the VPS where there's no paper sibling.
    Handles both field spellings (net_pct / real_net_pct, would_pnl_usd).

    A token only qualifies if the MEDIAN net% of its windows in the lookback is
    >= min_net_pct — i.e. it must currently clear the live trade threshold, not just
    have spiked once. This prevents chasing decayed listing-bursts (e.g. VELVET, whose
    spread compressed below min_net while a stale 24h avg still looked great)."""
    cutoff = time.time() - lookback_h * 3600
    by_tok = defaultdict(list)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("ts", 0) < cutoff:
                    continue
                if r.get("buy_ex") not in LIVE_ROUTE or r.get("sell_ex") not in LIVE_ROUTE:
                    continue
                sym = r.get("symbol") or ""
                if "/" not in sym:
                    continue
                tok = sym.split("/")[0]
                net = r.get("net_pct", r.get("real_net_pct", 0)) or 0
                pnl = r.get("would_pnl_usd", 0) or 0
                by_tok[tok].append((r["ts"], net, pnl))
    except FileNotFoundError:
        log.warning(f"shadow log not found: {path}")
        return []

    out = []
    for tok, rows in by_tok.items():
        # The executor trades the TAIL of the distribution, not the median tick:
        # a token oscillating around 0.1% with frequent 0.4-0.8% bursts is highly
        # tradeable (VELVET: 5251 above-threshold opps rejected overnight on a low
        # all-tick median). So gate/count on the rows that actually clear the live
        # threshold, not on the median of everything.
        qual = [x for x in rows if x[1] >= min_net_pct]
        if not qual:
            continue
        ts = sorted(x[0] for x in qual)
        windows = 1
        for a, b in zip(ts, ts[1:]):
            if b - a > window_gap_sec:
                windows += 1
        nets = [x[1] for x in qual]
        med_net = statistics.median(nets)
        pnl = sum(x[2] for x in qual)
        # Sanity gate: a median "spread" this wide isn't arb — it's a venue where
        # deposits/withdrawals are suspended or the token is being delisted
        # (e.g. BTW showed median 35% net). Never seed these.
        if med_net > max_net_pct:
            log.warning(f"[REBAL] skip {tok}: qualifying-median net {med_net:.1f}% > {max_net_pct:.1f}% — "
                        f"suspicious (likely suspended transfers/delisting)")
            continue
        if windows >= min_windows:
            out.append((tok, {"windows": windows, "trades": len(qual), "pnl": pnl,
                              "avg_net": sum(nets) / len(nets),
                              "median_net": med_net}))
    # rank by captured-able money (sum of above-threshold would-pnl), then windows
    out.sort(key=lambda x: (x[1]["pnl"], x[1]["windows"]), reverse=True)
    return out


def _ranked_candidates(lookback_h, min_windows):
    """Pick the window-history source. 'shadow' = live instance's own detected
    windows (default on VPS); 'paper' = sibling orbit_paper trades.jsonl."""
    source = os.getenv("REBALANCE_SOURCE", "paper").lower()
    if source == "shadow":
        shadow_path = os.getenv("REBALANCE_SHADOW_LOG", str(BASE_DIR / "tier1_shadow.jsonl"))
        # require median window net >= live trade threshold so we don't seed a token
        # whose spread already compressed below what the executor will trade.
        min_net = float(os.getenv("REBALANCE_MIN_NET_PCT", os.getenv("MIN_NET_PCT", "0.3")))
        max_net = float(os.getenv("REBALANCE_MAX_NET_PCT", "5.0"))
        return analyze_shadow(shadow_path, lookback_h, min_windows, min_net_pct=min_net,
                              max_net_pct=max_net)
    return analyze_paper(_paper_trades_path(), lookback_h, min_windows)


async def _book_supports(ex, sym, usd, slip_cap_pct):
    """True if buying `usd` fills fully within slip_cap of top ask."""
    try:
        ob = await asyncio.wait_for(ex.fetch_order_book(sym, limit=20), timeout=5.0)
    except Exception as e:
        log.warning(f"[REBAL] depth fetch {sym}@{ex.id} failed: {str(e)[:60]}")
        return False, 0.0
    asks = ob.get("asks") or []
    if not asks:
        return False, 0.0
    top = asks[0][0]
    vwap, base, spent, full = vwap_buy(asks, usd)
    if not full or vwap is None or top <= 0:
        return False, top
    slip = (vwap / top - 1) * 100
    return slip <= slip_cap_pct, top


async def _reconcile_fill(ex, res, sym):
    if not isinstance(res, dict):
        return 0.0, 0.0
    f = res.get("filled")
    p = res.get("average") or res.get("price")
    if f is not None and p:
        return float(f), float(p)
    oid = res.get("id")
    if oid:
        try:
            tr = await asyncio.wait_for(ex.fetch_my_trades(sym, limit=20), timeout=8.0)
            lots = [t for t in tr if str(t.get("order")) == str(oid)]
            if lots:
                qty = sum(float(t.get("amount") or 0) for t in lots)
                cost = sum(float(t.get("cost") or 0) for t in lots)
                if qty > 0:
                    return qty, cost / qty
        except Exception as e:
            log.warning(f"[REBAL] reconcile {sym}@{ex.id}: {str(e)[:60]}")
    return float(f or 0), float(p or 0)


async def _buy(ex, sym, usd, slip_cap_pct, dry):
    ob = await asyncio.wait_for(ex.fetch_order_book(sym, limit=10), timeout=5.0)
    ask = ob["asks"][0][0]
    amount = float(ex.amount_to_precision(sym, usd / ask))
    if dry:
        log.info(f"[REBAL DRY] would BUY {amount} {sym}@{ex.id} ioc<= ask×(1+{slip_cap_pct}%) (~${usd:.2f})")
        return amount * ask
    # Some venues (bitget) reject limit-buy prices too far above market (price band,
    # err 41118). Retry with progressively tighter buffer so the seed still goes in.
    last = None
    for buf in (slip_cap_pct, 0.15, 0.05, 0.0):
        price = float(ex.price_to_precision(sym, ask * (1 + buf / 100)))
        try:
            o = await asyncio.wait_for(ex.create_order(sym, "limit", "buy", amount, price,
                                                       {"timeInForce": "IOC"}), timeout=15.0)
            f, avg = await _reconcile_fill(ex, o, sym)
            log.info(f"[REBAL] BUY {sym}@{ex.id} filled={f} avg={avg} (~${f * avg:.2f}) buf={buf}%")
            return f * avg
        except Exception as e:
            last = e
            if "41118" in str(e) or "price" in str(e).lower():
                continue  # price-band reject -> tighter buffer
            raise
    raise last


async def _sell_all(ex, base, slip_cap_pct, balance_cache, dry):
    sym = f"{base}/USDT"
    qty = balance_cache.available(ex.id, base) if balance_cache else 0
    if qty <= 0:
        return 0.0
    try:
        amount = float(ex.amount_to_precision(sym, qty))
    except Exception:
        amount = qty
    if amount <= 0:
        return 0.0
    if dry:
        log.info(f"[REBAL DRY] would SELL {amount} {sym}@{ex.id} (liquidate)")
        return 0.0
    o = await asyncio.wait_for(ex.create_market_sell_order(sym, amount), timeout=15.0)
    f, avg = await _reconcile_fill(ex, o, sym)
    log.info(f"[REBAL] SELL {sym}@{ex.id} filled={f} avg={avg} (~${f * avg:.2f})")
    return f * avg


def _mark(hub, base):
    best = 0.0
    for _ex, tickers in hub.tickers.items():
        t = tickers.get(f"{base}/USDT")
        if t:
            mid = ((t.get("bid") or 0) + (t.get("ask") or 0)) / 2
            best = max(best, mid)
    return best


async def rebalance_once(executor, hedge, hub, ex_by_id, off_target_streak):
    """One rebalance pass. Returns the chosen target set (or None if skipped)."""
    from executor import WHITELIST_TOKENS

    dry = _dry_run()
    max_tokens = _i("REBALANCE_MAX_TOKENS", "3")
    per_token_usd = _f("REBALANCE_PER_TOKEN_USD", str(max(20.0, _f("POSITION_USD", "15") * 2)))
    min_windows = _i("REBALANCE_MIN_WINDOWS", "2")
    lookback_h = _f("REBALANCE_LOOKBACK_H", "24")
    reserve_pct = _f("REBALANCE_USDT_RESERVE_PCT", "40")
    slip_cap = _f("REBALANCE_MAX_SLIP_PCT", "2.5")
    drop_grace = _i("REBALANCE_DROP_GRACE", "2")

    bc = executor.balance_cache
    if bc is None:
        log.warning("[REBAL] no balance_cache (not live?) — skip")
        return None

    ranked = _ranked_candidates(lookback_h, min_windows)
    if not ranked:
        log.info(f"[REBAL] no qualifying tokens (>= {min_windows} windows in {lookback_h:.0f}h, "
                 f"source={os.getenv('REBALANCE_SOURCE', 'paper')}) — keep current whitelist")
        return None

    # held inventory value per token (across live route)
    def held_usd(tok):
        m = _mark(hub, tok)
        return sum((bc.available(exid, tok) or 0) for exid in LIVE_ROUTE) * m

    # capacity: how many tokens can we actually fund without breaching the USDT reserve?
    # (don't chase more tokens than the depo can seed on both legs — avoids perpetual
    #  "add=[X] -> skip reserve" churn for tokens that will never fit.)
    _usdt = sum((bc.available(e, "USDT") or 0) for e in LIVE_ROUTE)
    _cap = _usdt + sum(held_usd(t) for t in (set(WHITELIST_TOKENS) | {x[0] for x in ranked}))
    _deployable = _cap * (1 - reserve_pct / 100)
    max_fundable = max(1, int(_deployable // (per_token_usd * len(LIVE_ROUTE))))
    cap_tokens = min(max_tokens, max_fundable)

    # liquidity-filter candidates until we have cap_tokens
    target = []
    from executor import BANNED_TOKENS
    for tok, stats in ranked:
        if len(target) >= cap_tokens:
            break
        if tok in BANNED_TOKENS:
            log.info(f"[REBAL] skip {tok}: banned")
            continue
        sym = f"{tok}/USDT"
        ok_all = True
        for exid in LIVE_ROUTE:
            ex = ex_by_id.get(exid)
            if ex is None or sym not in (ex.markets or {}):
                ok_all = False
                break
            ok, _top = await _book_supports(ex, sym, per_token_usd, slip_cap)
            if not ok:
                ok_all = False
                break
        if ok_all:
            target.append(tok)
            log.info(f"[REBAL] candidate {tok}: windows={stats['windows']} trades={stats['trades']} "
                     f"pnl=${stats['pnl']:.2f} median_net={stats.get('median_net', 0):.2f}% "
                     f"avg_net={stats['avg_net']:.2f}% — liquidity OK")
        else:
            log.info(f"[REBAL] skip {tok}: insufficient liquidity for ${per_token_usd:.0f} within {slip_cap}%")

    if not target:
        log.info("[REBAL] no liquid candidates — keep current whitelist")
        return None

    target_set = set(target)
    current = set(WHITELIST_TOKENS)

    # Auto-ping (telegram) when a NEW qualifying candidate appears — esp. in dry-run,
    # so the user learns an edge showed up and can flip REBALANCE_DRY_RUN=0 to trade it.
    notifier = getattr(executor, "notifier", None)
    if notifier is not None and target_set:
        prev = getattr(executor, "_rebal_notified", set())
        new = target_set - prev
        if new:
            try:
                stats_by = {t: s for t, s in ranked}
                lines = [f"{t}: median_net {stats_by.get(t, {}).get('median_net', 0):.2f}%, "
                         f"{stats_by.get(t, {}).get('windows', 0)} окон" for t in sorted(new)]
                tail = ("\n\nЭто DRY-RUN — флипни REBALANCE_DRY_RUN=0, чтобы завести инвентарь и торговать."
                        if dry else "\n\nLIVE — ребалансер заводит инвентарь сам.")
                await notifier.broadcast("📊 Ребалансер: квалифицирован кандидат\n" + "\n".join(lines) + tail)
            except Exception:
                pass
    executor._rebal_notified = set(target_set)

    to_add = [t for t in target if held_usd(t) < per_token_usd * 0.5]   # under half target => top up
    # drop: held/whitelisted tokens no longer in target, with hysteresis
    candidates_drop = [t for t in current if t not in target_set]
    to_drop = []
    for t in list(off_target_streak.keys()):
        if t not in candidates_drop:
            off_target_streak.pop(t, None)
    for t in candidates_drop:
        off_target_streak[t] = off_target_streak.get(t, 0) + 1
        if off_target_streak[t] >= drop_grace:
            to_drop.append(t)

    # USDT reserve guard
    total_usdt = sum((bc.available(exid, "USDT") or 0) for exid in LIVE_ROUTE)
    total_token = sum(held_usd(t) for t in (current | target_set))
    total_cap = total_usdt + total_token
    reserve_floor = total_cap * reserve_pct / 100

    log.info(f"[REBAL] {'DRY-RUN' if dry else 'LIVE'} | target={sorted(target_set)} current={sorted(current)} "
             f"add={to_add} drop={to_drop} | USDT=${total_usdt:.0f} token=${total_token:.0f} reserve_floor=${reserve_floor:.0f}")

    # --- SELL drops first (frees USDT) ---
    for tok in to_drop:
        for exid in LIVE_ROUTE:
            ex = ex_by_id.get(exid)
            if ex is not None:
                try:
                    await _sell_all(ex, tok, slip_cap, bc, dry)
                except Exception as e:
                    log.error(f"[REBAL] sell {tok}@{exid} failed: {str(e)[:80]}")
        off_target_streak.pop(tok, None)

    # --- BUY adds (respect USDT reserve) ---
    # funded = target tokens we actually hold enough of (don't whitelist a token we
    # can't sell — executor would just reject it for missing inventory).
    funded = {t for t in target_set if t not in to_add}
    spent = 0.0
    for tok in to_add:
        sym = f"{tok}/USDT"
        for exid in LIVE_ROUTE:
            ex = ex_by_id.get(exid)
            if ex is None or sym not in (ex.markets or {}):
                continue
            usdt_here = bc.available(exid, "USDT") or 0
            need = per_token_usd - (bc.available(exid, tok) or 0) * _mark(hub, tok)
            if need <= 1:
                continue
            if (total_usdt - spent - need) < reserve_floor:
                log.info(f"[REBAL] skip buy {tok}@{exid}: would breach USDT reserve")
                continue
            if usdt_here < need * 1.02:
                log.info(f"[REBAL] skip buy {tok}@{exid}: USDT here ${usdt_here:.0f} < need ${need:.0f}")
                continue
            try:
                got = await _buy(ex, sym, need, slip_cap, dry)
                spent += got
                if got > 0:
                    funded.add(tok)
            except Exception as e:
                log.error(f"[REBAL] buy {tok}@{exid} failed: {str(e)[:80]}")

    # --- Apply whitelist + hedge-exclude (only when actually trading) ---
    # Only whitelist funded tokens; never wipe to empty (would halt all trading).
    new_wl = (funded | (current & target_set)) or current
    if not dry:
        WHITELIST_TOKENS.clear()
        WHITELIST_TOKENS.update(new_wl)
        # Hedge what we CAN (has a perp); leave only un-hedgeable microcaps excluded.
        # hedge_reconcile_loop then shorts the held inventory of the hedgeable ones.
        for t in new_wl:
            if hedge.can_hedge(t):
                hedge.exclude.discard(t)
            else:
                hedge.exclude.add(t)
        log.info(f"[REBAL] whitelist updated in-process -> {sorted(WHITELIST_TOKENS)}; "
                 f"hedge.exclude={sorted(hedge.exclude)} (hedgeable seeded tokens will be shorted)")
    else:
        log.info(f"[REBAL DRY] would set whitelist -> {sorted(new_wl)} (no change applied)")

    return target_set


def _whitelist_reject_counts(executor):
    """token -> count of 'not in whitelist (TOK)' rejects accumulated by the executor."""
    out = {}
    for k, v in getattr(executor, "rejects", {}).items():
        m = re.match(r"not in whitelist \((\w+)\)", k)
        if m:
            out[m.group(1)] = v
    return out


async def rebalance_watcher(executor, hedge, hub, ex_by_id):
    """Background loop. No-op unless REBALANCE_ENABLED=1. LIVE only.

    Runs a pass every REBALANCE_INTERVAL_MIN, but ALSO fires early when the
    executor is actively rejecting opportunities for being off-whitelist
    (REBALANCE_TRIGGER_REJECTS new rejects on one token, default 20) — bursts
    last minutes; a 30-min cadence sleeps through them."""
    interval = _f("REBALANCE_INTERVAL_MIN", "30") * 60
    trigger_rejects = _i("REBALANCE_TRIGGER_REJECTS", "20")
    min_gap = _f("REBALANCE_MIN_GAP_SEC", "120")
    off_target_streak: dict[str, int] = {}
    log.info(f"rebalancer watcher started (enabled={_enabled()}, dry_run={_dry_run()}, "
             f"interval={interval/60:.0f}min, trigger_rejects={trigger_rejects})")
    # small initial delay so balances/tickers are warm
    await asyncio.sleep(60)
    last_pass = 0.0
    seen = _whitelist_reject_counts(executor)
    while True:
        try:
            now = time.time()
            due = now - last_pass >= interval
            if not due and now - last_pass >= min_gap:
                cur = _whitelist_reject_counts(executor)
                from executor import BANNED_TOKENS
                for tok, n in cur.items():
                    if tok not in BANNED_TOKENS and n - seen.get(tok, 0) >= trigger_rejects:
                        log.info(f"[REBAL] early trigger: {n - seen.get(tok, 0)} new off-whitelist "
                                 f"rejects on {tok} — running pass now")
                        due = True
                        break
            if due and _enabled():
                last_pass = now
                seen = _whitelist_reject_counts(executor)
                await rebalance_once(executor, hedge, hub, ex_by_id, off_target_streak)
            # else: stay dormant but keep the loop alive so toggling env (on restart) works
        except Exception as e:
            log.exception(f"rebalancer error: {e}")
        await asyncio.sleep(30)
