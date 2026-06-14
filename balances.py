"""Balance tracking — real (from exchanges) and virtual (paper sim).

RealBalanceCache: periodically fetches balance from each exchange (live mode).
VirtualPortfolio: simulates a fixed starting capital split across exchanges (paper mode).
Both expose `available(ex_id, asset) -> float` for pre-trade checks.
"""
import asyncio
import json
import os
import time
from collections import defaultdict

from appdir import BASE_DIR
from logsetup import get_logger
log = get_logger("balances")


class RealBalanceCache:
    """Live mode: cache of real exchange balances, refreshed every N sec."""

    def __init__(self, ex_by_id, refresh_sec=30, snapshot_path=str(BASE_DIR / "balances_snapshot.json"),
                 hub=None, futures_client=None, futures_ex_id=None):
        self.ex_by_id = ex_by_id
        self.refresh_sec = refresh_sec
        self.balances: dict[str, dict[str, float]] = defaultdict(dict)
        self.last_update: dict[str, float] = {}
        self.errors: dict[str, str] = {}
        self.snapshot_path = snapshot_path
        self.hub = hub                       # for USD valuation (dust filter)
        # Optional futures/swap wallet (e.g. bingx hedge margin) so the snapshot total
        # isn't undercounted by the USDT locked as hedge margin + free swap USDT.
        self.futures_client = futures_client
        self.futures_ex_id = futures_ex_id
        self.futures_usdt: float = 0.0       # free+used USDT on the swap wallet
        self.futures_update: float = 0.0

    DUST_USD = 1.0                            # hide balances worth less than this

    def _mark(self, asset: str) -> float:
        """Best mid price for asset/USDT across exchanges (1.0 USDT, 0 if unknown)."""
        if asset == "USDT":
            return 1.0
        if self.hub is None:
            return 0.0
        best = 0.0
        for _ex, tickers in self.hub.tickers.items():
            t = tickers.get(f"{asset}/USDT")
            if t:
                mid = ((t.get("bid") or 0) + (t.get("ask") or 0)) / 2
                if mid > best:
                    best = mid
        return best

    def write_snapshot(self):
        """Persist current balances to JSON so other processes (dashboard / Mini App)
        can read live balances without sharing this in-memory object. Balances worth
        less than DUST_USD are filtered out; per-exchange + total spot USD value added."""
        try:
            exchanges = {}
            spot_value_total = 0.0
            for ex_id, bals in self.balances.items():
                shown = {}
                ex_value = 0.0
                for k, v in bals.items():
                    if not v or v <= 0:
                        continue
                    val = v if k == "USDT" else v * self._mark(k)
                    ex_value += val
                    if val >= self.DUST_USD:     # drop near-zero dust
                        shown[k] = v
                spot_value_total += ex_value
                exchanges[ex_id] = {
                    "balances": shown,
                    "usdt": bals.get("USDT", 0),
                    "value_usd": round(ex_value, 2),
                    "updated_at": self.last_update.get(ex_id, 0),
                    "error": self.errors.get(ex_id),
                }
            # Futures/swap wallet (hedge margin) as its own card so the grand total
            # reflects USDT locked as hedge margin, not just spot. value == USDT here.
            futures_usdt = 0.0
            if self.futures_client is not None and self.futures_ex_id:
                futures_usdt = round(self.futures_usdt, 2)
                exchanges[f"{self.futures_ex_id}_futures"] = {
                    "balances": {"USDT": futures_usdt} if futures_usdt >= self.DUST_USD else {},
                    "usdt": futures_usdt,
                    "value_usd": futures_usdt,
                    "updated_at": self.futures_update,
                    "error": None,
                }
            usdt_total = sum(b.get("USDT", 0) for b in self.balances.values()) + futures_usdt
            data = {
                "ts": time.time(),
                "exchanges": exchanges,
                "usdt_total": usdt_total,
                "spot_value_total": round(spot_value_total + futures_usdt, 2),
            }
            with open(self.snapshot_path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            log.debug(f"balance snapshot write failed: {e}")

    async def refresh_one(self, ex_id):
        ex = self.ex_by_id.get(ex_id)
        if ex is None:
            return
        try:
            bal = await ex.fetch_balance()
            free = (bal.get("free") or {})
            self.balances[ex_id] = {k: float(v or 0) for k, v in free.items() if v}
            self.last_update[ex_id] = time.time()
            self.errors.pop(ex_id, None)
        except Exception as e:
            self.errors[ex_id] = str(e)[:80]
            log.warning(f"fetch_balance({ex_id}) failed: {type(e).__name__}: {str(e)[:200]}")

    async def refresh_futures(self):
        """Fetch the swap-wallet USDT (free+used) on the hedge venue so the snapshot
        total includes hedge margin. Read-only. No-op without a futures client."""
        if self.futures_client is None:
            return
        try:
            bal = await self.futures_client.fetch_balance()
            u = bal.get("USDT") or {}
            self.futures_usdt = float(u.get("total") or
                                      (float(u.get("free") or 0) + float(u.get("used") or 0)))
            self.futures_update = time.time()
        except Exception as e:
            log.warning(f"fetch_balance(futures {self.futures_ex_id}) failed: {str(e)[:120]}")

    async def refresh_all(self):
        await asyncio.gather(
            *(self.refresh_one(ex_id) for ex_id in self.ex_by_id),
            self.refresh_futures(),
        )
        self.write_snapshot()

    async def watch(self):
        log.info(f"RealBalanceCache watch started (refresh={self.refresh_sec}s)")
        await self.refresh_all()
        while True:
            await asyncio.sleep(self.refresh_sec)
            try:
                await self.refresh_all()
            except Exception as e:
                log.exception(f"refresh_all crashed: {e}")

    def available(self, ex_id: str, asset: str) -> float:
        return self.balances.get(ex_id, {}).get(asset.upper(), 0.0)

    def total_usdt_value(self, hub) -> float:
        """Sum of all USDT-equivalent across exchanges (USDT directly + tokens at current bid)."""
        total = 0.0
        for ex_id, bals in self.balances.items():
            for asset, qty in bals.items():
                if qty <= 0:
                    continue
                if asset == "USDT":
                    total += qty
                    continue
                # Find a USDT price for this asset (cross-exchange best bid)
                best_bid = 0.0
                for ex_check_id, syms in hub.tickers.items():
                    t = syms.get(f"{asset}/USDT")
                    if t and t.get("bid", 0) > best_bid:
                        best_bid = t["bid"]
                total += qty * best_bid
        return total

    def status_text(self) -> str:
        n_ex = len(self.balances)
        errs = sum(1 for _ in self.errors)
        usdt_total = sum(b.get("USDT", 0) for b in self.balances.values())
        return f"real_bal: {n_ex} ex, USDT={usdt_total:.0f}, err={errs}"


class VirtualPortfolio:
    """Paper mode: simulated balances starting from a fixed capital.

    Tracks USDT and base-token balances per exchange.
    Pre-trade check: can we afford position (USDT on buy_ex AND base on sell_ex)?
    """

    def __init__(self, ex_ids, total_usd=1000.0, state_path=str(BASE_DIR / "virtual_portfolio.json"),
                 token_seed_usd: float = 0.0):
        self.ex_ids = list(ex_ids)
        self.state_path = state_path
        self.start_usd = total_usd
        self.token_seed_usd = token_seed_usd   # $ of each token to seed per exchange
        # Spread USDT evenly initially
        per_ex = total_usd / len(self.ex_ids)
        self.balances: dict[str, dict[str, float]] = {ex: {"USDT": per_ex} for ex in self.ex_ids}
        self.would_block_count = 0   # trades that would have failed in real
        self.executed_count = 0
        self.fees_paid_usd = 0.0
        self._load()

    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as f:
                s = json.load(f)
            if abs(s.get("start_usd", 0) - self.start_usd) < 0.01:
                self.balances = s.get("balances", self.balances)
                self.would_block_count = s.get("would_block_count", 0)
                self.executed_count = s.get("executed_count", 0)
                self.fees_paid_usd = s.get("fees_paid_usd", 0)
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.state_path, "w") as f:
                json.dump({
                    "start_usd": self.start_usd,
                    "balances": self.balances,
                    "would_block_count": self.would_block_count,
                    "executed_count": self.executed_count,
                    "fees_paid_usd": self.fees_paid_usd,
                }, f)
        except Exception:
            pass

    def available(self, ex_id: str, asset: str) -> float:
        return self.balances.get(ex_id, {}).get(asset.upper(), 0.0)

    def can_execute(self, buy_ex, sell_ex, base, target_usd, base_amount, taker_fee=0.001) -> tuple[bool, str]:
        """Check if we have USDT on buy_ex AND base token on sell_ex.
        USDT requirement includes the taker fee so apply_trade can't go negative."""
        usdt_needed = target_usd * (1 + taker_fee)
        usdt_avail = self.available(buy_ex, "USDT")
        if usdt_avail < usdt_needed:
            return False, f"need ${usdt_needed:.0f} USDT on {buy_ex}, have ${usdt_avail:.2f}"
        base_avail = self.available(sell_ex, base)
        if base_avail < base_amount:
            return False, f"need {base_amount:.4f} {base} on {sell_ex}, have {base_avail:.4f}"
        return True, "ok"

    def apply_trade(self, buy_ex, sell_ex, base, target_usd, base_amount, buy_price, sell_price, taker_fee=0.001):
        """Apply executed paper trade to balances."""
        # buy_ex: -USDT (cost+fee), +base
        cost = buy_price * base_amount * (1 + taker_fee)
        self.balances.setdefault(buy_ex, {})
        self.balances[buy_ex]["USDT"] = self.balances[buy_ex].get("USDT", 0) - cost
        self.balances[buy_ex][base] = self.balances[buy_ex].get(base, 0) + base_amount
        # sell_ex: +USDT (proceeds-fee), -base
        proc = sell_price * base_amount * (1 - taker_fee)
        self.balances.setdefault(sell_ex, {})
        self.balances[sell_ex]["USDT"] = self.balances[sell_ex].get("USDT", 0) + proc
        self.balances[sell_ex][base] = self.balances[sell_ex].get(base, 0) - base_amount
        self.fees_paid_usd += (cost - buy_price * base_amount) + (sell_price * base_amount - proc)
        self.executed_count += 1
        self._save()

    def liquidate_token(self, token: str, qty: float, price: float, taker_fee: float = 0.001) -> float:
        """Paper: sell up to `qty` of `token` across exchanges that hold it, at `price`.
        Mirrors a guard stop-out so MTM stays aligned with realized PnL instead of
        marking a position the guard already considers closed. Returns USDT proceeds."""
        if qty <= 0 or price <= 0:
            return 0.0
        remaining = qty
        proceeds_total = 0.0
        for ex_id in self.ex_ids:
            if remaining <= 0:
                break
            have = self.balances.get(ex_id, {}).get(token, 0.0)
            if have <= 0:
                continue
            take = min(have, remaining)
            proceeds = price * take * (1 - taker_fee)
            self.balances[ex_id][token] = have - take
            self.balances[ex_id]["USDT"] = self.balances[ex_id].get("USDT", 0) + proceeds
            proceeds_total += proceeds
            remaining -= take
        if proceeds_total:
            self._save()
        return proceeds_total

    def seed_token_inventory(self, hub, universe: list[str], whitelist: set | None = None):
        """Seed each exchange with $token_seed_usd worth of each universe token at current ask.
        If whitelist provided, only those tokens are seeded.
        Adds the seed value to start_usd so MTM PnL math stays correct.
        Idempotent: only seeds tokens that aren't already seeded on that ex."""
        if self.token_seed_usd <= 0:
            return
        seeded_total_usd = 0.0
        seeded_count = 0
        for sym in universe:
            if "/" not in sym:
                continue
            base, quote = sym.split("/", 1)
            if quote != "USDT" or base == "USDT":
                continue
            if whitelist and base not in whitelist:
                continue
            for ex_id in self.ex_ids:
                tickers = hub.tickers.get(ex_id, {})
                t = tickers.get(sym)
                if not t or t.get("ask", 0) <= 0:
                    continue
                if self.balances.get(ex_id, {}).get(base, 0) > 0:
                    continue   # already seeded
                ask = t["ask"]
                qty = self.token_seed_usd / ask
                self.balances.setdefault(ex_id, {})
                self.balances[ex_id][base] = qty
                seeded_total_usd += self.token_seed_usd
                seeded_count += 1
        # Adjust start_usd so initial MTM == start_usd (no fake "profit" from seeding)
        self.start_usd += seeded_total_usd
        log.info(f"VirtualPortfolio: seeded {seeded_count} (token,ex) positions, "
                 f"+${seeded_total_usd:.2f} added to start_usd "
                 f"(new start=${self.start_usd:.2f})")
        self._save()

    def total_value_usd(self, hub) -> float:
        """Mark-to-market total portfolio value at current prices."""
        total = 0.0
        for ex_id, bals in self.balances.items():
            for asset, qty in bals.items():
                if qty == 0:
                    continue
                if asset == "USDT":
                    total += qty
                    continue
                # current best bid across exchanges
                best = 0.0
                for syms in hub.tickers.values():
                    t = syms.get(f"{asset}/USDT")
                    if t and t.get("bid", 0) > best:
                        best = t["bid"]
                total += qty * best
        return total

    def status_text(self, hub) -> str:
        mtm = self.total_value_usd(hub)
        pnl = mtm - self.start_usd
        block_pct = self.would_block_count / max(1, self.would_block_count + self.executed_count) * 100
        return (
            f"vport: MTM=${mtm:.2f} pnl=${pnl:+.2f} "
            f"executed={self.executed_count} blocked={self.would_block_count} ({block_pct:.0f}%)"
        )
