import json, time, statistics as st
from collections import Counter

trades=[json.loads(l) for l in open("trades.jsonl") if l.strip()]
print("=== TIER 3 (Pro / full bot) — executed paper trades ===")
print(f"trades: {len(trades)}")
if trades:
    pnls=[t["actual_pnl_usd"] for t in trades]
    nets=[t["actual_net_pct"] for t in trades]
    wins=[p for p in pnls if p>0]
    span=trades[-1]["ts"]-trades[0]["ts"]
    print(f"window: {time.strftime('%Y-%m-%d %H:%M',time.localtime(trades[0]['ts']))} -> {time.strftime('%H:%M',time.localtime(trades[-1]['ts']))}  ({span/3600:.1f}h)")
    print(f"win rate: {len(wins)}/{len(trades)} = {100*len(wins)/len(trades):.0f}%")
    print(f"total PnL: {sum(pnls):+.4f} USDT")
    print(f"avg net%: {st.mean(nets):.3f}%  | median: {st.median(nets):.3f}%")
    bt=max(trades,key=lambda t:t['actual_pnl_usd']); wt=min(trades,key=lambda t:t['actual_pnl_usd'])
    print(f"best:  {bt['actual_pnl_usd']:+.4f} USDT ({bt['symbol']})")
    print(f"worst: {wt['actual_pnl_usd']:+.4f} USDT ({wt['symbol']})")
    print(f"avg opp_age: {st.mean(t['opp_age_ms'] for t in trades):.1f}ms | avg exec_latency: {st.mean(t['exec_latency_ms'] for t in trades):.1f}ms")
    pairs=Counter(f"{t['buy_ex']}->{t['sell_ex']}" for t in trades)
    print("by route:")
    for k,v in pairs.most_common(): print(f"  {k}: {v}")
    print("trades:")
    for t in trades:
        print(f"  {time.strftime('%H:%M:%S',time.localtime(t['ts']))} {t['symbol']:12} {t['buy_ex']}->{t['sell_ex']:8} net={t['actual_net_pct']:.3f}% pnl={t['actual_pnl_usd']:+.4f}")

try:
    sh=[json.loads(l) for l in open("shadow_opps.jsonl") if l.strip()]
    print(f"\nshadow_opps (would-have-traded candidates): {len(sh)} (last {time.strftime('%Y-%m-%d %H:%M',time.localtime(sh[-1].get('ts',0)))})")
except Exception as e:
    print("shadow_opps:",e)

p=json.load(open("virtual_portfolio.json"))
print(f"\nstart_usd: {p.get('start_usd')}")
for k in ("realized_pnl_usd","total_equity_usd","equity_usd","realized_pnl","realized","pnl_usd"):
    if k in p: print(f"{k}: {p[k]}")
print("portfolio keys:", list(p.keys()))
