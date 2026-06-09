import json, time
ts=[json.loads(l)["ts"] for l in open("trades.jsonl") if l.strip()]
ts.sort()
print(f"trades={len(ts)}  span={(ts[-1]-ts[0])/3600:.1f}h  rate={len(ts)/((ts[-1]-ts[0])/3600):.2f}/h")
# biggest gaps
gaps=[(ts[i+1]-ts[i], ts[i], ts[i+1]) for i in range(len(ts)-1)]
gaps.sort(reverse=True)
print("biggest gaps (no trades):")
for g,a,b in gaps[:8]:
    print(f"  {g/3600:.2f}h  {time.strftime('%m-%d %H:%M',time.localtime(a))} -> {time.strftime('%m-%d %H:%M',time.localtime(b))}")
# trades per hour bucket recent
import collections
now=ts[-1]
buckets=collections.Counter()
for t in ts:
    h=int((now-t)//3600)
    buckets[h]+=1
print("trades in last N hours (h ago: count):")
for h in range(0,12):
    print(f"  {h}h: {buckets.get(h,0)}")
