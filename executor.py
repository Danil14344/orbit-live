"""Arbitrage executor with PAPER and LIVE modes.

PAPER: simulate execution using order-book VWAP as fill price.
LIVE:  place real market orders on both exchanges in parallel.

Hard risk-limits enforced before every execution. State persisted to disk.
"""
import asyncio
import json
import os
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from enum import Enum

from appdir import BASE_DIR
from logsetup import get_logger
log = get_logger("executor")

try:
    import db as _db
    _db.init_db()
except Exception:
    _db = None
from typing import Optional


# Tokens banned from trading — consistently trigger stop-losses due to thin liquidity
# Hard ban list — tokens that look like arb but aren't tradeable end-to-end
# (suspended deposits/withdrawals, delistings). Env: BANNED_TOKENS=BTW,XXX
BANNED_TOKENS = {
    t.strip().upper() for t in os.getenv("BANNED_TOKENS", "").split(",") if t.strip()
}
# (exchange_id, symbol) pairs the venue refuses to trade via API (learned at runtime
# from order errors like bingx 100421). Reset on restart — cheap to re-learn.
API_BANNED_PAIRS: set[tuple] = set()

# Liquid majors: their true cross-exchange spread is <0.05%. Any "window" above a
# few bps is a stale/lagging feed, not arb — buying never crosses, the sell leg
# one-legs, and we eat fees + dust (BTC: 3 phantom 0.4-1.4% windows, all one-legged,
# left ~$9 dust on 2026-06-11). Hard-skip them regardless of apparent spread.
MAJOR_TOKENS = {
    t.strip().upper() for t in os.getenv(
        "MAJOR_TOKENS", "BTC,ETH,BNB,SOL,XRP,ADA,DOGE,TRX,LTC,BCH,DOT,AVAX,LINK,USDC"
    ).split(",") if t.strip()
}

# Phantom-window circuit breaker: tokens whose "spread" is a stale-feed artifact
# (the sell leg misses by a wide gap every time). Learned at runtime from leg-miss.
PHANTOM_GAP_PCT = float(os.getenv("PHANTOM_GAP_PCT", "0.8"))     # gap that marks a strike
PHANTOM_MAX_STRIKES = int(os.getenv("PHANTOM_MAX_STRIKES", "2"))  # strikes before soft-ban
PHANTOM_BAN_SEC = float(os.getenv("PHANTOM_BAN_SEC", "3600"))     # soft-ban duration
PHANTOM_STRIKES: dict[str, int] = {}
PHANTOM_BANNED_UNTIL: dict[str, float] = {}

# Whitelist mode — if non-empty, ONLY these tokens are traded. Others are logged
# to shadow_opps.jsonl as "would have traded" for later analysis (swap candidates).
# Loaded from WHITELIST env var (comma-separated bases, e.g. "OPG,ULTIMA,BSB").
WHITELIST_TOKENS: set[str] = {
    t.strip().upper() for t in os.getenv("WHITELIST", "").split(",") if t.strip()
}
SHADOW_LOG_PATH = "shadow_opps.jsonl"

# ====== SUBSCRIPTION TIER MOCK ======
# Simulate what each subscription tier gives. Set via TIER env var (default 3).
# Tier 1 = Scanner only: detects spreads, logs them, NEVER trades
# Tier 2 = Lite: trades only "majors" (BTC/ETH/SOL/BNB/XRP), capped $50/trade
# Tier 3 = Pro: full bot (whitelist + hedge + all features) — current behavior
SUBSCRIPTION_TIER = int(os.getenv("TIER", "3"))
TIER2_MAJORS = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT"}
TIER2_MAX_POSITION_USD = 50.0
TIER_SCANNER_LOG = "tier_scanner_opps.jsonl"
TIER1_SHADOW_LOG = "tier1_shadow.jsonl"
TIER2_SHADOW_LOG = "tier2_shadow.jsonl"


def _shadow_log(path: str, opp, would_pos_usd: float):
    """Append a tier-1/tier-2 'would-have-seen' record."""
    try:
        import json as _j
        rec = {
            "ts": time.time(),
            "symbol": opp["symbol"],
            "buy_ex": opp["buy_ex"], "sell_ex": opp["sell_ex"],
            "net_pct": opp.get("real_net_pct", 0),
            "buy_ask": opp.get("buy_ask", 0),
            "sell_bid": opp.get("sell_bid", 0),
            "depth_usd": opp.get("max_usd_achievable", 0),
            "would_pnl_usd": would_pos_usd * opp.get("real_net_pct", 0) / 100,
            "position_usd": would_pos_usd,
        }
        with open(path, "a") as f:
            f.write(_j.dumps(rec) + "\n")
    except Exception:
        pass


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


@dataclass
class ExecConfig:
    mode: Mode = Mode.PAPER
    position_size_usd: float = 30.0
    min_position_usd: float = 30.0        # adaptive sizing floor
    max_position_usd: float = 30.0        # adaptive sizing ceiling (fixed $30/trade)
    adaptive_size_pct_of_depth: float = 0.7  # use 70% of available depth, capped
    min_real_net_pct: float = 0.30
    require_depth_full: bool = True
    max_concurrent: int = 3
    cooldown_after_win_sec: int = 30
    cooldown_after_loss_sec: int = 300
    daily_loss_limit_usd: float = -30.0
    consecutive_losses_trigger: int = 2
    pause_on_loss_streak_sec: int = 3600
    order_timeout_sec: float = 5.0
    journal_path: str = str(BASE_DIR / "trades.jsonl")
    state_path: str = str(BASE_DIR / "executor_state.json")
    # Capital management
    total_capital_usd: float = 1000.0
    reserve_pct: float = 0.20             # never bind more than (1-reserve)% of capital
    max_position_per_token_usd: float = 100.0  # combined inventory cap per token
    # Bidirectional filter
    require_bidirectional: bool = False
    # Daily portfolio drawdown guard (paper: virtual MTM; live: real balances MTM)
    daily_drawdown_pct_stop: float = 10.0  # stop trading if portfolio -10% from start
    # Pre-execution sanity check (live only): verify spread still exists via fresh REST
    pre_exec_sanity_check: bool = True   # re-validate spread on fresh book + reprice IOC before firing
    pre_exec_max_decay_pct: float = 0.20
    # If a ws-sourced pre-exec quote implies a spread this wide, REST-verify it
    # before trusting (a stale ws feed fakes huge spreads → phantom one-legs).
    ws_verify_spread_pct: float = 0.8
    # Inventory skewing: tighten threshold when inventory is full, loosen when empty
    inventory_skew_enabled: bool = True
    skew_max_tighten_pct: float = 0.4      # add up to this much to base threshold
    skew_max_loosen_pct: float = 0.15      # subtract up to this from base for unwinding trades
    # Volatility-aware threshold (extra cushion when prices swinging)
    volatility_skew_enabled: bool = True
    volatility_factor: float = 0.15        # extra threshold = symbol_vol_pct * this
    # IOC limit orders for live (safer than market)
    use_ioc_orders: bool = True
    ioc_price_buffer_pct: float = 0.20     # buy at ask × (1+buffer), sell at bid × (1-buffer); covers micro-jitter so legs cross
    # Adaptive IOC buffer: buf = clamp(net% * frac, min, max). Scales aggression
    # with the spread so fat-spread legs cross through book jitter (fixing the
    # one-legged mexc sell misses) while thin opps stay tight (buffer never eats edge).
    ioc_buffer_net_frac: float = 0.25
    ioc_buffer_min_pct: float = 0.15
    ioc_buffer_max_pct: float = 0.60
    # Cooldown legacy alias (kept for compatibility)
    cooldown_per_pair_sec: int = 60


@dataclass
class TradeRecord:
    id: str
    ts: float
    mode: str
    symbol: str
    buy_ex: str
    sell_ex: str
    target_usd: float
    expected_net_pct: float
    # Fill data
    buy_fill_price: Optional[float] = None
    sell_fill_price: Optional[float] = None
    base_filled: float = 0.0
    actual_pnl_usd: Optional[float] = None
    actual_net_pct: Optional[float] = None
    status: str = "pending"
    error: str = ""
    # Latency tracking (ms) — opp_seen → exec_start → exec_end
    opp_age_ms: Optional[float] = None    # how stale the opp was when we acted
    exec_latency_ms: Optional[float] = None  # actual execution duration
    # Pre-exec quote source per leg ("ws" | "rest" | None) — leg-risk telemetry
    pre_exec_buy_src: Optional[str] = None
    pre_exec_sell_src: Optional[str] = None


class Executor:
    def __init__(self, ex_by_id: dict, config: ExecConfig):
        self.ex_by_id = ex_by_id
        self.cfg = config
        self.active: set[str] = set()
        self.active_position_usd: dict[str, float] = {}  # sym -> bound $ while in-flight
        self.cooldowns: dict[str, float] = {}
        self.daily_pnl_usd: float = 0.0
        self.daily_started: float = time.time()
        self.consecutive_losses: int = 0
        self.paused_until: float = 0.0
        self.total_trades: int = 0
        self.total_wins: int = 0
        self.rejects: dict[str, int] = defaultdict(int)
        self.considered: int = 0
        # ---- leg-risk telemetry ----
        self.outcomes: dict[str, int] = defaultdict(int)   # rec.status -> count
        self.leg_miss: dict[str, int] = defaultdict(int)   # leg-miss verdict -> count
        self.slip_sum: float = 0.0    # sum(expected_net_pct - actual_net_pct) over filled trades
        self.slip_n: int = 0          # count of filled trades measured
        self.preexec_ws_hits: int = 0   # pre-exec legs served from a fresh ws quote
        self.preexec_legs: int = 0      # total pre-exec leg reads (ws + rest)
        # ---- liveness timestamps for the health monitor ----
        self.last_opp_ts: float = time.time()    # last time the scanner saw any window
        self.last_fill_ts: float = time.time()   # last successful (status=ok) fill
        self.guard = None              # InventoryGuard
        self.bidir = None              # BidirectionalTracker
        self.balance_cache = None      # RealBalanceCache (live)
        self.hedge = None              # HedgeManager
        self.virtual_portfolio = None  # VirtualPortfolio (paper)
        self.notifier = None           # TelegramBot (optional) — open/close notifications
        self._load_state()

    # ---------- state persistence ----------
    def _load_state(self):
        if not os.path.exists(self.cfg.state_path):
            return
        try:
            with open(self.cfg.state_path) as f:
                s = json.load(f)
            # only carry over today
            if time.time() - s.get("daily_started", 0) < 86400:
                self.daily_pnl_usd = s.get("daily_pnl_usd", 0.0)
                self.daily_started = s.get("daily_started", time.time())
                self.consecutive_losses = s.get("consecutive_losses", 0)
            self.total_trades = s.get("total_trades", 0)
            self.total_wins = s.get("total_wins", 0)
            self.outcomes = defaultdict(int, s.get("outcomes", {}))
            self.slip_sum = s.get("slip_sum", 0.0)
            self.slip_n = s.get("slip_n", 0)
            self.preexec_ws_hits = s.get("preexec_ws_hits", 0)
            self.preexec_legs = s.get("preexec_legs", 0)
        except Exception:
            pass

    def _save_state(self):
        s = {
            "daily_pnl_usd": self.daily_pnl_usd,
            "daily_started": self.daily_started,
            "consecutive_losses": self.consecutive_losses,
            "total_trades": self.total_trades,
            "total_wins": self.total_wins,
            "outcomes": dict(self.outcomes),
            "slip_sum": self.slip_sum,
            "slip_n": self.slip_n,
            "preexec_ws_hits": self.preexec_ws_hits,
            "preexec_legs": self.preexec_legs,
        }
        try:
            with open(self.cfg.state_path, "w") as f:
                json.dump(s, f)
        except Exception:
            pass

    def leg_risk_summary(self) -> str:
        """Compact leg-risk telemetry line for periodic logging."""
        tot = sum(self.outcomes.values())
        def pct(n):
            return f"{n/tot*100:.0f}%" if tot else "0%"
        ok = self.outcomes.get("ok", 0)
        decay = self.outcomes.get("aborted_spread_decayed", 0)
        sanity = self.outcomes.get("aborted_sanity_check", 0)
        # one-legged / imbalance-related outcomes
        one_leg = sum(v for k, v in self.outcomes.items()
                      if k in ("kept_buy_excess", "rebalanced_sell_excess",
                               "imbalanced_unwind_failed", "buy_only_unfilled",
                               "sell_only_unfilled", "hedged_sell_failed",
                               "hedged_buy_failed", "hedge_failed", "both_failed",
                               "partial"))
        avg_slip = (self.slip_sum / self.slip_n) if self.slip_n else 0.0
        ws_rate = (self.preexec_ws_hits / self.preexec_legs * 100) if self.preexec_legs else 0.0
        others = {k: v for k, v in self.outcomes.items()
                  if k not in ("ok", "aborted_spread_decayed", "aborted_sanity_check")}
        others_top = ", ".join(f"{k}:{v}" for k, v in sorted(others.items(), key=lambda x: -x[1])[:4])
        rej_top = ", ".join(f"{k}:{v}" for k, v in sorted(self.rejects.items(), key=lambda x: -x[1])[:5])
        miss_top = ", ".join(f"{k}:{v}" for k, v in sorted(self.leg_miss.items(), key=lambda x: -x[1]))
        return (
            f"[LEG-RISK] n={tot} ok={ok}({pct(ok)}) decay={decay}({pct(decay)}) "
            f"sanity_abort={sanity}({pct(sanity)}) one_legged={one_leg}({pct(one_leg)}) "
            f"avg_slip={avg_slip:+.3f}% ws_preexec={ws_rate:.0f}% ({self.preexec_ws_hits}/{self.preexec_legs})"
            + (f" | {others_top}" if others_top else "")
            + (f" | leg_miss[{miss_top}]" if miss_top else "")
            + f" || considered={self.considered} rej:[{rej_top}]"
        )

    def _journal(self, rec: TradeRecord):
        d = asdict(rec)
        try:
            with open(self.cfg.journal_path, "a") as f:
                f.write(json.dumps(d) + "\n")
        except Exception as e:
            log.warning(f"jsonl write failed: {e}")
        if _db is not None:
            _db.insert_trade(d)

    def _reset_daily_if_needed(self):
        if time.time() - self.daily_started >= 86400:
            self.daily_pnl_usd = 0.0
            self.daily_started = time.time()
            self.consecutive_losses = 0
            self.paused_until = 0.0

    # ---------- gating ----------
    def status_text(self) -> str:
        rej_top = ", ".join(f"{k}:{v}" for k, v in sorted(self.rejects.items(), key=lambda x: -x[1])[:3])
        return (
            f"mode={self.cfg.mode.value} "
            f"considered={self.considered} trades={self.total_trades} wins={self.total_wins} "
            f"pnl=${self.daily_pnl_usd:.2f} "
            f"streak={self.consecutive_losses} active={len(self.active)} "
            + (f"PAUSED({int(self.paused_until - time.time())}s) " if self.paused_until > time.time() else "")
            + (f"rej:[{rej_top}]" if rej_top else "")
        )

    def position_size_for(self, opp) -> float:
        depth_usd = opp.get("max_usd_achievable", 0)
        proposed = depth_usd * self.cfg.adaptive_size_pct_of_depth
        return max(self.cfg.min_position_usd, min(self.cfg.max_position_usd, proposed))

    def skewed_threshold(self, opp, base_threshold: float) -> float:
        """Combined adjustment: volatility cushion + inventory skewing."""
        threshold = base_threshold
        # Volatility skew: in volatile markets need bigger cushion
        if self.cfg.volatility_skew_enabled:
            hub = opp.get("__hub")
            if hub is not None:
                vol = hub.volatility_pct.get(opp["symbol"], 0)
                threshold += vol * self.cfg.volatility_factor

        if not self.cfg.inventory_skew_enabled or self.guard is None:
            return threshold
        token = opp["symbol"].split("/")[0]
        # Global net inventory $ for this token (pre-funded model)
        p = self.guard.positions.get(token)
        total_inv_usd = (p["qty"] * p["avg_cost"]) if p and p["qty"] > 0 else 0.0
        cap = self.cfg.max_position_per_token_usd
        if cap <= 0:
            return threshold
        pressure = min(1.0, total_inv_usd / cap)
        # Net long inventory → new buy adds pressure → tighten
        if pressure > 0:
            tighten = self.cfg.skew_max_tighten_pct * pressure
            return threshold + tighten
        return threshold

    def min_cost_for(self, ex_id, symbol) -> float:
        """Per-exchange minimum order size in USDT, from cached markets."""
        ex = self.ex_by_id.get(ex_id)
        if ex is None:
            return 0
        m = (ex.markets or {}).get(symbol, {}) or {}
        cost_min = ((m.get("limits") or {}).get("cost") or {}).get("min") or 0
        return float(cost_min or 0)

    def allowed(self, opp) -> tuple[bool, str, float]:
        # ====== TIER GATING ======
        sym = opp["symbol"]
        base = sym.split("/")[0]

        # ---- SHADOW logging: always-on, regardless of active tier ----
        # Tier 1: log every opportunity that passed scanner filters (any token)
        _shadow_log(TIER1_SHADOW_LOG, opp, would_pos_usd=30.0)
        # Tier 2: log only opps on major tokens (what a Lite user would have seen)
        if base in TIER2_MAJORS:
            _shadow_log(TIER2_SHADOW_LOG, opp, would_pos_usd=50.0)

        if SUBSCRIPTION_TIER == 1:
            # Scanner only — log opp, never trade
            try:
                import json as _j
                rec = {"ts": time.time(), "tier": 1, "symbol": sym,
                       "buy_ex": opp["buy_ex"], "sell_ex": opp["sell_ex"],
                       "net_pct": opp.get("real_net_pct", 0),
                       "would_pnl": 30 * opp.get("real_net_pct", 0) / 100}
                with open(TIER_SCANNER_LOG, "a") as f:
                    f.write(_j.dumps(rec) + "\n")
            except Exception:
                pass
            return False, "tier 1: scanner-only mode", 0
        if SUBSCRIPTION_TIER == 2:
            if base not in TIER2_MAJORS:
                return False, f"tier 2: {base} not in majors list", 0

        self._reset_daily_if_needed()
        now = time.time()
        if now < self.paused_until:
            return False, f"paused {int(self.paused_until - now)}s", 0
        if self.daily_pnl_usd <= self.cfg.daily_loss_limit_usd:
            return False, "daily loss limit hit", 0
        # MTM drawdown check
        if self.virtual_portfolio is not None and opp.get("__hub") is not None:
            mtm = self.virtual_portfolio.total_value_usd(opp["__hub"])
            dd = (1 - mtm / self.virtual_portfolio.start_usd) * 100
            if dd >= self.cfg.daily_drawdown_pct_stop:
                self.paused_until = now + 3 * 3600
                log.error(f"MTM DRAWDOWN STOP: portfolio=${mtm:.2f} start=${self.virtual_portfolio.start_usd} dd={dd:.2f}% >= {self.cfg.daily_drawdown_pct_stop}% — PAUSED 3h")
                return False, f"MTM drawdown {dd:.1f}% >= {self.cfg.daily_drawdown_pct_stop:.0f}% — PAUSED 3h", 0
        if len(self.active) >= self.cfg.max_concurrent:
            return False, "max concurrent reached", 0
        sym = opp["symbol"]
        if sym in self.active:
            return False, "pair already trading", 0
        cd_until = self.cooldowns.get(sym, 0)
        if now < cd_until:
            return False, f"cooldown {int(cd_until - now)}s", 0
        # Momentum filter disabled — global inventory + hedge removes the need to dodge falling prices

        # Inventory-aware dynamic threshold
        eff_threshold = self.skewed_threshold(opp, self.cfg.min_real_net_pct)
        if opp.get("real_net_pct", -1) < eff_threshold:
            return False, f"below skewed net% ({eff_threshold:.2f}%)", 0
        if self.cfg.require_depth_full and not opp.get("depth_full"):
            return False, "depth not full", 0
        # Per-exchange min order size — buy side
        position = self.position_size_for(opp)
        if SUBSCRIPTION_TIER == 2 and position > TIER2_MAX_POSITION_USD:
            position = TIER2_MAX_POSITION_USD
        min_cost_buy = self.min_cost_for(opp["buy_ex"], opp["symbol"])
        min_cost_sell = self.min_cost_for(opp["sell_ex"], opp["symbol"])
        min_required = max(min_cost_buy, min_cost_sell)
        if min_required > 0 and position < min_required * 1.05:
            return False, f"below ex min order ${min_required:.2f}", 0
        depth_usd = opp.get("max_usd_achievable", 0)
        if depth_usd < self.cfg.min_position_usd:
            return False, "depth < min position", 0

        base = sym.split("/")[0]

        if base in BANNED_TOKENS:
            return False, f"banned token ({base})", 0

        if base in MAJOR_TOKENS:
            return False, f"major (phantom spread) ({base})", 0

        _pban = PHANTOM_BANNED_UNTIL.get(base, 0)
        if now < _pban:
            return False, f"phantom soft-ban ({base}, {int(_pban - now)}s)", 0

        if (opp["buy_ex"], sym) in API_BANNED_PAIRS or (opp["sell_ex"], sym) in API_BANNED_PAIRS:
            return False, f"api-banned pair ({sym})", 0

        if WHITELIST_TOKENS and base not in WHITELIST_TOKENS:
            # The whitelist is a FUNDING policy (what the rebalancer seeds), not a
            # trade gate. If we already hold enough of the token on the sell side
            # (residuals, dropped-from-whitelist inventory), trade it anyway —
            # overnight 2026-06-11 the bot held $25 VELVET and rejected 5251
            # above-threshold VELVET opps purely on whitelist membership.
            inventory_backed = False
            if self.cfg.mode == Mode.LIVE and self.balance_cache is not None:
                have = self.balance_cache.available(opp["sell_ex"], base) or 0
                need = position / opp["vwap_ask"] if opp.get("vwap_ask") else 0
                inventory_backed = need > 0 and have >= need
            if not inventory_backed:
                # Shadow-log this would-have-traded opp for later analysis
                try:
                    import json as _json
                    rec = {
                        "ts": time.time(), "token": base, "symbol": sym,
                        "buy_ex": opp["buy_ex"], "sell_ex": opp["sell_ex"],
                        "real_net_pct": opp.get("real_net_pct", 0),
                        "would_pnl_usd": position * opp.get("real_net_pct", 0) / 100,
                        "depth_usd": opp.get("max_usd_achievable", 0),
                    }
                    with open(SHADOW_LOG_PATH, "a") as _f:
                        _f.write(_json.dumps(rec) + "\n")
                except Exception:
                    pass
                return False, f"not in whitelist ({base})", 0

        if self.guard is not None and self.guard.is_blacklisted(opp["buy_ex"], base):
            return False, "token blacklisted (recent stop-loss)", 0

        if self.cfg.require_bidirectional and self.bidir is not None:
            if not self.bidir.is_bidirectional(base, opp["buy_ex"], opp["sell_ex"]):
                return False, "not bidirectional yet", 0

        # Per-token aggregate inventory cap
        if self.guard is not None:
            p_inv = self.guard.positions.get(base)
            inv_usd = (p_inv["qty"] * p_inv["avg_cost"]) if p_inv and p_inv["qty"] > 0 else 0.0
            if inv_usd + position > self.cfg.max_position_per_token_usd:
                return False, f"per-token cap (${inv_usd:.0f}/{self.cfg.max_position_per_token_usd:.0f})", 0

        # Capital reserve check (paper or live)
        if self.cfg.mode == Mode.PAPER and self.virtual_portfolio is not None:
            base_amount = position / opp["vwap_ask"]
            ok, why = self.virtual_portfolio.can_execute(opp["buy_ex"], opp["sell_ex"], base, position, base_amount)
            if not ok:
                self.virtual_portfolio.would_block_count += 1
                # Don't reject — let perfect-capital paper continue. But count.
        elif self.cfg.mode == Mode.LIVE and self.balance_cache is not None:
            usdt_have = self.balance_cache.available(opp["buy_ex"], "USDT")
            base_have = self.balance_cache.available(opp["sell_ex"], base)
            if usdt_have < position * 1.01:    # 1% buffer for fee
                # Shrink to the USDT actually on the buy venue rather than skipping
                # the window outright — 39 above-threshold opps died overnight on
                # "no USDT on mexc ($11<$25)" while $160 idled on the other venues.
                shrunk = usdt_have / 1.01
                floor = max(min_required * 1.05 if min_required > 0 else 0.0, 5.0)
                if shrunk < floor:
                    return False, f"no USDT on {opp['buy_ex']} (${usdt_have:.2f}<${position:.0f})", 0
                position = shrunk
            base_amount = position / opp["vwap_ask"]
            if base_have < base_amount:
                # Same idea on the sell side: trade the inventory we do have.
                shrunk = base_have * opp["vwap_ask"]
                floor = max(min_required * 1.05 if min_required > 0 else 0.0, 5.0)
                if shrunk < floor:
                    return False, f"no {base} on {opp['sell_ex']} ({base_have:.4f}<{base_amount:.4f})", 0
                position = min(position, shrunk)

        # Total capital binding check — sum of the actual sizes of in-flight trades
        used_capital = sum(self.active_position_usd.values())
        max_bind = self.cfg.total_capital_usd * (1 - self.cfg.reserve_pct)
        if used_capital + position > max_bind:
            return False, f"capital reserve hit (${used_capital:.0f}+${position:.0f}>{max_bind:.0f})", 0

        return True, "ok", position

    # ---------- execution entry ----------
    async def consider(self, opp):
        self.considered += 1
        ok, reason, position = self.allowed(opp)
        if not ok:
            self.rejects[reason] += 1
            return None

        opp = dict(opp)
        opp["__position_size"] = position
        sym = opp["symbol"]
        self.active.add(sym)
        self.active_position_usd[sym] = position
        rec = None
        exec_start = time.time()
        log.info(
            f"[OPEN] {sym} | {opp['buy_ex']} -> {opp['sell_ex']} | "
            f"pos=${position:.0f} spread={opp.get('real_net_pct', 0):.3f}% "
            f"ask={opp.get('buy_ask', 0):.6g} bid={opp.get('sell_bid', 0):.6g}"
        )
        if self.notifier is not None:
            try:
                asyncio.create_task(self.notifier.notify_open(opp, position))
            except Exception:
                pass
        try:
            if self.cfg.mode == Mode.PAPER:
                rec = await self._execute_paper(opp)
            else:
                rec = await self._execute_live(opp)
            if rec is not None:
                rec.exec_latency_ms = (time.time() - exec_start) * 1000
                opp_seen_ts = opp.get("__seen_ts", exec_start)
                rec.opp_age_ms = (exec_start - opp_seen_ts) * 1000
            self._update_after_trade(rec)
            if rec and rec.status == "ok":
                log.info(
                    f"[CLOSE OK] {sym} | {opp['buy_ex']} -> {opp['sell_ex']} | "
                    f"buy={rec.buy_fill_price:.6g} sell={rec.sell_fill_price:.6g} "
                    f"qty={rec.base_filled:.4f} pnl=${rec.actual_pnl_usd:.4f} ({rec.actual_net_pct:.3f}%) "
                    f"latency={rec.exec_latency_ms:.0f}ms age={rec.opp_age_ms:.0f}ms"
                )
            elif rec:
                log.warning(
                    f"[CLOSE {rec.status.upper()}] {sym} | {opp['buy_ex']} -> {opp['sell_ex']} | "
                    f"err={rec.error} latency={rec.exec_latency_ms:.0f}ms"
                )
            return rec
        except Exception as e:
            self.rejects[f"exec_err:{type(e).__name__}"] += 1
            log.exception(f"execute crashed for {sym}: {e}")
            return None
        finally:
            self.active.discard(sym)
            self.active_position_usd.pop(sym, None)
            cd = self.cfg.cooldown_after_win_sec
            if rec is None or (rec.actual_pnl_usd or 0) <= 0:
                cd = self.cfg.cooldown_after_loss_sec
            self.cooldowns[sym] = time.time() + cd

    def _update_after_trade(self, rec: TradeRecord):
        if rec is None:
            return
        self.total_trades += 1
        # ---- leg-risk telemetry ----
        self.outcomes[rec.status] += 1
        if rec.status == "ok":
            self.last_fill_ts = time.time()
            if rec.actual_net_pct is not None and rec.expected_net_pct is not None:
                self.slip_sum += (rec.expected_net_pct - rec.actual_net_pct)
                self.slip_n += 1
        for src in (rec.pre_exec_buy_src, rec.pre_exec_sell_src):
            if src is not None:
                self.preexec_legs += 1
                if src == "ws":
                    self.preexec_ws_hits += 1
        pnl = rec.actual_pnl_usd or 0.0
        self.daily_pnl_usd += pnl
        if pnl > 0:
            self.total_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.cfg.consecutive_losses_trigger:
                self.paused_until = time.time() + self.cfg.pause_on_loss_streak_sec
        self._journal(rec)
        self._save_state()
        if self.notifier is not None:
            try:
                asyncio.create_task(self.notifier.notify_close(rec))
            except Exception:
                pass

    # ---------- PAPER execution ----------
    async def _execute_paper(self, opp) -> TradeRecord:
        position_usd = opp.get("__position_size") or self.cfg.position_size_usd
        rec = TradeRecord(
            id=str(uuid.uuid4())[:8], ts=time.time(), mode="paper",
            symbol=opp["symbol"], buy_ex=opp["buy_ex"], sell_ex=opp["sell_ex"],
            target_usd=position_usd,
            expected_net_pct=opp.get("real_net_pct", 0),
        )

        # Simulate live latency: wait 150ms, re-check spread, abort if decayed
        if self.cfg.pre_exec_sanity_check:
            await asyncio.sleep(0.15)
            hub = opp.get("__hub")
            if hub is not None:
                fresh_buy = hub.tickers.get(opp["buy_ex"], {}).get(opp["symbol"])
                fresh_sell = hub.tickers.get(opp["sell_ex"], {}).get(opp["symbol"])
                if fresh_buy and fresh_sell:
                    fresh_ask = fresh_buy.get("ask", 0)
                    fresh_bid = fresh_sell.get("bid", 0)
                    if fresh_ask > 0 and fresh_bid > 0:
                        old_spread = (opp["sell_bid"] - opp["buy_ask"]) / opp["buy_ask"] * 100
                        new_spread = (fresh_bid - fresh_ask) / fresh_ask * 100
                        decay = old_spread - new_spread
                        if decay > self.cfg.pre_exec_max_decay_pct:
                            rec.status = "aborted_spread_decayed"
                            rec.error = f"paper sanity: spread {old_spread:.3f}%→{new_spread:.3f}% (decay {decay:.3f}%)"
                            rec.actual_pnl_usd = 0.0
                            rec.actual_net_pct = 0.0
                            log.debug(f"[SANITY ABORT] {opp['symbol']} spread decayed {old_spread:.3f}%→{new_spread:.3f}%")
                            return rec

        vwap_a = opp.get("vwap_ask")
        vwap_b = opp.get("vwap_bid")
        if vwap_a is None or vwap_b is None or vwap_a <= 0:
            rec.status = "failed"; rec.error = "no vwap data"; return rec

        base = position_usd / vwap_a
        from depth import taker_fee_for
        cost = vwap_a * base * (1 + taker_fee_for(opp["buy_ex"]))
        proceeds = vwap_b * base * (1 - taker_fee_for(opp["sell_ex"]))
        wfee_usd = (opp.get("wfee_pct") or 0) / 100 * position_usd
        pnl = proceeds - cost - wfee_usd

        log.info(
            f"[PAPER FILL] buy@{opp['buy_ex']} {vwap_a:.6g} | sell@{opp['sell_ex']} {vwap_b:.6g} | "
            f"qty={base:.4f} cost=${cost:.4f} proceeds=${proceeds:.4f} wfee=${wfee_usd:.4f}"
        )

        rec.buy_fill_price = vwap_a
        rec.sell_fill_price = vwap_b
        rec.base_filled = base
        rec.actual_pnl_usd = pnl
        rec.actual_net_pct = pnl / position_usd * 100
        rec.status = "ok"

        token = opp["symbol"].split("/")[0]
        if self.guard is not None:
            self.guard.on_fill(opp["buy_ex"], token, "buy", base, vwap_a)
            self.guard.on_fill(opp["sell_ex"], token, "sell", base, vwap_b)
            # Sync hedge ONCE after both legs — only residual drift gets hedged.
            if self.hedge is not None:
                net_qty = self.guard.positions.get(token, {}).get("qty", 0.0)
                mark = (vwap_a + vwap_b) / 2
                await self.hedge.adjust(token, net_qty, mark)
        if self.bidir is not None:
            self.bidir.record(token, opp["buy_ex"], opp["sell_ex"])
        if self.virtual_portfolio is not None:
            self.virtual_portfolio.apply_trade(
                opp["buy_ex"], opp["sell_ex"], token, position_usd, base, vwap_a, vwap_b,
                taker_fee_for(opp["buy_ex"])
            )
        return rec

    # ---------- LIVE execution ----------
    async def _execute_live(self, opp) -> TradeRecord:
        position_usd = opp.get("__position_size") or self.cfg.position_size_usd
        rec = TradeRecord(
            id=str(uuid.uuid4())[:8], ts=time.time(), mode="live",
            symbol=opp["symbol"], buy_ex=opp["buy_ex"], sell_ex=opp["sell_ex"],
            target_usd=position_usd,
            expected_net_pct=opp.get("real_net_pct", 0),
        )
        ex_buy = self.ex_by_id.get(opp["buy_ex"])
        ex_sell = self.ex_by_id.get(opp["sell_ex"])
        if not ex_buy or not ex_sell:
            rec.status = "failed"; rec.error = "exchange missing"; return rec

        sym = opp["symbol"]
        target_base = position_usd / opp["vwap_ask"]
        # Adaptive IOC buffer: scale with the spread so fat-spread legs cross even
        # when a volatile microcap's top-of-book jitters (the mexc sell leg was
        # missing at a flat 0.2% buffer), while thin opps keep a tight buffer.
        _net = opp.get("real_net_pct", 0) or 0
        buf = max(self.cfg.ioc_buffer_min_pct,
                  min(self.cfg.ioc_buffer_max_pct, _net * self.cfg.ioc_buffer_net_frac))
        ioc_buy_price = opp["vwap_ask"] * (1 + buf / 100)
        ioc_sell_price = opp["vwap_bid"] * (1 - buf / 100)

        # Pre-execution sanity check — verify spread still real via fresh REST
        if self.cfg.pre_exec_sanity_check:
            try:
                # Prefer a warm ws top-of-book (hot-set feeder) over a REST fetch — it's
                # ~100-400ms fresh vs a ~150ms round-trip, and cuts the pre-fire window.
                # REST-fetch only the side(s) lacking a fresh ws quote.
                hub = opp.get("__hub")
                ws_age = getattr(self.cfg, "ws_quote_max_age_sec", 1.5)
                fresh_ask = fresh_bid = None
                src_buy = src_sell = "rest"
                if hub is not None and hasattr(hub, "ws_quote"):
                    qb = hub.ws_quote(ex_buy.id, sym, ws_age)
                    if qb:
                        fresh_ask = qb[1]; src_buy = "ws"
                    qs = hub.ws_quote(ex_sell.id, sym, ws_age)
                    if qs:
                        fresh_bid = qs[0]; src_sell = "ws"
                need = []
                if fresh_ask is None:
                    need.append(("buy", ex_buy))
                if fresh_bid is None:
                    need.append(("sell", ex_sell))
                if need:
                    obs = await asyncio.gather(*[
                        asyncio.wait_for(ex.fetch_order_book(sym, limit=5), timeout=2.0)
                        for _, ex in need
                    ])
                    for (side, _), ob in zip(need, obs):
                        if side == "buy":
                            fresh_ask = ob["asks"][0][0]
                        else:
                            fresh_bid = ob["bids"][0][0]
                # Stale-feed guard: a ws quote can lag the real book by 1-3% (mexc
                # spot feed does this), making the spread look huge when it isn't —
                # we then reprice onto the phantom price and the leg never crosses
                # (BTC/VELVET traps). If the ws-implied spread is implausibly wide,
                # don't trust ws — REST-verify the ws-sourced side(s) against the
                # live matching engine and use those.
                ws_spread_pct = (fresh_bid - fresh_ask) / fresh_ask * 100 if fresh_ask else 0
                verify_thr = getattr(self.cfg, "ws_verify_spread_pct", 0.8)
                if ws_spread_pct >= verify_thr and (src_buy == "ws" or src_sell == "ws"):
                    reverify = []
                    if src_buy == "ws":
                        reverify.append(("buy", ex_buy))
                    if src_sell == "ws":
                        reverify.append(("sell", ex_sell))
                    obs = await asyncio.gather(*[
                        asyncio.wait_for(ex.fetch_order_book(sym, limit=5), timeout=2.0)
                        for _, ex in reverify
                    ], return_exceptions=True)
                    for (side, _), ob in zip(reverify, obs):
                        if isinstance(ob, Exception):
                            continue
                        if side == "buy" and ob.get("asks"):
                            fresh_ask = float(ob["asks"][0][0]); src_buy = "rest-verified"
                        elif side == "sell" and ob.get("bids"):
                            fresh_bid = float(ob["bids"][0][0]); src_sell = "rest-verified"
                    log.info(f"[PRE-EXEC] {sym} ws spread {ws_spread_pct:.2f}% >= {verify_thr}% "
                             f"— REST-verified -> bid={fresh_bid:.6g} ask={fresh_ask:.6g}")
                rec.pre_exec_buy_src = src_buy
                rec.pre_exec_sell_src = src_sell
                if src_buy == "ws" or src_sell == "ws":
                    log.info(f"[PRE-EXEC] {sym} buy_src={src_buy}@{ex_buy.id} sell_src={src_sell}@{ex_sell.id} (ws skips REST fetch)")
                fresh_spread_pct = (fresh_bid - fresh_ask) / fresh_ask * 100
                expected_spread_pct = (opp["sell_bid"] - opp["buy_ask"]) / opp["buy_ask"] * 100
                decay = expected_spread_pct - fresh_spread_pct
                if decay > self.cfg.pre_exec_max_decay_pct:
                    rec.status = "aborted_spread_decayed"
                    rec.error = f"spread decay {decay:.3f}% (expected {expected_spread_pct:.3f}, fresh {fresh_spread_pct:.3f})"
                    rec.actual_pnl_usd = 0.0
                    rec.actual_net_pct = 0.0
                    return rec
                # Spread still holds — REPRICE off the fresh top-of-book so both IOC legs
                # actually cross. The depth-eval vwap goes stale in the ~1s between scan
                # and execution, which was leaving the buy leg below the live ask (no fill)
                # while the sell leg crossed → one-legged fills.
                if fresh_ask > 0 and fresh_bid > 0:
                    target_base = position_usd / fresh_ask
                    ioc_buy_price = fresh_ask * (1 + buf / 100)
                    ioc_sell_price = fresh_bid * (1 - buf / 100)
            except Exception as e:
                rec.status = "aborted_sanity_check"
                rec.error = f"sanity check failed: {str(e)[:80]}"
                rec.actual_pnl_usd = 0.0
                rec.actual_net_pct = 0.0
                return rec

        if self.cfg.use_ioc_orders:
            async def buy():
                return await asyncio.wait_for(
                    ex_buy.create_order(sym, "limit", "buy", target_base, ioc_buy_price,
                                         {"timeInForce": "IOC"}),
                    timeout=self.cfg.order_timeout_sec,
                )

            async def sell():
                return await asyncio.wait_for(
                    ex_sell.create_order(sym, "limit", "sell", target_base, ioc_sell_price,
                                          {"timeInForce": "IOC"}),
                    timeout=self.cfg.order_timeout_sec,
                )
        else:
            async def buy():
                return await asyncio.wait_for(
                    ex_buy.create_market_buy_order(sym, target_base),
                    timeout=self.cfg.order_timeout_sec,
                )

            async def sell():
                return await asyncio.wait_for(
                    ex_sell.create_market_sell_order(sym, target_base),
                    timeout=self.cfg.order_timeout_sec,
                )

        log.info(
            f"[ORDER PLACE] BUY {sym}@{opp['buy_ex']} qty={target_base:.4f} ioc_price={ioc_buy_price:.6g} | "
            f"SELL {sym}@{opp['sell_ex']} qty={target_base:.4f} ioc_price={ioc_sell_price:.6g} | buf={buf:.3f}%"
        )
        # Fire both in parallel
        results = await asyncio.gather(buy(), sell(), return_exceptions=True)
        buy_res, sell_res = results

        buy_ok = not isinstance(buy_res, Exception)
        sell_ok = not isinstance(sell_res, Exception)

        # Venue-level API trading bans are permanent for the symbol (e.g. bingx
        # 100421 "this symbol is not allowed to place via api" on GENIUS) — learn
        # the pair so allowed() stops feeding it (-$0.50 lesson on 2026-06-11).
        for _ok, _res, _exid in ((buy_ok, buy_res, opp["buy_ex"]), (sell_ok, sell_res, opp["sell_ex"])):
            if not _ok and "not allowed to place via api" in str(_res):
                API_BANNED_PAIRS.add((_exid, sym))
                log.warning(f"[API-BAN] {sym}@{_exid}: symbol not tradeable via API — pair banned for this run")

        async def _reconcile(ex, res):
            """Return (filled_qty, avg_price). Some venues (e.g. mexc IOC) return
            filled/average=None on the create response even when the order executed.
            Re-fetch by id, then fall back to aggregating recent my_trades."""
            if not isinstance(res, dict):
                return 0.0, 0.0
            filled = res.get("filled")
            price = res.get("average") or res.get("price")
            if filled is not None and price:
                return float(filled), float(price)
            oid = res.get("id")
            if oid:
                try:
                    o = await asyncio.wait_for(ex.fetch_order(oid, sym), timeout=self.cfg.order_timeout_sec)
                    f, p = o.get("filled"), (o.get("average") or o.get("price"))
                    if f is not None and p:
                        return float(f), float(p)
                    filled = f if f is not None else filled
                    price = p or price
                except Exception as e:
                    log.warning(f"[RECONCILE] fetch_order {sym}@{ex.id} failed: {str(e)[:80]}")
                try:
                    tr = await asyncio.wait_for(ex.fetch_my_trades(sym, limit=20), timeout=self.cfg.order_timeout_sec)
                    lots = [t for t in tr if str(t.get("order")) == str(oid)]
                    if lots:
                        qty = sum(float(t.get("amount") or 0) for t in lots)
                        cost = sum(float(t.get("cost") or 0) for t in lots)
                        if qty > 0:
                            return qty, cost / qty
                except Exception as e:
                    log.warning(f"[RECONCILE] my_trades {sym}@{ex.id} failed: {str(e)[:80]}")
            return float(filled or 0), float(price or 0)

        if buy_ok and sell_ok:
            buy_filled, buy_price = await _reconcile(ex_buy, buy_res)
            sell_filled, sell_price = await _reconcile(ex_sell, sell_res)
            base = min(buy_filled, sell_filled)
            cost = buy_price * base
            proc = sell_price * base
            log.info(
                f"[FILL] BUY@{opp['buy_ex']} filled={buy_filled:.4f} avg={buy_price:.6g} | "
                f"SELL@{opp['sell_ex']} filled={sell_filled:.4f} avg={sell_price:.6g} | "
                f"net_qty={base:.4f} pnl=${proc - cost:.4f}"
            )
            # One-legged diagnostic: both orders ACCEPTED but one returned filled=0
            # (its IOC didn't cross). Snapshot the live book on the dead side to see
            # whether our price missed, the size was too deep, or the book vanished —
            # this is the dominant loss mode (75% of attempts) and we were blind to why.
            if (buy_filled <= 0) != (sell_filled <= 0):
                dead_ex = ex_buy if buy_filled <= 0 else ex_sell
                dead_side = "BUY" if buy_filled <= 0 else "SELL"
                our_px = ioc_buy_price if buy_filled <= 0 else ioc_sell_price
                try:
                    our_px = float(our_px or 0)
                    ob = await asyncio.wait_for(dead_ex.fetch_order_book(sym, limit=5), timeout=2.0)
                    if dead_side == "BUY":
                        lvl = float((ob.get("asks") or [[0]])[0][0] or 0)   # need ask <= our bid
                        gap = (lvl / our_px - 1) * 100 if our_px else 0
                        miss = lvl > our_px
                    else:
                        lvl = float((ob.get("bids") or [[0]])[0][0] or 0)   # need bid >= our ask
                        gap = (our_px / lvl - 1) * 100 if lvl else 0
                        miss = lvl < our_px
                    verdict = "price_missed" if miss else "book_moved/size"
                    self.leg_miss[f"{dead_ex.id}:{verdict}"] += 1
                    # Phantom-window circuit breaker: a price_missed with a gap this
                    # wide means the SCAN feed was stale (the spread wasn't real) —
                    # the buy filled, the sell can't cross by 2.7%, and we just
                    # accumulate one-sided inventory (VELVET trap, 2026-06-11). Count
                    # strikes per token; soft-ban after a few so we stop seeding it.
                    if miss and gap >= PHANTOM_GAP_PCT:
                        _pt = sym.split("/")[0]
                        PHANTOM_STRIKES[_pt] = PHANTOM_STRIKES.get(_pt, 0) + 1
                        if PHANTOM_STRIKES[_pt] >= PHANTOM_MAX_STRIKES:
                            PHANTOM_BANNED_UNTIL[_pt] = time.time() + PHANTOM_BAN_SEC
                            log.error(f"[PHANTOM] {_pt}: {PHANTOM_STRIKES[_pt]} stale-feed "
                                      f"strikes (gap {gap:+.2f}%) — soft-banned {PHANTOM_BAN_SEC/60:.0f}min")
                    log.warning(
                        f"[LEG-MISS] {dead_side} {sym}@{dead_ex.id} filled=0 | our_ioc={our_px:.6g} "
                        f"book_top={lvl:.6g} gap={gap:+.3f}% verdict={verdict} "
                        f"| opp_age={float(rec.opp_age_ms or 0):.0f}ms buf={buf:.3f}%"
                    )
                except Exception as e:
                    self.leg_miss[f"{dead_ex.id}:unknown"] += 1
                    log.warning(f"[LEG-MISS] {dead_side} {sym}@{dead_ex.id} filled=0 (book fetch failed: {str(e)[:50]})")
            token = sym.split("/")[0]
            # Record ACTUAL per-leg fills (not just the matched base) so inventory tracks
            # reality even when the legs fill asymmetrically.
            if self.guard is not None:
                if buy_filled > 0:
                    self.guard.on_fill(opp["buy_ex"], token, "buy", buy_filled, buy_price)
                if sell_filled > 0:
                    self.guard.on_fill(opp["sell_ex"], token, "sell", sell_filled, sell_price)
            rec.buy_fill_price = buy_price
            rec.sell_fill_price = sell_price
            rec.base_filled = base
            rec.actual_pnl_usd = proc - cost
            rec.actual_net_pct = (proc - cost) / position_usd * 100
            rec.status = "ok" if base > 0 else "partial"

            # Leg imbalance: one leg filled more than the other => unintended one-sided
            # exposure. Two cases:
            #   over-SOLD (residual<0): we depleted token inventory / risk going short =>
            #     MUST buy it back to flatten (critical).
            #   over-BOUGHT (residual>0): we hold extra of a WHITELISTED token we want
            #     inventory of anyway => KEEP it (flattening would just eat the spread+fees,
            #     and selling exactly `filled` fails on fee-reduced balance). Just record it.
            residual = buy_filled - sell_filled
            unwind_price = buy_price or sell_price
            if abs(residual) * (unwind_price or 0) >= 1.5:
                if residual > 0:
                    log.warning(f"[IMBALANCE] over-bought {residual:.4f} {token}@{opp['buy_ex']} — kept as inventory (whitelisted)")
                    rec.status = "kept_buy_excess"
                else:
                    qty = -residual
                    log.warning(f"[IMBALANCE] over-sold {qty:.4f} {token}@{opp['sell_ex']} — aggressive IOC buy to flatten")
                    try:
                        # Limit IOC instead of market buy: bitget's market buys take
                        # COST not qty (createMarketBuyOrderRequiresPrice) — an
                        # aggressive crossing limit behaves identically everywhere.
                        _px = float(ex_sell.price_to_precision(sym, unwind_price * 1.01))
                        cres = await asyncio.wait_for(
                            ex_sell.create_order(sym, "limit", "buy", qty, _px, {"timeInForce": "IOC"}),
                            timeout=self.cfg.order_timeout_sec)
                        cf, cp = await _reconcile(ex_sell, cres)
                        if self.guard is not None:
                            self.guard.on_fill(opp["sell_ex"], token, "buy", cf or qty, cp or sell_price)
                        rec.status = "rebalanced_sell_excess"
                    except Exception as e:
                        rec.status = "imbalanced_unwind_failed"
                        rec.error = f"residual {residual:.4f} {token}: {e}"
                        log.error(f"[IMBALANCE] unwind failed {sym}: {e} — guard will carry residual")

            # Sync hedge on the net tracked inventory (no-op for hedge-excluded tokens).
            if self.guard is not None and self.hedge is not None and (buy_filled > 0 or sell_filled > 0):
                net_qty = self.guard.positions.get(token, {}).get("qty", 0.0)
                mark = unwind_price or ((buy_price + sell_price) / 2 if (buy_price and sell_price) else 0)
                if mark > 0:
                    await self.hedge.adjust(token, net_qty, mark)
            if base > 0 and self.bidir is not None:
                self.bidir.record(token, opp["buy_ex"], opp["sell_ex"])
            return rec

        # One side failed → emergency hedge: close the side that succeeded.
        # Record every real fill (the leg that filled AND the emergency close) into
        # the inventory guard, so net inventory reflects reality. If a close fails,
        # the open leg stays recorded and the guard's stop-loss will unwind it later.
        token = sym.split("/")[0]
        if not buy_ok:
            log.error(f"[ORDER FAIL] BUY {sym}@{opp['buy_ex']}: {buy_res}")
        if not sell_ok:
            log.error(f"[ORDER FAIL] SELL {sym}@{opp['sell_ex']}: {sell_res}")

        def _fill_qty(res):
            return float(res.get("filled") or 0) if res else 0.0

        def _fill_price(res, fallback=0.0):
            return float(res.get("average") or res.get("price") or fallback) if res else fallback

        from depth import taker_fee_for
        realized = 0.0   # actual realized cash PnL on this attempt (0 if nothing filled)
        if buy_ok and not sell_ok:
            filled, buy_price = await _reconcile(ex_buy, buy_res)
            if buy_price <= 0:
                buy_price = opp["vwap_ask"]
            if filled > 0 and self.guard is not None:
                self.guard.on_fill(opp["buy_ex"], token, "buy", filled, buy_price)
            try:
                if filled > 0:
                    # Fee may be taken in base currency, so free balance can be
                    # slightly below `filled` — selling `filled` then fails with
                    # "balance not enough". Cap at the actual free amount.
                    close_qty = filled
                    try:
                        bal = await asyncio.wait_for(ex_buy.fetch_balance(), timeout=10)
                        avail = float((bal.get(token) or {}).get("free") or 0)
                        if 0 < avail < close_qty:
                            close_qty = avail
                    except Exception:
                        pass
                    log.warning(f"[HEDGE] sell failed — closing buy leg: market_sell {sym}@{opp['buy_ex']} qty={close_qty:.4f}")
                    close_res = await asyncio.wait_for(
                        ex_buy.create_market_sell_order(sym, close_qty),
                        timeout=self.cfg.order_timeout_sec,
                    )
                    cf = _fill_qty(close_res) or close_qty
                    cp = _fill_price(close_res, buy_price)
                    if self.guard is not None:
                        self.guard.on_fill(opp["buy_ex"], token, "sell", cf, cp)
                    # Real PnL: bought `filled`@buy_price, sold `cf`@cp, both taker.
                    fee = taker_fee_for(opp["buy_ex"])
                    realized = cp * cf * (1 - fee) - buy_price * filled * (1 + fee)
                    rec.status = "hedged_sell_failed"
                else:
                    rec.status = "buy_only_unfilled"
            except Exception as e:
                rec.status = "hedge_failed"
                rec.error = f"sell_err={sell_res}; hedge_err={e}"
                log.error(f"[HEDGE FAIL] {sym}: {e} — holding {filled:.4f} {token}, guard will unwind")
                # Open leg still held → loss is unrealized; guard's stop-loss books it
                # if/when it unwinds. Don't fabricate a number into daily PnL here.
            else:
                rec.error = f"sell_err={sell_res}"
        elif sell_ok and not buy_ok:
            filled, sell_price = await _reconcile(ex_sell, sell_res)
            if sell_price <= 0:
                sell_price = opp["vwap_bid"]
            if filled > 0 and self.guard is not None:
                self.guard.on_fill(opp["sell_ex"], token, "sell", filled, sell_price)
            try:
                if filled > 0:
                    log.warning(f"[HEDGE] buy failed — closing sell leg: IOC buy {sym}@{opp['sell_ex']} qty={filled:.4f}")
                    # Aggressive crossing IOC, not market buy — bitget market buys
                    # take COST not qty and error without a price.
                    _px = float(ex_sell.price_to_precision(sym, sell_price * 1.01))
                    close_res = await asyncio.wait_for(
                        ex_sell.create_order(sym, "limit", "buy", filled, _px, {"timeInForce": "IOC"}),
                        timeout=self.cfg.order_timeout_sec,
                    )
                    cf = _fill_qty(close_res) or filled
                    cp = _fill_price(close_res, sell_price)
                    if self.guard is not None:
                        self.guard.on_fill(opp["sell_ex"], token, "buy", cf, cp)
                    # Real PnL: sold `filled`@sell_price, bought back `cf`@cp, both taker.
                    fee = taker_fee_for(opp["sell_ex"])
                    realized = sell_price * filled * (1 - fee) - cp * cf * (1 + fee)
                    rec.status = "hedged_buy_failed"
                else:
                    rec.status = "sell_only_unfilled"
            except Exception as e:
                rec.status = "hedge_failed"
                rec.error = f"buy_err={buy_res}; hedge_err={e}"
                log.error(f"[HEDGE FAIL] {sym}: {e} — short {filled:.4f} {token}, guard will unwind")
            else:
                rec.error = f"buy_err={buy_res}"
        else:
            rec.status = "both_failed"
            rec.error = f"buy={buy_res}; sell={sell_res}"
            log.error(f"[BOTH FAILED] {sym}")

        # Use the REAL realized cash flow (0 when nothing filled or loss is still
        # unrealized in inventory). No more fabricated -$0.5 / -0.2% polluting PnL.
        rec.actual_pnl_usd = realized
        rec.actual_net_pct = (realized / position_usd * 100) if position_usd else 0.0
        return rec
