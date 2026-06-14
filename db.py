"""SQLite mirror of trades.jsonl + stops.jsonl for fast queries.

Parallel to JSONL: every appended trade also gets inserted here.
Used by dashboard and analyze for indexed queries.
"""
import json
import sqlite3
import time
from pathlib import Path

from appdir import BASE_DIR

# Must resolve against the stable app home (folder of the .exe when frozen), NOT
# Path(__file__).parent — in a PyInstaller build that points into a per-process
# _MEIPASS temp dir, so the scanner and the dashboard would each open a DIFFERENT
# orbit.db (split-brain: dashboard shows no trades / bot "offline").
DB_PATH = BASE_DIR / "orbit.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    mode TEXT,
    symbol TEXT,
    base TEXT,
    buy_ex TEXT,
    sell_ex TEXT,
    target_usd REAL,
    expected_net_pct REAL,
    buy_fill_price REAL,
    sell_fill_price REAL,
    base_filled REAL,
    actual_pnl_usd REAL,
    actual_net_pct REAL,
    status TEXT,
    error TEXT,
    opp_age_ms REAL,
    exec_latency_ms REAL
);
CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades(ts);
CREATE INDEX IF NOT EXISTS ix_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS ix_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS ix_trades_buyex ON trades(buy_ex);

CREATE TABLE IF NOT EXISTS stops (
    ts REAL NOT NULL,
    ex TEXT,
    token TEXT,
    qty REAL,
    sell_price REAL,
    avg_cost REAL,
    loss_usd REAL,
    drop_pct REAL
);
CREATE INDEX IF NOT EXISTS ix_stops_ts ON stops(ts);
CREATE INDEX IF NOT EXISTS ix_stops_token ON stops(token);
"""


def init_db():
    with get_conn() as c:
        c.executescript(SCHEMA)


def insert_trade(rec: dict):
    base = (rec.get("symbol") or "").split("/")[0]
    cols = [
        "id", "ts", "mode", "symbol", "base", "buy_ex", "sell_ex",
        "target_usd", "expected_net_pct", "buy_fill_price", "sell_fill_price",
        "base_filled", "actual_pnl_usd", "actual_net_pct", "status", "error",
        "opp_age_ms", "exec_latency_ms",
    ]
    vals = (
        rec.get("id"), rec.get("ts"), rec.get("mode"), rec.get("symbol"), base,
        rec.get("buy_ex"), rec.get("sell_ex"), rec.get("target_usd"),
        rec.get("expected_net_pct"), rec.get("buy_fill_price"), rec.get("sell_fill_price"),
        rec.get("base_filled"), rec.get("actual_pnl_usd"), rec.get("actual_net_pct"),
        rec.get("status"), rec.get("error"), rec.get("opp_age_ms"), rec.get("exec_latency_ms"),
    )
    placeholders = ",".join("?" * len(cols))
    try:
        with get_conn() as c:
            c.execute(f"INSERT OR REPLACE INTO trades ({','.join(cols)}) VALUES ({placeholders})", vals)
    except Exception:
        pass


def insert_stop(rec: dict):
    cols = ["ts", "ex", "token", "qty", "sell_price", "avg_cost", "loss_usd", "drop_pct"]
    vals = tuple(rec.get(c) for c in cols)
    try:
        with get_conn() as c:
            c.execute(f"INSERT INTO stops ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
    except Exception:
        pass


def import_jsonl(jsonl_path, kind="trades"):
    """One-time backfill from existing JSONL."""
    p = Path(jsonl_path)
    if not p.exists():
        return 0
    n = 0
    with p.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                if kind == "trades":
                    insert_trade(r)
                else:
                    insert_stop(r)
                n += 1
            except Exception:
                pass
    return n


if __name__ == "__main__":
    init_db()
    nt = import_jsonl("trades.jsonl", "trades")
    ns = import_jsonl("stops.jsonl", "stops")
    print(f"imported: {nt} trades, {ns} stops")
