"""Inventory guard with weighted-average cost tracking and stop-loss.

Tracks position cost basis per (exchange_id, base_token).
Background watcher checks current bid vs avg_cost — triggers stop-loss when drop > threshold.
After stop, token+exchange goes to cooldown blacklist (no rebuy for N minutes).
"""
import asyncio
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass

from appdir import BASE_DIR
from logsetup import get_logger
log = get_logger("guard")

try:
    import db as _db
except Exception:
    _db = None


STABLES = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE", "USDP", "BUSD"}


@dataclass
class GuardConfig:
    default_threshold_pct: float = 1.5     # default stop trigger %
    check_interval_sec: float = 5.0
    cooldown_after_stop_sec: int = 1800    # 30 min — no rebuy after stop
    position_timeout_sec: int = 300        # force-close residual drift after 5min if in loss
    state_path: str = str(BASE_DIR / "inventory_state.json")
    journal_path: str = str(BASE_DIR / "stops.jsonl")
    # per-token overrides (volatility-aware)
    threshold_overrides: dict = None

    def __post_init__(self):
        if self.threshold_overrides is None:
            self.threshold_overrides = {
                # majors — tighter stop
                "BTC": 0.7, "ETH": 0.7, "BNB": 0.8, "SOL": 1.0,
                # known volatile alts in our universe — looser stop
                "WARD": 3.0, "CHIP": 3.0, "OMI": 3.0, "POLS": 2.5,
                "SAGA": 2.0, "ULTIMA": 3.0, "SIREN": 3.0, "OPG": 2.5,
                "LAB": 3.0, "KAIO": 3.0, "CHECK": 3.0, "XDC": 1.5,
            }


class InventoryGuard:
    def __init__(self, hub, ex_by_id, config: GuardConfig, executor=None, hedge=None):
        self.hub = hub
        self.ex_by_id = ex_by_id
        self.cfg = config
        self.executor = executor   # back-ref for paper mode (apply virtual loss to PnL)
        self.hedge = hedge         # HedgeManager for delta-neutral short hedge
        # token -> {"qty", "avg_cost"}  — GLOBAL net inventory across all exchanges
        # (pre-funded model: arbitrage buy+sell net out; only residual drift tracked)
        self.positions = defaultdict(lambda: {"qty": 0.0, "avg_cost": 0.0})
        self.position_opened_ts: dict = {}   # token -> opened_ts
        self.position_peak: dict = {}        # token -> peak_price
        # cooldowns stay per (ex_id, token) because blacklist is trade-side-specific
        self.cooldowns: dict = {}
        self.stops_triggered = 0
        self.stop_loss_total_usd = 0.0
        self._load_state()

    # ---------- state ----------
    def _load_state(self):
        if not os.path.exists(self.cfg.state_path):
            return
        try:
            with open(self.cfg.state_path) as f:
                s = json.load(f)
            for token, v in (s.get("positions") or {}).items():
                self.positions[token] = v
            for k, v in (s.get("cooldowns") or {}).items():
                ex_id, token = k.split("|")
                if v > time.time():
                    self.cooldowns[(ex_id, token)] = v
            self.stops_triggered = s.get("stops_triggered", 0)
            self.stop_loss_total_usd = s.get("stop_loss_total_usd", 0.0)
        except Exception:
            pass

    def _save_state(self):
        try:
            s = {
                "positions": {t: v for t, v in self.positions.items() if v["qty"] > 0},
                "cooldowns": {f"{e}|{t}": v for (e, t), v in self.cooldowns.items() if v > time.time()},
                "stops_triggered": self.stops_triggered,
                "stop_loss_total_usd": self.stop_loss_total_usd,
            }
            with open(self.cfg.state_path, "w") as f:
                json.dump(s, f)
        except Exception:
            pass

    def _journal_stop(self, ex_id, token, qty, sell_price, avg_cost, loss_usd):
        rec = {
            "ts": time.time(), "ex": ex_id, "token": token,
            "qty": qty, "sell_price": sell_price, "avg_cost": avg_cost,
            "loss_usd": loss_usd, "drop_pct": (sell_price/avg_cost - 1) * 100 if avg_cost else 0,
        }
        try:
            with open(self.cfg.journal_path, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except Exception as e:
            log.warning(f"stops jsonl write failed: {e}")
        if _db is not None:
            _db.insert_stop(rec)

    # ---------- API for executor ----------
    def on_fill(self, ex_id: str, token: str, side: str, qty: float, price: float):
        """Update GLOBAL net inventory after a fill (ex_id ignored — pre-funded model).
        Atomic arbitrage buy+sell of equal qty leaves net=0; only drift accumulates."""
        if token in STABLES or qty <= 0 or price <= 0:
            return
        pos = self.positions[token]
        if side == "buy":
            new_qty = pos["qty"] + qty
            if new_qty > 0:
                pos["avg_cost"] = (pos["qty"] * pos["avg_cost"] + qty * price) / new_qty
            pos["qty"] = new_qty
            if token not in self.position_opened_ts:
                self.position_opened_ts[token] = time.time()
                self.position_peak[token] = price
        else:  # sell
            pos["qty"] = max(0, pos["qty"] - qty)
            if pos["qty"] == 0:
                pos["avg_cost"] = 0.0
                self.position_opened_ts.pop(token, None)
                self.position_peak.pop(token, None)
        # NOTE: hedge sync is intentionally NOT called here — for atomic arb pairs,
        # buy and sell on_fill calls would oscillate the hedge open→close with zero net.
        # Executor calls hedge.adjust() once AFTER both legs finalize, using net qty.
        self._save_state()

    def is_blacklisted(self, ex_id: str, token: str) -> bool:
        until = self.cooldowns.get((ex_id, token), 0)
        if until <= 0:
            return False
        if time.time() >= until:
            self.cooldowns.pop((ex_id, token), None)
            return False
        return True

    def status_text(self) -> str:
        active_pos = sum(1 for p in self.positions.values() if p["qty"] > 0)
        active_bl = sum(1 for v in self.cooldowns.values() if v > time.time())
        return (
            f"inv: pos={active_pos} bl={active_bl} stops={self.stops_triggered} "
            f"sl_loss=${self.stop_loss_total_usd:.2f}"
        )

    # ---------- watcher ----------
    async def watch(self):
        log.info(f"InventoryGuard watch started (interval={self.cfg.check_interval_sec}s, default_threshold={self.cfg.default_threshold_pct}%)")
        while True:
            await asyncio.sleep(self.cfg.check_interval_sec)
            try:
                await self._scan_once()
            except Exception as e:
                log.exception(f"guard scan crashed: {e}")

    async def _scan_once(self):
        now = time.time()
        for token, pos in list(self.positions.items()):
            if pos["qty"] <= 0 or pos["avg_cost"] <= 0:
                continue
            if token in STABLES:
                continue
            # Find best (highest) bid across all exchanges for liquidation reference
            best_bid = 0.0
            best_ex = None
            for ex_id, tickers in self.hub.tickers.items():
                t = tickers.get(f"{token}/USDT")
                if t and t.get("bid", 0) > best_bid:
                    best_bid = t["bid"]
                    best_ex = ex_id
            if best_bid <= 0 or best_ex is None:
                continue
            bid = best_bid
            ex_id = best_ex
            avg_cost = pos["avg_cost"]

            if bid > self.position_peak.get(token, avg_cost):
                self.position_peak[token] = bid
            peak = self.position_peak.get(token, avg_cost)

            if peak > avg_cost and bid <= avg_cost:
                log.warning(f"TRAILING close: {token} peaked={peak:.6g} returned to avg={avg_cost:.6g} bid={bid:.6g}@{ex_id}")
                await self._stop_out(ex_id, token, pos["qty"], bid, avg_cost, reason="trailing")
                continue

            opened_ts = self.position_opened_ts.get(token, now)
            age_sec = now - opened_ts
            if self.cfg.position_timeout_sec > 0 and age_sec >= self.cfg.position_timeout_sec:
                if bid > avg_cost:
                    log.debug(f"TIMEOUT skipped: {token} in profit bid={bid:.6g} > avg={avg_cost:.6g}")
                else:
                    log.warning(f"TIMEOUT force-close: {token} held {age_sec:.0f}s bid={bid:.6g}@{ex_id} avg={avg_cost:.6g} loss=${((bid - avg_cost) * pos['qty']):.4f}")
                    await self._stop_out(ex_id, token, pos["qty"], bid, avg_cost, reason="timeout")
                    continue

            threshold_pct = self.cfg.threshold_overrides.get(token, self.cfg.default_threshold_pct)
            trigger = avg_cost * (1 - threshold_pct / 100)
            if bid <= trigger:
                await self._stop_out(ex_id, token, pos["qty"], bid, avg_cost)

    async def _stop_out(self, ex_id, token, qty, sell_price, avg_cost, reason="stop"):
        # Live mode: real market sell. Paper: simulated.
        loss_usd = (sell_price - avg_cost) * qty
        is_live = self.executor and getattr(self.executor.cfg, "mode", None) and \
                  self.executor.cfg.mode.value == "live"

        if is_live:
            # Inventory is tracked as a GLOBAL net qty, but a market sell must hit an
            # exchange that actually holds the token. Prefer the exchange with the
            # largest real balance of `token`, and clamp the sell to what's there.
            bc = getattr(self.executor, "balance_cache", None)
            if bc is not None:
                holdings = [(eid, bc.available(eid, token)) for eid in self.ex_by_id]
                best = max(holdings, key=lambda x: x[1], default=(ex_id, 0.0))
                if best[1] > 0:
                    ex_id = best[0]
                    qty = min(qty, best[1])
            ex = self.ex_by_id.get(ex_id)
            if ex is None:
                log.error(f"stop-out: exchange {ex_id} missing")
                return
            if qty <= 0:
                log.error(f"stop-out: no real {token} balance to sell — skipping market order")
                return
            try:
                log.warning(f"LIVE {reason.upper()}: {token}@{ex_id} qty={qty:.4f} bid={sell_price} avg_cost={avg_cost} loss=${loss_usd:.2f}")
                await asyncio.wait_for(
                    ex.create_market_sell_order(f"{token}/USDT", qty),
                    timeout=10.0,
                )
                log.info(f"LIVE {reason.upper()} executed: {token}@{ex_id}")
            except Exception as e:
                log.exception(f"LIVE {reason.upper()} FAILED for {token}@{ex_id}: {e}")
                self._journal_stop(ex_id, token, qty, sell_price, avg_cost, 0)
                self.cooldowns[(ex_id, token)] = time.time() + 300
                return
        else:
            log.info(f"PAPER {reason}: {token}@{ex_id} qty={qty:.4f} bid={sell_price} avg_cost={avg_cost} loss=${loss_usd:.4f}")
            # Mirror the close in the virtual portfolio so MTM and daily PnL agree.
            vp = getattr(self.executor, "virtual_portfolio", None) if self.executor else None
            if vp is not None:
                try:
                    vp.liquidate_token(token, qty, sell_price)
                except Exception as e:
                    log.debug(f"vport liquidate_token failed: {e}")

        # Apply loss (real or simulated) to executor's daily PnL
        if self.executor is not None:
            self.executor.daily_pnl_usd += loss_usd
            self.executor.consecutive_losses += 1
            self.executor.total_trades += 1  # count stop as a "trade event"

        self.stops_triggered += 1
        self.stop_loss_total_usd += loss_usd
        self._journal_stop(ex_id, token, qty, sell_price, avg_cost, loss_usd)
        # Reset GLOBAL position, blacklist on the trade-side exchange
        self.positions[token] = {"qty": 0.0, "avg_cost": 0.0}
        self.position_opened_ts.pop(token, None)
        self.position_peak.pop(token, None)
        self.cooldowns[(ex_id, token)] = time.time() + self.cfg.cooldown_after_stop_sec
        self._save_state()
