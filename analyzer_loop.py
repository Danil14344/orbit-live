"""Periodic analyzer — runs pair_scorer + inv_alert every INTERVAL and appends to
analysis_report.log. SUGGEST/ALERT only — never changes config, never trades.

Run: py analyzer_loop.py    (ANALYZER_INTERVAL_SEC overrides cadence, default 3h)
"""
import os, sys, time, subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(ROOT, "analysis_report.log")
INTERVAL = int(os.getenv("ANALYZER_INTERVAL_SEC", "10800"))  # 3h
PY = sys.executable

# Research feed: score the PAPER-LAB data across all 6 venues.
PAPER_TRADES = os.path.join(os.path.dirname(ROOT), "orbit_paper", "trades.jsonl")
PAPER_ENV = {
    "SCORE_TRADES_FILE": PAPER_TRADES,
    "ACTIVE_EXCHANGES": "mexc,kucoin,bitget,htx,bingx,bitmart",
}


def run(script, extra_env=None):
    try:
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        r = subprocess.run([PY, os.path.join(ROOT, script)], cwd=ROOT,
                           capture_output=True, text=True, timeout=120, env=env)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"[{script} failed: {e}]"


def main():
    while True:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        block = [f"\n{'='*70}\n# {ts}\n{'='*70}",
                 "----- pair_scorer (PAPER-LAB, 6 venues) -----", run("pair_scorer.py", PAPER_ENV),
                 "----- inv_alert (LIVE execution) -----", run("inv_alert.py")]
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(block) + "\n")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
