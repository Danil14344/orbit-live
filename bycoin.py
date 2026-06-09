import json, time, statistics as st
from collections import defaultdict

rows=[json.loads(l) for l in open("trades.jsonl") if l.strip()]
now=max(r["ts"] for r in rows)
by=defaultdict(list)
for r in rows:
    by[r["symbol"].split("/")[0]].append(r)

agg=[]
for tok,ts in by.items():
    pnls=[t["actual_pnl_usd"] for t in ts]
    nets=[t["actual_net_pct"] for t in ts]
    last=max(t["ts"] for t in ts)
    first=min(t["ts"] for t in ts)
    age_h=(now-last)/3600
    agg.append({
        "tok":tok,"n":len(ts),"pnl":sum(pnls),
        "avg_net":st.mean(nets),"med_net":st.median(nets),"max_net":max(nets),
        "last_age_h":age_h,"span_h":(last-first)/3600,
        "routes":set(f"{t['buy_ex']}/{t['sell_ex']}" for t in ts),
    })

print(f"now anchor = last trade ts; total trades={len(rows)}")
print(f"{'TOKEN':10} {'n':>3} {'PnL$':>8} {'avgNet%':>7} {'medNet%':>7} {'maxNet%':>7} {'lastAgo_h':>9} {'spanH':>6}  routes")
for a in sorted(agg,key=lambda x:-x["pnl"]):
    mexc_dep = any('mexc' in r for r in a['routes'])
    print(f"{a['tok']:10} {a['n']:>3} {a['pnl']:>8.3f} {a['avg_net']:>7.2f} {a['med_net']:>7.2f} {a['max_net']:>7.2f} {a['last_age_h']:>9.1f} {a['span_h']:>6.1f}  {','.join(sorted(a['routes']))}")

print("\n=== STILL ALIVE (last trade < 12h ago) ===")
alive=[a for a in agg if a["last_age_h"]<12]
for a in sorted(alive,key=lambda x:-x["pnl"]):
    print(f"  {a['tok']:10} n={a['n']:>3} pnl={a['pnl']:>7.3f} medNet={a['med_net']:.2f}% lastAgo={a['last_age_h']:.1f}h")

print("\n=== DEAD (no trade in >24h) ===")
dead=[a for a in agg if a["last_age_h"]>24]
print("  "+", ".join(f"{a['tok']}({a['last_age_h']:.0f}h)" for a in sorted(dead,key=lambda x:x['last_age_h'])))
