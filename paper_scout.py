"""Paper scout — scans the paper instance for NEW promising arb coins on the live
route (mexc/bitget/bingx) and prints a CANDIDATE line (once per coin) so the
operator/agent can evaluate liquidity and seed+whitelist it.

Run periodically (e.g. via Monitor loop). Dedups via scout_alerted.txt.
Env knobs: SCOUT_LOOKBACK_H (4), SCOUT_PNL_MIN ($8), SCOUT_WIN_MIN (3).
"""
import json, os, time
from collections import defaultdict
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(ROOT, ".env"))
PAPER = os.path.join(os.path.dirname(ROOT), "orbit_paper", "trades.jsonl")
ALERTED = os.path.join(ROOT, "scout_alerted.txt")
ROUTE = {"mexc", "bitget", "bingx"}
WL = {t.strip().upper() for t in os.getenv("WHITELIST", "").split(",") if t.strip()}
LOOKBACK_H = float(os.getenv("SCOUT_LOOKBACK_H", "4"))
PNL_MIN = float(os.getenv("SCOUT_PNL_MIN", "8"))
WIN_MIN = int(os.getenv("SCOUT_WIN_MIN", "3"))


def alerted_set():
    try:
        return {l.strip() for l in open(ALERTED) if l.strip()}
    except FileNotFoundError:
        return set()


def mark(tok):
    with open(ALERTED, "a") as f:
        f.write(tok + "\n")


def main():
    try:
        rows = [json.loads(l) for l in open(PAPER, encoding="utf-8") if l.strip()]
    except FileNotFoundError:
        return
    cut = time.time() - LOOKBACK_H * 3600
    rows = [r for r in rows if r.get("status") == "ok" and r.get("ts", 0) >= cut
            and r.get("buy_ex") in ROUTE and r.get("sell_ex") in ROUTE]
    agg = defaultdict(lambda: {"pnl": 0.0, "ts": []})
    for r in rows:
        t = r["symbol"].split("/")[0]
        agg[t]["pnl"] += r.get("actual_pnl_usd", 0)
        agg[t]["ts"].append(r["ts"])
    done = alerted_set()
    skip = WL | {"HOME"} | done
    for tok, d in sorted(agg.items(), key=lambda x: -x[1]["pnl"]):
        if tok in skip:
            continue
        ts = sorted(d["ts"])
        wins = 1
        for a, b in zip(ts, ts[1:]):
            if b - a > 600:
                wins += 1
        if d["pnl"] >= PNL_MIN and wins >= WIN_MIN:
            print(f"CANDIDATE {tok}: paper pnl ${d['pnl']:.2f} / {wins} windows in last {LOOKBACK_H:.0f}h (not in whitelist)")
            mark(tok)


if __name__ == "__main__":
    main()
