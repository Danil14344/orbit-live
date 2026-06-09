"""Trade journal analyzer.

Usage:
  python analyze.py            — full report
  python analyze.py --hours 6  — only last 6 hours
  python analyze.py --pair WARD — only WARD pair
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict, Counter


def load(path, since_ts=None, pair_filter=None):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
                if since_ts and r.get("ts", 0) < since_ts:
                    continue
                if pair_filter and pair_filter.upper() not in r.get("symbol", "").upper():
                    continue
                out.append(r)
            except Exception:
                pass
    return out


def fmt_dur(sec):
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec/60:.1f}min"
    if sec < 86400:
        return f"{sec/3600:.1f}h"
    return f"{sec/86400:.1f}d"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=None, help="last N hours only")
    ap.add_argument("--pair", default=None, help="filter pair (substring)")
    ap.add_argument("--journal", default="trades.jsonl")
    ap.add_argument("--stops", default="stops.jsonl")
    args = ap.parse_args()

    since = time.time() - args.hours * 3600 if args.hours else None
    trades = load(args.journal, since, args.pair)
    stops = load(args.stops, since, args.pair)

    if not trades:
        print("no trades in window")
        return

    n = len(trades)
    wins = sum(1 for r in trades if r["actual_pnl_usd"] > 0)
    pnl = sum(r["actual_pnl_usd"] for r in trades)
    elapsed = trades[-1]["ts"] - trades[0]["ts"]
    print(f"\n=== TRADES ({fmt_dur(elapsed)}) ===")
    print(f"  total: {n} | wins: {wins} ({wins/n*100:.1f}%) | pnl: ${pnl:.2f}")
    print(f"  rate: {n/(elapsed/3600):.1f}/h | avg pnl/trade: ${pnl/n:.4f}")
    print(f"  best: ${max(r['actual_pnl_usd'] for r in trades):.3f} | worst: ${min(r['actual_pnl_usd'] for r in trades):.3f}")

    # By pair+route
    print(f"\n=== TOP PAIRS ===")
    by_route = defaultdict(list)
    for r in trades:
        by_route[(r["symbol"], r["buy_ex"], r["sell_ex"])].append(r)
    rows = []
    for (sym, b, s), rs in by_route.items():
        rows.append((sym, b, s, len(rs), sum(x["actual_pnl_usd"] for x in rs)))
    rows.sort(key=lambda x: -x[4])
    for sym, b, s, cnt, p in rows[:15]:
        print(f"  {sym:18} {b:8}->{s:8} x{cnt:4} ${p:7.3f} avg ${p/cnt:.4f}")

    # Bidirectional analysis
    print(f"\n=== BIDIRECTIONAL ===")
    pair_dirs = defaultdict(set)
    for r in trades:
        token = r["symbol"].split("/")[0]
        a, b = sorted([r["buy_ex"], r["sell_ex"]])
        pair_dirs[(token, a, b)].add(r["buy_ex"])
    bidir = sum(1 for dirs in pair_dirs.values() if len(dirs) >= 2)
    print(f"  pairs total: {len(pair_dirs)} | bidirectional: {bidir} ({bidir/len(pair_dirs)*100:.0f}%)")
    bidir_pnl = 0.0
    unidir_pnl = 0.0
    for r in trades:
        token = r["symbol"].split("/")[0]
        a, b = sorted([r["buy_ex"], r["sell_ex"]])
        if len(pair_dirs[(token, a, b)]) >= 2:
            bidir_pnl += r["actual_pnl_usd"]
        else:
            unidir_pnl += r["actual_pnl_usd"]
    print(f"  bidir PnL: ${bidir_pnl:.2f} ({bidir_pnl/pnl*100:.0f}%) | unidir PnL: ${unidir_pnl:.2f}")

    # By exchange involvement
    print(f"\n=== EXCHANGE PARTICIPATION ===")
    ex_count = Counter()
    ex_pnl = defaultdict(float)
    for r in trades:
        for e in (r["buy_ex"], r["sell_ex"]):
            ex_count[e] += 1
            ex_pnl[e] += r["actual_pnl_usd"] / 2
    for e, c in ex_count.most_common():
        print(f"  {e:10} x{c:5}  pnl-share=${ex_pnl[e]:.2f}")

    # Hourly PnL distribution
    print(f"\n=== HOURLY DISTRIBUTION ===")
    hour_pnl = defaultdict(float)
    hour_cnt = defaultdict(int)
    for r in trades:
        h = int(r["ts"] // 3600)
        hour_pnl[h] += r["actual_pnl_usd"]
        hour_cnt[h] += 1
    hours = sorted(hour_pnl.keys())
    for h in hours[-12:]:
        bar = "#" * int(hour_cnt[h] / 2)
        print(f"  hour-{int((time.time()//3600)-h):2}h  trades={hour_cnt[h]:3} pnl=${hour_pnl[h]:6.2f}  {bar}")

    # Stops
    if stops:
        print(f"\n=== STOPS ===")
        sl_loss = sum(s["loss_usd"] for s in stops)
        print(f"  triggered: {len(stops)} | total loss: ${sl_loss:.2f}")
        sc = Counter((s["ex"], s["token"]) for s in stops)
        for (e, t), c in sc.most_common(10):
            print(f"  {e:10} {t:10} x{c}")
        if pnl != 0:
            print(f"  stops as % of arb profit: {abs(sl_loss)/pnl*100:.0f}%")


if __name__ == "__main__":
    main()
