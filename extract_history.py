import re, json, sys

log_path = r"C:/Users/danil/.claude/projects/c--Users-danil-Downloads-Telegram-Desktop-mexc-session-v7/df05edd4-7bea-4350-a382-249100d5a2ea.jsonl"
log = open(log_path, encoding="utf-8").read()

# Find escaped trade records inside tool outputs
pat = re.compile(r'\{\\"id\\": \\"[a-f0-9]{8}\\".*?actual_pnl_usd\\":\s*[-\d.e]+.*?\}', re.DOTALL)
matches = pat.findall(log)
print(f"raw matches: {len(matches)}", file=sys.stderr)

records = []
seen_ids = set()
for m in matches:
    s = m.replace('\\"', '"').replace('\\n', '').replace('\\\\', '\\')
    try:
        r = json.loads(s)
        if r.get("id") not in seen_ids:
            seen_ids.add(r.get("id"))
            records.append(r)
    except Exception:
        pass

print(f"unique parsed: {len(records)}", file=sys.stderr)

# Append to existing trades.jsonl (deduplicating against existing IDs)
existing_ids = set()
existing_path = r"c:/Users/danil/Downloads/Telegram Desktop/orbit/trades.jsonl"
try:
    with open(existing_path) as f:
        for line in f:
            try:
                r = json.loads(line)
                existing_ids.add(r.get("id"))
            except Exception:
                pass
except FileNotFoundError:
    pass

new = [r for r in records if r.get("id") not in existing_ids]
print(f"new to add: {len(new)}", file=sys.stderr)
with open(existing_path, "a") as f:
    for r in sorted(new, key=lambda x: x.get("ts", 0)):
        f.write(json.dumps(r) + "\n")
print(f"done", file=sys.stderr)
