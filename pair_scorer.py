"""Pair/exchange scorer — SUGGEST mode (no config changes, no orders).

Reads trades.jsonl, ranks (token) by robust quality on currently-active exchanges,
prints a report + a suggested WHITELIST line, and flags which *additional* exchange
would unlock the strongest tokens that current routes can't trade.

Run: py pair_scorer.py     (optionally: set ACTIVE_EXCHANGES="mexc,bingx")
"""
import json, os, time, math, statistics as st
from collections import defaultdict

TRADES = os.getenv("SCORE_TRADES_FILE", "trades.jsonl")
ACTIVE = {e.strip().lower() for e in os.getenv("ACTIVE_EXCHANGES", "mexc,bingx").split(",") if e.strip()}
ALIVE_H = float(os.getenv("SCORE_ALIVE_H", "12"))      # "still alive" window
MIN_N = int(os.getenv("SCORE_MIN_N", "5"))              # min trades to trust a token
MIN_NET = float(os.getenv("SCORE_MIN_NET", "0.30"))    # min median net% to whitelist
GLITCH_NET = 5.0                                        # net% above this = likely bad-quote outlier
MAX_WHITELIST = int(os.getenv("SCORE_MAX_WHITELIST", "6"))


def load():
    rows = []
    if not os.path.exists(TRADES):
        return rows
    with open(TRADES) as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try: rows.append(json.loads(ln))
                except Exception: pass
    return rows


def main():
    rows = load()
    if not rows:
        print("no trades"); return
    now = max(r["ts"] for r in rows)

    # group by token
    by_tok = defaultdict(list)
    for r in rows:
        by_tok[r["symbol"].split("/")[0]].append(r)

    active_scores = []   # tokens tradeable on ACTIVE exchanges
    locked = []          # strong tokens whose flow needs a non-active exchange

    for tok, ts in by_tok.items():
        act = [t for t in ts if t["buy_ex"] in ACTIVE and t["sell_ex"] in ACTIVE]
        allnets = [t["actual_net_pct"] for t in ts]
        last_all = max(t["ts"] for t in ts)
        age_all = (now - last_all) / 3600

        def pack(trs):
            nets = [t["actual_net_pct"] for t in trs]
            pnl = sum(t["actual_pnl_usd"] for t in trs)
            last = max(t["ts"] for t in trs)
            age = (now - last) / 3600
            glitch_pnl = sum(t["actual_pnl_usd"] for t in trs if t["actual_net_pct"] > GLITCH_NET)
            dirs = {(t["buy_ex"], t["sell_ex"]) for t in trs}
            bidir = any((b, a) in dirs for (a, b) in dirs)
            return {
                "n": len(trs), "pnl": pnl, "median_net": st.median(nets), "mean_net": st.mean(nets),
                "max_net": max(nets), "last_age_h": age, "glitch_share": (glitch_pnl / pnl if pnl else 0),
                "dirs": dirs, "bidir": bidir,
            }

        if act:
            m = pack(act)
            alive = m["last_age_h"] < ALIVE_H
            recency = max(0.0, 1 - m["last_age_h"] / ALIVE_H)
            score = m["median_net"] * math.sqrt(m["n"]) * (0.3 + 0.7 * recency)
            eligible = alive and m["n"] >= MIN_N and m["median_net"] >= MIN_NET and m["glitch_share"] < 0.5
            reasons = []
            if not alive: reasons.append(f"dead {m['last_age_h']:.0f}h")
            if m["n"] < MIN_N: reasons.append(f"thin n={m['n']}")
            if m["median_net"] < MIN_NET: reasons.append(f"low net {m['median_net']:.2f}%")
            if m["glitch_share"] >= 0.5: reasons.append(f"glitch {m['glitch_share']*100:.0f}%")
            active_scores.append((tok, score, m, eligible, reasons))
        else:
            # strong token not tradeable on active set — which exchange would unlock it?
            buy_exs = defaultdict(int)
            for t in ts: buy_exs[t["buy_ex"]] += 1
            top_ex = max(buy_exs, key=buy_exs.get)
            if age_all < ALIVE_H and len(ts) >= MIN_N and st.median(allnets) >= MIN_NET:
                locked.append((tok, len(ts), st.median(allnets), age_all, top_ex))

    active_scores.sort(key=lambda x: -x[1])
    print(f"# anchor=now(last trade)  ACTIVE={sorted(ACTIVE)}  alive<{ALIVE_H}h  min_n={MIN_N}  min_net={MIN_NET}%")
    print(f"{'TOKEN':10} {'score':>6} {'n':>3} {'medNet%':>7} {'PnL$':>7} {'maxNet%':>7} {'ageH':>5} {'glitch':>6} {'bidir':>5}  ok")
    for tok, score, m, elig, reasons in active_scores:
        flag = "WL" if elig else ("- " + ",".join(reasons))
        print(f"{tok:10} {score:>6.2f} {m['n']:>3} {m['median_net']:>7.2f} {m['pnl']:>7.2f} {m['max_net']:>7.2f} "
              f"{m['last_age_h']:>5.1f} {m['glitch_share']*100:>5.0f}% {str(m['bidir']):>5}  {flag}")

    wl = [tok for tok, _, _, elig, _ in active_scores if elig][:MAX_WHITELIST]
    print(f"\n>>> SUGGESTED WHITELIST={','.join(wl) if wl else '(none qualify)'}")

    if locked:
        print("\n# Strong tokens NOT tradeable on active exchanges (would unlock by adding an exchange):")
        for tok, n, med, age, ex in sorted(locked, key=lambda x: -x[1])[:8]:
            print(f"  {tok:10} n={n:>3} medNet={med:.2f}% ageH={age:.1f}  -> needs exchange: {ex}")

    # persist report
    with open("pair_scores_report.txt", "w") as f:
        f.write(f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')} ACTIVE={sorted(ACTIVE)}\n")
        f.write(f"SUGGESTED WHITELIST={','.join(wl) if wl else '(none)'}\n")
    print("\n(report written to pair_scores_report.txt — SUGGEST only, nothing applied)")


if __name__ == "__main__":
    main()
