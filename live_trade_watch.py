"""Watch for the FIRST live trade, then exit (so the parent gets notified).
Polls trades.jsonl for new mode==live rows. Read-only — no orders, no config changes.
"""
import json, os, time

ROOT = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(ROOT, "trades.jsonl")
OUT = os.path.join(ROOT, "live_trade_alert.txt")
POLL = 45
MAX_SEC = 8 * 3600  # give up after 8h


def live_rows():
    rows = []
    try:
        with open(TRADES) as f:
            for ln in f:
                if '"mode": "live"' in ln:
                    try: rows.append(json.loads(ln))
                    except Exception: pass
    except FileNotFoundError:
        pass
    return rows


def main():
    baseline = len(live_rows())
    start = time.time()
    while time.time() - start < MAX_SEC:
        time.sleep(POLL)
        rows = live_rows()
        if len(rows) > baseline:
            new = rows[baseline:]
            lines = ["FIRST LIVE TRADE(S) DETECTED @ " + time.strftime("%Y-%m-%d %H:%M:%S")]
            for t in new:
                lines.append(
                    f"  {t.get('symbol')} {t.get('buy_ex')}->{t.get('sell_ex')} "
                    f"net={t.get('actual_net_pct'):.3f}% pnl=${t.get('actual_pnl_usd'):+.4f} "
                    f"target=${t.get('target_usd')} status={t.get('status')}"
                )
            text = "\n".join(lines)
            with open(OUT, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print(text)
            return
    print("live_trade_watch: timed out after 8h, no live trade")


if __name__ == "__main__":
    main()
