"""Delta-neutral hedging via perpetual futures short.

In live mode: opens/adjusts short futures position to offset spot inventory drift.
In paper mode: simulates the hedge alongside virtual spot inventory and tracks
mark-to-market PnL so we can see what the hedge would have saved.

Funding fees accrue every 8h (assumed 0.01%/8h average — adjustable).
"""
import asyncio
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass

from logsetup import get_logger
log = get_logger("hedge")


@dataclass
class HedgeConfig:
    enabled: bool = True
    futures_exchange: str = "bingx"        # which ex to route futures on (live)
    funding_rate_8h: float = 0.0001        # 0.01% / 8h average (paper sim)
    state_path: str = "hedge_state.json"
    min_hedge_qty_usd: float = 5.0         # don't bother hedging dust
    # 1x default — no liquidation headroom games. HEDGE_LEVERAGE env can raise it
    # when margin is the binding constraint (isolated; loss capped at posted margin).
    leverage: int = int(os.getenv("HEDGE_LEVERAGE", "1"))
    # When True, live hedge LOGS the order it would place but does NOT send it.
    # Flip to False (HEDGE_DRY_RUN=0) only after a funded smoke test.
    dry_run: bool = True


class HedgeManager:
    """Tracks a virtual (paper) or real (live) short futures position per token.

    Invariant: short_qty[token] mirrors spot_long_qty[token].
    PnL on short = (entry_avg - current_mark) * qty   (positive when price falls)
    """
    def __init__(self, hub, ex_by_id, config: HedgeConfig, mode_is_live: bool = False,
                 futures_client=None):
        self.hub = hub
        self.ex_by_id = ex_by_id
        self.cfg = config
        self.live = mode_is_live
        self.futures = futures_client          # ccxt async swap exchange (live only)
        self.perp_symbol: dict[str, str] = {}  # token -> unified perp symbol, built in setup()
        self._setup_done = False
        self._lev_set: set[str] = set()        # perps we've already forced to 1x (lazy)
        # token -> {"qty": float, "entry_avg": float, "opened_ts": float, "last_funding_ts": float}
        self.shorts: dict = defaultdict(lambda: {"qty": 0.0, "entry_avg": 0.0,
                                                 "opened_ts": 0.0, "last_funding_ts": 0.0})
        self.realized_pnl_usd: float = 0.0      # closed shorts cumulative
        self.funding_paid_usd: float = 0.0
        # Tokens to leave UNHEDGED on purpose (env HEDGE_EXCLUDE="ULTIMA,FOO").
        # adjust() returns early for these, so no short is ever opened/tracked for them.
        self.exclude: set[str] = {
            t.strip().upper() for t in os.getenv("HEDGE_EXCLUDE", "").split(",") if t.strip()
        }
        if self.exclude:
            log.info(f"[HEDGE] excluded (will stay UNHEDGED): {sorted(self.exclude)}")
        self._load_state()

    async def setup(self, tokens=None):
        """Load futures markets, map tokens to USDT-perp symbols, force 1x leverage
        and one-way position mode. Safe to call once at startup. No-op without a
        futures client (paper mode)."""
        if self.futures is None or self._setup_done:
            self._setup_done = True
            return
        try:
            await self.futures.load_markets()
        except Exception as e:
            log.error(f"[HEDGE setup] futures load_markets failed: {e}")
            return
        # one-way mode removes positionSide ambiguity on open/close
        try:
            await self.futures.set_position_mode(False)
        except Exception as e:
            log.debug(f"[HEDGE setup] set_position_mode(one-way) skipped: {str(e)[:80]}")
        for sym, m in self.futures.markets.items():
            if m.get("swap") and m.get("quote") == "USDT" and m.get("settle") == "USDT":
                base = m.get("base")
                if base and (tokens is None or base in tokens):
                    self.perp_symbol[base] = sym
        # Sync tracked shorts to ACTUAL open futures positions. Persisted state (or
        # dry-run bookkeeping) can otherwise create a phantom short across restarts —
        # which both blocks the real short from opening (delta=0) and triggers bogus
        # reduceOnly closes. Reality from the exchange is the source of truth.
        try:
            positions = await self.futures.fetch_positions()
            real = {}
            for p in positions:
                c = float(p.get("contracts") or 0)
                if c > 0 and p.get("side") == "short":
                    base = (p.get("symbol") or "").split("/")[0]
                    real[base] = float(p.get("entryPrice") or 0)
            for token in list(self.shorts.keys()):
                if token not in real:
                    self.shorts[token] = {"qty": 0.0, "entry_avg": 0.0, "opened_ts": 0.0, "last_funding_ts": 0.0}
            for base, entry in real.items():
                s = self.shorts[base]
                s["qty"] = next(float(p.get("contracts") or 0) for p in positions
                                if (p.get("symbol") or "").split("/")[0] == base and p.get("side") == "short")
                s["entry_avg"] = entry
                if s["opened_ts"] == 0:
                    s["opened_ts"] = time.time(); s["last_funding_ts"] = time.time()
            self._save_state()
            log.info(f"[HEDGE setup] synced shorts to real positions: "
                     f"{ {k: round(v['qty'], 4) for k, v in self.shorts.items() if v['qty'] > 0} }")
        except Exception as e:
            log.warning(f"[HEDGE setup] position sync skipped: {str(e)[:90]}")
        # Leverage is forced to 1x lazily on first order per symbol (see _place) to
        # avoid hammering the API with hundreds of set_leverage calls at startup.
        self._setup_done = True
        log.info(f"[HEDGE setup] live futures ready on {self.cfg.futures_exchange}: "
                 f"{len(self.perp_symbol)} perps mapped, leverage={self.cfg.leverage}x (lazy), dry_run={self.cfg.dry_run}")

    def can_hedge(self, token: str) -> bool:
        return token in self.perp_symbol

    async def sync_shorts_to_real(self):
        """Re-sync tracked short qty/entry to the ACTUAL open futures positions.
        Self-heals any drift between book and exchange (e.g. a margin-capped order
        that under-filled, or a manual close) so the reconcile loop never trusts a
        phantom short. Called periodically. Live only."""
        if not self.live or self.cfg.dry_run or self.futures is None:
            return
        try:
            positions = await self.futures.fetch_positions()
        except Exception as e:
            log.debug(f"[HEDGE] sync_shorts_to_real fetch failed: {str(e)[:90]}")
            return
        real = {}
        for p in positions:
            c = float(p.get("contracts") or 0)
            if c > 0 and p.get("side") == "short":
                base = (p.get("symbol") or "").split("/")[0]
                real[base] = (c, float(p.get("entryPrice") or 0))
        changed = False
        for token in list(self.shorts.keys()):
            if token not in real and self.shorts[token]["qty"] > 0:
                log.warning(f"[HEDGE sync] {token}: tracked short {self.shorts[token]['qty']:.4f} "
                            f"but exchange has none — correcting to 0 (was phantom)")
                self.shorts[token] = {"qty": 0.0, "entry_avg": 0.0, "opened_ts": 0.0, "last_funding_ts": 0.0}
                changed = True
        for base, (qty, entry) in real.items():
            s = self.shorts[base]
            if abs(s["qty"] - qty) > max(1e-9, qty * 0.001):
                log.warning(f"[HEDGE sync] {base}: tracked short {s['qty']:.4f} -> real {qty:.4f} (corrected)")
                changed = True
            s["qty"] = qty
            if entry > 0:
                s["entry_avg"] = entry
            if s["opened_ts"] == 0:
                s["opened_ts"] = time.time(); s["last_funding_ts"] = time.time()
        if changed:
            self._save_state()

    async def _place(self, side: str, token: str, qty: float, reduce_only: bool) -> float:
        """Send (or in dry_run, log) a futures market order. side='sell' opens/grows
        the short; side='buy' (reduceOnly) closes it. One-way mode.
        Returns the qty actually placed (may be < requested when margin caps an open)."""
        sym = self.perp_symbol.get(token)
        if not sym:
            log.warning(f"[HEDGE LIVE] {token}: NO PERP — position is UNHEDGED")
            return 0.0
        try:
            amt = float(self.futures.amount_to_precision(sym, qty))
        except Exception:
            amt = qty
        if amt <= 0:
            return 0.0
        params = {"reduceOnly": True} if reduce_only else {}
        if self.cfg.dry_run:
            log.info(f"[HEDGE DRY-RUN] would {side} {amt} {sym} reduceOnly={reduce_only} (no order sent)")
            return amt
        # Margin-aware open: cap the order to what free futures USDT can carry,
        # instead of letting the whole order bounce with "Insufficient margin"
        # every reconcile pass. Partial hedge > no hedge.
        if not reduce_only:
            mark = self._best_mark(token)
            # Pull missing margin from the venue's spot wallet first (auto-flow),
            # then cap to whatever is actually free.
            if mark > 0:
                await self._ensure_margin(sym, amt * mark / self.cfg.leverage * 1.05)
            free = -1.0
            try:
                bal = await self.futures.fetch_balance()
                free = float((bal.get("USDT") or {}).get("free") or 0)
            except Exception as e:
                log.debug(f"[HEDGE] fetch_balance for margin cap: {e}")
            if free >= 0 and mark > 0:
                max_amt = free * 0.95 * self.cfg.leverage / mark
                if amt > max_amt:
                    if max_amt * mark < self.cfg.min_hedge_qty_usd:
                        if time.time() >= getattr(self, "_margin_mute_until", 0):
                            log.error(f"[HEDGE LIVE] no margin for {sym}: need ~${amt * mark / self.cfg.leverage:.0f}, "
                                      f"free=${free:.0f} — UNDER-HEDGED (muted 10min)")
                            self._margin_mute_until = time.time() + 600
                        return 0.0
                    log.warning(f"[HEDGE LIVE] margin cap {sym}: {amt:.4f} -> {max_amt:.4f} "
                                f"(free=${free:.0f}, {self.cfg.leverage}x) — PARTIAL hedge")
                    try:
                        amt = float(self.futures.amount_to_precision(sym, max_amt))
                    except Exception:
                        amt = max_amt
                    if amt <= 0:
                        return 0.0
        # Force 1x leverage once per symbol, right before the first real order.
        # bingx requires a `side` arg on setLeverage; in one-way mode it's BOTH.
        # Without it the call errors and the symbol keeps the exchange default (e.g.
        # 20x) — dangerous: a tight isolated liq on a volatile microcap.
        if sym not in self._lev_set:
            ok = False
            for params_lev in ({"side": "BOTH"}, {}):
                try:
                    await self.futures.set_leverage(self.cfg.leverage, sym, params_lev)
                    ok = True
                    break
                except Exception as e:
                    log.debug(f"[HEDGE] set_leverage {self.cfg.leverage}x {sym} params={params_lev}: {str(e)[:70]}")
            if not ok:
                log.warning(f"[HEDGE] set_leverage {self.cfg.leverage}x {sym} FAILED — symbol keeps exchange default leverage")
            self._lev_set.add(sym)
        order = await self.futures.create_order(sym, "market", side, amt, None, params)
        log.info(f"[HEDGE LIVE] {side} {amt} {sym} reduceOnly={reduce_only} -> id={order.get('id')}")
        # Return the qty ACTUALLY placed so the caller tracks reality, not intent.
        # Prefer the venue-reported filled amount; fall back to the (margin-capped)
        # requested amt. Without this, a margin-capped short was recorded at full
        # size → "phantom hedge": book says hedged, exchange is naked.
        try:
            filled = float(order.get("filled") or 0)
        except (TypeError, ValueError):
            filled = 0.0
        return filled if filled > 0 else amt

    def _load_state(self):
        if not os.path.exists(self.cfg.state_path):
            return
        try:
            with open(self.cfg.state_path) as f:
                s = json.load(f)
            for tok, v in (s.get("shorts") or {}).items():
                self.shorts[tok] = v
            self.realized_pnl_usd = s.get("realized_pnl_usd", 0.0)
            self.funding_paid_usd = s.get("funding_paid_usd", 0.0)
        except Exception:
            pass

    def _save_state(self):
        try:
            with open(self.cfg.state_path, "w") as f:
                json.dump({
                    "shorts": {t: v for t, v in self.shorts.items() if v["qty"] > 0},
                    "realized_pnl_usd": self.realized_pnl_usd,
                    "funding_paid_usd": self.funding_paid_usd,
                }, f)
        except Exception:
            pass

    # ---------- Called by Executor after both arb legs finalize ----------
    async def adjust(self, token: str, new_spot_qty: float, mark_price: float):
        """Sync short qty to match spot long qty. Grows or reduces the hedge and,
        in live mode, places the matching futures order (1x, one-way)."""
        if not self.cfg.enabled:
            return
        if token.upper() in self.exclude:
            # Intentionally unhedged token — carry spot price risk, place no futures order.
            return
        if mark_price <= 0:
            return
        s = self.shorts[token]
        delta = new_spot_qty - s["qty"]
        if abs(delta * mark_price) < self.cfg.min_hedge_qty_usd:
            return
        now = time.time()
        if delta > 0:
            # Grow short — place the futures order FIRST; record only what ACTUALLY
            # filled (margin cap may shrink it). Recording the requested delta here is
            # exactly what created the phantom hedge: tracked short > real short.
            placed = delta
            if self.live:
                try:
                    placed = await self._place("sell", token, delta, reduce_only=False)
                except Exception as e:
                    log.error(f"[HEDGE LIVE] open short {token} +{delta:.4f} FAILED: {e} — position UNDER-HEDGED")
                    return
                placed = float(placed or 0)
                if placed <= 0:
                    log.warning(f"[HEDGE LIVE] short {token} +{delta:.4f} placed 0 (margin-capped) "
                                f"— tracked short unchanged, position UNDER-HEDGED by ~${delta * mark_price:.0f}")
                    return
            new_qty = s["qty"] + placed
            s["entry_avg"] = (s["qty"] * s["entry_avg"] + placed * mark_price) / new_qty
            s["qty"] = new_qty
            if s["opened_ts"] == 0:
                s["opened_ts"] = now
                s["last_funding_ts"] = now
            mode = "LIVE" if self.live else "PAPER"
            short_fall = max(0.0, delta - placed)
            tail = f" | UNDER-HEDGED short_fall={short_fall:.4f} (~${short_fall * mark_price:.0f})" if short_fall * mark_price >= 1 else ""
            log.info(f"[HEDGE {mode}] short {token} +{placed:.4f} @ {mark_price:.6g} | total_short={s['qty']:.4f} entry_avg={s['entry_avg']:.6g}{tail}")
        else:
            # Reduce short — close on the futures side first, then realize PnL in state
            closed_qty = -delta
            if self.live:
                try:
                    placed = await self._place("buy", token, closed_qty, reduce_only=True)
                except Exception as e:
                    log.error(f"[HEDGE LIVE] close short {token} -{closed_qty:.4f} FAILED: {e} — short still OPEN")
                    return
                placed = float(placed or 0)
                if placed > 0:
                    closed_qty = min(closed_qty, placed)
            pnl = (s["entry_avg"] - mark_price) * closed_qty
            self.realized_pnl_usd += pnl
            s["qty"] -= closed_qty
            mode = "LIVE" if self.live else "PAPER"
            log.info(f"[HEDGE {mode}] close short {token} -{closed_qty:.4f} @ {mark_price:.6g} | pnl=${pnl:+.4f} | remaining={s['qty']:.4f}")
            if s["qty"] <= 1e-9:
                s["qty"] = 0.0
                s["entry_avg"] = 0.0
                s["opened_ts"] = 0.0
        self._save_state()

    # ---------- Funding accrual (called periodically by watcher) ----------
    def accrue_funding(self):
        """Accrue funding fee on all open shorts. Paper-sim: 0.01%/8h average."""
        now = time.time()
        for token, s in self.shorts.items():
            if s["qty"] <= 0:
                continue
            hours_since = (now - s["last_funding_ts"]) / 3600
            if hours_since < 8:
                continue
            # Mark via hub
            mark = self._best_mark(token)
            if mark <= 0:
                continue
            periods = int(hours_since / 8)
            notional = s["qty"] * mark
            fee = notional * self.cfg.funding_rate_8h * periods
            self.funding_paid_usd += fee
            s["last_funding_ts"] = now
            log.debug(f"[HEDGE funding] {token} notional=${notional:.2f} fee=${fee:.4f} ({periods} periods)")
        self._save_state()

    def _best_mark(self, token: str) -> float:
        best = 0.0
        for ex_id, tickers in self.hub.tickers.items():
            t = tickers.get(f"{token}/USDT")
            if t:
                mid = (t.get("bid", 0) + t.get("ask", 0)) / 2
                if mid > best:
                    best = mid
        return best

    # ---------- Auto margin flow: spot <-> futures USDT on the hedge venue ----------
    async def _ensure_margin(self, sym: str, need_usd: float):
        """Top up futures USDT from the same venue's SPOT wallet when free margin
        can't carry a new short. Keeps HEDGE_SPOT_USDT_RESERVE on spot (that USDT
        funds arb buys / XEMM hedges)."""
        if self.cfg.dry_run or self.futures is None:
            return
        try:
            bal = await self.futures.fetch_balance()
            free = float((bal.get("USDT") or {}).get("free") or 0)
        except Exception:
            return
        if free >= need_usd:
            return
        shortfall = need_usd - free + 1.0
        spot = self.ex_by_id.get(self.cfg.futures_exchange)
        if spot is None:
            return
        try:
            sbal = await spot.fetch_balance()
            sfree = float((sbal.get("USDT") or {}).get("free") or 0)
        except Exception as e:
            log.debug(f"[HEDGE] spot balance for top-up: {e}")
            return
        reserve = float(os.getenv("HEDGE_SPOT_USDT_RESERVE", "25"))
        amt = min(shortfall, max(0.0, sfree - reserve))
        if amt < 1.0:
            if time.time() >= getattr(self, "_topup_mute_until", 0):
                log.warning(f"[HEDGE] margin top-up impossible for {sym}: spot free=${sfree:.0f} "
                            f"reserve=${reserve:.0f} shortfall=${shortfall:.0f} (muted 10min)")
                self._topup_mute_until = time.time() + 600
            return
        try:
            await spot.transfer("USDT", round(amt, 2), "spot", "swap")
            log.info(f"[HEDGE] margin top-up: ${amt:.2f} spot->swap on "
                     f"{self.cfg.futures_exchange} for {sym}")
        except Exception as e:
            log.warning(f"[HEDGE] margin top-up transfer failed: {str(e)[:120]}")

    async def sweep_excess_margin(self):
        """Return idle futures USDT back to spot so it can trade. Keeps a small
        buffer; isolated-margin locked under open shorts is untouched (not 'free')."""
        if not self.live or self.cfg.dry_run or self.futures is None:
            return
        try:
            bal = await self.futures.fetch_balance()
            free = float((bal.get("USDT") or {}).get("free") or 0)
        except Exception:
            return
        # Keep enough idle futures margin to hedge ~2 new positions. Sweeping down to
        # $15 starved the hedge so every new short got margin-capped — the chronic
        # PARTIAL-hedge / naked-inventory cause. Tunable via HEDGE_SWAP_USDT_BUFFER.
        _pos = float(os.getenv("POSITION_USD", "25"))
        keep = float(os.getenv("HEDGE_SWAP_USDT_BUFFER", str(max(40.0, 2 * _pos))))
        excess = free - keep
        if excess < 10.0:
            return
        spot = self.ex_by_id.get(self.cfg.futures_exchange)
        if spot is None:
            return
        try:
            await spot.transfer("USDT", round(excess, 2), "swap", "spot")
            log.info(f"[HEDGE] margin sweep: ${excess:.2f} swap->spot on {self.cfg.futures_exchange}")
        except Exception as e:
            log.warning(f"[HEDGE] margin sweep transfer failed: {str(e)[:120]}")

    async def ensure_free_margin(self, target_usd: float) -> float:
        """Proactively move idle spot USDT -> futures on the hedge venue so the
        rebalancer can SEED (and we can hedge) up to target_usd of new inventory.
        Without this the rebalancer caps seeds to whatever happens to be free in the
        futures wallet — which after a sweep is ~$0 — so idle spot USDT deadlocks: it
        can't seed because there's no margin, and margin only tops up lazily on a hedge
        order that the cap already blocked. Keeps HEDGE_SPOT_USDT_RESERVE on spot.
        Returns the resulting free futures USDT. Live only."""
        if not self.live or self.cfg.dry_run or self.futures is None:
            return 0.0
        try:
            fb = await self.futures.fetch_balance()
            free = float((fb.get("USDT") or {}).get("free") or 0)
        except Exception:
            return 0.0
        if free >= target_usd or target_usd <= 0:
            return free
        spot = self.ex_by_id.get(self.cfg.futures_exchange)
        if spot is None:
            return free
        try:
            sbal = await spot.fetch_balance()
            sfree = float((sbal.get("USDT") or {}).get("free") or 0)
        except Exception:
            return free
        reserve = float(os.getenv("HEDGE_SPOT_USDT_RESERVE", "25"))
        amt = min(target_usd - free, max(0.0, sfree - reserve))
        if amt < 1.0:
            return free
        try:
            await spot.transfer("USDT", round(amt, 2), "spot", "swap")
            log.info(f"[HEDGE] proactive margin top-up: ${amt:.2f} spot->swap on "
                     f"{self.cfg.futures_exchange} (target=${target_usd:.0f}, free was ${free:.0f})")
            return free + amt
        except Exception as e:
            log.warning(f"[HEDGE] proactive top-up failed: {str(e)[:100]}")
            return free

    # ---------- Mark-to-market PnL of all open shorts ----------
    def unrealized_pnl_usd(self) -> float:
        total = 0.0
        for token, s in self.shorts.items():
            if s["qty"] <= 0:
                continue
            mark = self._best_mark(token)
            if mark <= 0:
                continue
            total += (s["entry_avg"] - mark) * s["qty"]
        return total

    def status_text(self) -> str:
        n = sum(1 for s in self.shorts.values() if s["qty"] > 0)
        return (f"hedge: open={n} realized=${self.realized_pnl_usd:+.2f} "
                f"unreal=${self.unrealized_pnl_usd():+.2f} funding=${self.funding_paid_usd:.2f}")


import asyncio


async def hedge_watcher(hedge: HedgeManager, interval_sec: float = 60.0):
    """Background loop: accrue funding; sweep idle futures margin back to spot."""
    log.info(f"hedge watcher started (interval={interval_sec}s, enabled={hedge.cfg.enabled})")
    tick = 0
    while True:
        await asyncio.sleep(interval_sec)
        tick += 1
        try:
            hedge.accrue_funding()
            if tick % 5 == 0:   # every ~5 min
                await hedge.sweep_excess_margin()
        except Exception as e:
            log.exception(f"hedge watcher error: {e}")
