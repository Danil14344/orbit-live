"""Eye Crypt Bot — local dashboard (served at http://localhost:8765).

Brand-matching UI for the desktop bot client. Reads bot state files (trades.jsonl,
executor_state.json, etc.) and exposes control endpoints (settings, start/stop,
update check).
"""
import json
import os
import time
import subprocess
import sys
from collections import defaultdict, Counter
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

try:
    from logsetup import init_logging, get_logger
    init_logging("dashboard")
    log = get_logger("dashboard")
except Exception:
    import logging
    log = logging.getLogger("dashboard")

from appdir import BASE_DIR
ROOT = BASE_DIR
VERSION = "1.0.3"   # bumped on each release; auto-update compares this
DEFAULT_BACKEND = "https://api.eyecryptbot.com"

app = FastAPI()


# ────────────────────────────── file helpers ──────────────────────────────

def safe_load_json(path):
    p = ROOT / path
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def load_jsonl(path, since_ts=None):
    p = ROOT / path
    if not p.exists():
        return []
    out = []
    with p.open() as f:
        for line in f:
            try:
                r = json.loads(line)
                if since_ts and r.get("ts", 0) < since_ts:
                    continue
                out.append(r)
            except Exception:
                pass
    return out


# ────────────────────────────── API: stats ──────────────────────────────

@app.get("/api/stats")
def api_stats():
    trades = load_jsonl("trades.jsonl")
    stops = load_jsonl("stops.jsonl")
    exec_state = safe_load_json("executor_state.json")
    inventory = safe_load_json("inventory_state.json")
    portfolio = safe_load_json("virtual_portfolio.json")

    n = len(trades)
    if n == 0:
        return {"empty": True}

    wins = sum(1 for r in trades if r.get("actual_pnl_usd", 0) > 0)
    pnl = sum(r.get("actual_pnl_usd", 0) for r in trades)
    elapsed = trades[-1]["ts"] - trades[0]["ts"] if n > 1 else 1

    lat_records = [r for r in trades if r.get("exec_latency_ms") is not None]
    avg_lat = sum(r["exec_latency_ms"] for r in lat_records) / len(lat_records) if lat_records else 0
    avg_age = sum(r.get("opp_age_ms") or 0 for r in lat_records) / len(lat_records) if lat_records else 0

    by_route = defaultdict(list)
    for r in trades:
        by_route[(r["symbol"], r["buy_ex"], r["sell_ex"])].append(r)
    pairs = []
    for (sym, b, s), rs in by_route.items():
        pairs.append({
            "symbol": sym, "buy": b, "sell": s,
            "count": len(rs), "pnl": sum(x.get("actual_pnl_usd", 0) for x in rs),
        })
    pairs.sort(key=lambda x: -x["pnl"])

    ex_count = Counter()
    ex_pnl = defaultdict(float)
    for r in trades:
        for e in (r["buy_ex"], r["sell_ex"]):
            ex_count[e] += 1
            ex_pnl[e] += r.get("actual_pnl_usd", 0) / 2

    hour_pnl = defaultdict(float)
    hour_cnt = defaultdict(int)
    for r in trades:
        h = int(r["ts"] // 3600)
        hour_pnl[h] += r.get("actual_pnl_usd", 0)
        hour_cnt[h] += 1
    hours = sorted(hour_pnl.keys())[-24:]
    hourly = [{"hour": h, "trades": hour_cnt[h], "pnl": hour_pnl[h]} for h in hours]

    # last 20 trades for live feed
    recent = sorted(trades, key=lambda r: r["ts"], reverse=True)[:20]
    recent = [{
        "ts": r["ts"], "symbol": r["symbol"],
        "buy_ex": r["buy_ex"], "sell_ex": r["sell_ex"],
        "pnl": r.get("actual_pnl_usd", 0),
        "net_pct": r.get("actual_net_pct", 0),
    } for r in recent]

    extrap_valid = elapsed >= 1800 and n >= 10
    return {
        "summary": {
            "total_trades": n, "wins": wins,
            "win_rate_pct": wins / n * 100,
            "pnl_usd": pnl, "elapsed_hours": elapsed / 3600,
            "rate_per_hour": n / max(1, elapsed / 3600) if elapsed >= 60 else None,
            "extrap_24h_pnl": pnl * 86400 / elapsed if extrap_valid else None,
            "avg_pnl_per_trade": pnl / n,
            "best_trade": max(r.get("actual_pnl_usd", 0) for r in trades),
            "worst_trade": min(r.get("actual_pnl_usd", 0) for r in trades),
        },
        "latency": {"avg_exec_ms": avg_lat, "avg_opp_age_ms": avg_age},
        "executor_state": exec_state,
        "top_pairs": pairs[:15],
        "exchanges": [{"name": e, "trades": c, "pnl": ex_pnl[e]} for e, c in ex_count.most_common()],
        "hourly": hourly,
        "recent": recent,
        "stops": {"count": len(stops), "total_loss_usd": sum(s.get("loss_usd", 0) for s in stops)},
        "inventory_count": sum(1 for v in (inventory.get("positions") or {}).values() if v.get("qty", 0) > 0),
    }


# ────────────────────────────── API: status & version ──────────────────────────────

def _read_env() -> dict:
    p = ROOT / ".env"
    out = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _write_env(data: dict):
    p = ROOT / ".env"
    lines = []
    seen = set()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k = line.split("=", 1)[0].strip()
                if k in data:
                    lines.append(f"{k}={data[k]}")
                    seen.add(k)
                else:
                    lines.append(line)
            else:
                lines.append(line)
    for k, v in data.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _scanner_alive() -> bool:
    p = ROOT / "scanner_heartbeat.txt"
    if not p.exists():
        return False
    try:
        return time.time() - p.stat().st_mtime < 90
    except Exception:
        return False


@app.get("/api/status")
def api_status():
    env = _read_env()
    return {
        "version": VERSION,
        "scanner_alive": _scanner_alive(),
        "mode": env.get("MODE", "paper"),
        "has_license": bool(env.get("EYECRYPT_LICENSE")),
        "backend": env.get("EYECRYPT_API", DEFAULT_BACKEND),
        "paused": env.get("PAUSED", "0") == "1",
        "ts": time.time(),
    }


@app.get("/api/balances")
def api_balances():
    """Live exchange balances, written by the scanner's RealBalanceCache."""
    snap = safe_load_json("balances_snapshot.json")
    if not snap:
        return {"empty": True}
    return snap


@app.get("/api/version-check")
def api_version_check():
    """Check backend for newer version of the bot."""
    env = _read_env()
    backend = env.get("EYECRYPT_API", DEFAULT_BACKEND)
    try:
        import httpx
        r = httpx.get(f"{backend}/v1/bot/latest-version", timeout=8)
        if r.status_code != 200:
            return {"current": VERSION, "latest": VERSION, "update_available": False}
        j = r.json()
        latest = j.get("version", VERSION)
        return {
            "current": VERSION,
            "latest": latest,
            "update_available": _version_gt(latest, VERSION),
            "download_url": j.get("download_url"),
            "changelog": j.get("changelog", ""),
        }
    except Exception as e:
        return {"current": VERSION, "latest": VERSION, "update_available": False, "error": str(e)}


def _version_gt(a: str, b: str) -> bool:
    try:
        ap = [int(x) for x in a.split(".")]
        bp = [int(x) for x in b.split(".")]
        return ap > bp
    except Exception:
        return False


# ────────────────────────────── API: settings ──────────────────────────────

class SettingsUpdate(BaseModel):
    license_key: str | None = None
    mode: str | None = None
    position_usd: float | None = None
    min_net_pct: float | None = None
    backend_url: str | None = None


@app.get("/api/settings")
def get_settings():
    env = _read_env()
    return {
        "license_key": env.get("EYECRYPT_LICENSE", ""),
        "mode": env.get("MODE", "paper"),
        "position_usd": float(env.get("POSITION_USD", 30)),
        "min_net_pct": float(env.get("MIN_NET_PCT", 0.15)),
        "backend_url": env.get("EYECRYPT_API", DEFAULT_BACKEND),
    }


@app.post("/api/settings")
def update_settings(s: SettingsUpdate):
    updates = {}
    if s.license_key is not None: updates["EYECRYPT_LICENSE"] = s.license_key.strip()
    if s.mode is not None:
        if s.mode not in ("paper", "live"):
            return JSONResponse({"error": "mode must be paper or live"}, status_code=400)
        updates["MODE"] = s.mode
    if s.position_usd is not None: updates["POSITION_USD"] = str(s.position_usd)
    if s.min_net_pct is not None: updates["MIN_NET_PCT"] = str(s.min_net_pct)
    if s.backend_url is not None: updates["EYECRYPT_API"] = s.backend_url.strip()
    _write_env(updates)
    return {"status": "saved", "restart_required": True}


# ────────────────────────────── API: exchange keys ──────────────────────────────

EXCHANGES = ["mexc", "kucoin", "bitget", "htx", "bingx", "bitmart"]


class ApiKeyUpdate(BaseModel):
    exchange: str
    api_key: str
    api_secret: str
    api_passphrase: str | None = None


@app.get("/api/exchange-keys")
def get_exchange_keys():
    env = _read_env()
    out = []
    for ex in EXCHANGES:
        k = env.get(f"{ex.upper()}_API_KEY", "")
        s = env.get(f"{ex.upper()}_API_SECRET", "")
        p = env.get(f"{ex.upper()}_API_PASSPHRASE", "")
        out.append({
            "exchange": ex,
            "configured": bool(k and s),
            "key_preview": (k[:6] + "…" + k[-4:]) if len(k) > 10 else "",
            "needs_passphrase": ex in ("kucoin", "bitget", "bitmart", "htx"),
        })
    return out


@app.post("/api/exchange-keys")
def set_exchange_keys(k: ApiKeyUpdate):
    if k.exchange not in EXCHANGES:
        return JSONResponse({"error": "unknown exchange"}, status_code=400)
    updates = {
        f"{k.exchange.upper()}_API_KEY": k.api_key.strip(),
        f"{k.exchange.upper()}_API_SECRET": k.api_secret.strip(),
    }
    if k.api_passphrase is not None:
        updates[f"{k.exchange.upper()}_API_PASSPHRASE"] = k.api_passphrase.strip()
    _write_env(updates)
    return {"status": "saved"}


# ────────────────────────────── API: controls ──────────────────────────────

@app.post("/api/control/restart")
def control_restart():
    """Force scanner restart by clearing heartbeat."""
    try:
        p = ROOT / "scanner_heartbeat.txt"
        if p.exists():
            p.unlink()
        return {"status": "ok", "msg": "scanner_will_restart_in_60s"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/control/pause")
def control_pause():
    env = _read_env()
    paused = env.get("PAUSED", "0") == "1"
    _write_env({"PAUSED": "0" if paused else "1"})
    return {"status": "ok", "paused": not paused}


class UpdateRequest(BaseModel):
    download_url: str


# Only these hosts may serve an update binary. Prevents a malicious local web page
# (CSRF) or anything else from pointing the updater at an attacker-controlled .exe.
_ALLOWED_UPDATE_HOSTS = ("api.eyecryptbot.com", "github.com")


def _update_url_ok(url: str) -> bool:
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme != "https":
        return False
    host = (u.hostname or "").lower()
    return host in _ALLOWED_UPDATE_HOSTS or host.endswith(".githubusercontent.com")


@app.post("/api/control/update")
def control_update(req: UpdateRequest):
    """Download new exe + queue swap. Returns immediately; the detached swap.bat
    then kills every .exe process (watchdog + scanner + dashboard), overwrites the
    binary, and relaunches it. No self-exit here — taskkill in the bat is the single
    authority on shutdown so the watchdog can't respawn the old version mid-swap."""
    if not _update_url_ok(req.download_url):
        return JSONResponse({"error": "download_url host not allowed"}, status_code=400)
    # Fetch the expected sha256 server-side from the allowlisted backend rather than
    # trusting the client, so a tampered binary fails the post-download hash check.
    expected_sha = None
    try:
        import httpx
        env = _read_env()
        backend = env.get("EYECRYPT_API", DEFAULT_BACKEND)
        r = httpx.get(f"{backend}/v1/bot/latest-version", timeout=8)
        if r.status_code == 200:
            expected_sha = (r.json().get("sha256") or "").strip() or None
    except Exception as e:
        log.warning(f"could not fetch update sha256: {e}")
    try:
        import updater
        bat = updater.apply_update(req.download_url, expected_sha256=expected_sha)
        if os.name == "nt":
            subprocess.Popen(
                ["cmd", "/c", str(bat)],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                close_fds=True,
            )
        return {"status": "ok", "msg": "update_queued — app will restart in ~5s"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ────────────────────────────── HTML ──────────────────────────────

@app.get("/")
def index():
    return HTMLResponse(HTML)


@app.get("/miniapp")
def miniapp():
    return HTMLResponse(MINIAPP_HTML)


MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
<title>Eye Crypt</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
:root {
  --bg: #07060d; --card: rgba(20,16,38,0.6); --card-border: rgba(139,92,246,0.18);
  --card-border-hov: rgba(217,70,239,0.4); --text: #fafafa; --text-dim: #a1a1aa;
  --text-mute: #71717a; --violet: #8b5cf6; --violet-2: #a78bfa; --fuchsia: #d946ef;
  --fuchsia-2: #f0abfc; --green: #4ade80; --red: #f87171; --yellow: #fbbf24;
}
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); min-height: 100vh; }
body {
  font-family: 'Inter', -apple-system, system-ui, sans-serif; -webkit-font-smoothing: antialiased;
  background-image:
    radial-gradient(ellipse 80% 50% at 50% -10%, rgba(139,92,246,0.18), transparent),
    radial-gradient(ellipse 60% 50% at 90% 110%, rgba(217,70,239,0.12), transparent);
  background-attachment: fixed; padding: 16px 14px 32px;
}
h1,h2,h3 { font-family: 'Space Grotesk','Inter',sans-serif; letter-spacing:-0.02em; margin:0; }
.mono { font-family: 'JetBrains Mono','Consolas',monospace; }
.gradient-text { background: linear-gradient(135deg,var(--violet-2),var(--fuchsia-2));
  -webkit-background-clip:text; background-clip:text; color:transparent; }

/* header */
.brand { display:flex; align-items:center; gap:12px; margin-bottom:18px; }
.brand-mark {
  position:relative; width:42px; height:42px; border-radius:11px;
  background: linear-gradient(135deg,var(--violet),var(--fuchsia));
  display:grid; place-items:center; box-shadow:0 6px 26px -4px rgba(217,70,239,0.5);
  animation: aura 4.5s ease-in-out infinite;
}
.brand-mark svg { width:26px; height:26px; }
@keyframes aura { 0%,100%{ box-shadow:0 6px 22px -6px rgba(139,92,246,0.5);} 50%{ box-shadow:0 8px 34px -2px rgba(217,70,239,0.8);} }
.brand-name { font-family:'Space Grotesk'; font-weight:700; font-size:19px; line-height:1; }
.brand-sub { color:var(--text-mute); font-size:11px; font-family:'JetBrains Mono'; margin-top:3px; }
.pill { margin-left:auto; display:inline-flex; align-items:center; gap:7px; padding:6px 12px;
  border-radius:999px; font-size:12px; background:rgba(139,92,246,0.1); border:1px solid var(--card-border); }
.dot { width:8px; height:8px; border-radius:50%; }
.dot.live{ background:var(--green); box-shadow:0 0 0 0 rgba(74,222,128,0.7); animation:pulse 2s infinite; }
.dot.off{ background:var(--text-mute); } .dot.paused{ background:var(--yellow); }
@keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(74,222,128,0.7);} 70%{box-shadow:0 0 0 9px rgba(74,222,128,0);} 100%{box-shadow:0 0 0 0 rgba(74,222,128,0);} }

/* hero pnl */
.hero { position:relative; overflow:hidden; border-radius:18px; padding:20px; margin-bottom:14px;
  background: var(--card); border:1px solid var(--card-border); backdrop-filter:blur(12px); }
.hero::before { content:""; position:absolute; inset:-1px; border-radius:inherit; padding:1.5px;
  background:linear-gradient(120deg,#8b5cf6,#d946ef,#a855f7,#d946ef,#8b5cf6); background-size:300% 300%;
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0); -webkit-mask-composite:xor;
  mask-composite:exclude; animation:flow 6s linear infinite; pointer-events:none; opacity:0.5; }
@keyframes flow { to { background-position:300% 50%; } }
.hero h2 { font-size:11px; color:var(--text-mute); text-transform:uppercase; letter-spacing:0.12em; font-weight:600; }
.hero .big { font-size:38px; font-weight:700; font-family:'Space Grotesk'; line-height:1.1; margin-top:6px; }
.hero .sub { color:var(--text-dim); font-size:13px; margin-top:6px; }
.pos { color:var(--green); } .neg { color:var(--red); }

/* metric row */
.row { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:14px; }
.stat { background:var(--card); border:1px solid var(--card-border); border-radius:14px; padding:14px; backdrop-filter:blur(12px); }
.stat .k { font-size:10px; color:var(--text-mute); text-transform:uppercase; letter-spacing:0.1em; }
.stat .v { font-size:22px; font-weight:700; font-family:'Space Grotesk'; margin-top:5px; }

/* section */
.section-title { font-size:12px; color:var(--text-mute); text-transform:uppercase; letter-spacing:0.12em;
  font-weight:600; margin:18px 4px 9px; }
.card { background:var(--card); border:1px solid var(--card-border); border-radius:14px; padding:6px 4px;
  backdrop-filter:blur(12px); }
.exrow { display:flex; align-items:center; justify-content:space-between; padding:11px 13px;
  border-bottom:1px solid rgba(139,92,246,0.08); }
.exrow:last-child { border-bottom:none; }
.exname { font-weight:600; font-size:14px; text-transform:capitalize; }
.exsub { font-size:11px; color:var(--text-mute); font-family:'JetBrains Mono'; margin-top:2px; }
.exval { text-align:right; }
.exval .u { font-weight:700; font-family:'Space Grotesk'; font-size:15px; }
.exval .a { font-size:11px; color:var(--text-dim); font-family:'JetBrains Mono'; }
.trade { display:flex; align-items:center; justify-content:space-between; padding:10px 13px;
  border-bottom:1px solid rgba(139,92,246,0.08); font-size:13px; }
.trade:last-child{ border-bottom:none; }
.trade .rt { font-family:'JetBrains Mono'; font-size:11px; color:var(--text-dim); }
.muted { color:var(--text-mute); font-size:13px; text-align:center; padding:18px; }
.err { color:var(--red); font-size:11px; }
.refresh { width:100%; margin-top:18px; padding:13px; border-radius:12px; border:1px solid var(--card-border);
  background:linear-gradient(135deg,rgba(139,92,246,0.22),rgba(217,70,239,0.16)); color:var(--text);
  font-family:inherit; font-weight:600; font-size:14px; cursor:pointer; }
.refresh:active { transform:scale(0.98); }
.foot { text-align:center; color:var(--text-mute); font-size:11px; margin-top:16px; font-family:'JetBrains Mono'; }
</style>
</head>
<body>
<div class="brand">
  <div class="brand-mark">
    <svg viewBox="0 0 24 24" fill="none"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12Z" stroke="#fff" stroke-width="1.6"/><circle cx="12" cy="12" r="3.2" fill="#fff"/></svg>
  </div>
  <div>
    <div class="brand-name">Eye Crypt</div>
    <div class="brand-sub" id="ver">кабинет</div>
  </div>
  <span class="pill"><span class="dot off" id="dot"></span><span id="statusTxt">…</span></span>
</div>

<div class="hero">
  <h2>PnL за всё время</h2>
  <div class="big" id="pnl">—</div>
  <div class="sub" id="pnlSub">загрузка…</div>
</div>

<div class="row">
  <div class="stat"><div class="k">Сделок</div><div class="v" id="trades">—</div></div>
  <div class="stat"><div class="k">Винрейт</div><div class="v" id="wr">—</div></div>
</div>

<div class="section-title">Балансы по биржам</div>
<div class="card" id="balances"><div class="muted">загрузка…</div></div>

<div class="section-title">Последние сделки</div>
<div class="card" id="recent"><div class="muted">загрузка…</div></div>

<button class="refresh" id="refreshBtn">Обновить</button>
<div class="foot" id="foot"></div>

<script>
const tg = window.Telegram ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }
const $ = id => document.getElementById(id);
const usd = v => (v<0?'-':'') + '$' + Math.abs(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const sign = v => (v>=0?'+':'') + usd(v);
const num = (v,d=6) => { let s = (+v).toPrecision(d); return parseFloat(s).toString(); };
const ago = ts => { if(!ts) return ''; const s=Date.now()/1000-ts; if(s<60)return Math.floor(s)+'с'; if(s<3600)return Math.floor(s/60)+'м'; return (s/3600).toFixed(1)+'ч'; };

async function load() {
  try {
    const [st, ba, status] = await Promise.all([
      fetch('/api/stats').then(r=>r.json()),
      fetch('/api/balances').then(r=>r.json()),
      fetch('/api/status').then(r=>r.json()).catch(()=>({})),
    ]);
    // status pill
    const dot=$('dot'); const stx=$('statusTxt');
    if (status.paused){ dot.className='dot paused'; stx.textContent='пауза'; }
    else if (status.scanner_alive){ dot.className='dot live'; stx.textContent=(status.mode||'').toUpperCase()||'онлайн'; }
    else { dot.className='dot off'; stx.textContent='офлайн'; }
    if (status.version) $('ver').textContent='v'+status.version;

    // pnl / stats
    if (st && !st.empty) {
      const s=st.summary;
      $('pnl').textContent=sign(s.pnl_usd); $('pnl').className='big '+(s.pnl_usd>=0?'pos':'neg');
      $('pnlSub').textContent=`${s.total_trades} сделок · ср. ${sign(s.avg_pnl_per_trade)}`;
      $('trades').textContent=s.total_trades;
      $('wr').textContent=s.win_rate_pct.toFixed(1)+'%';
      // recent
      const rc=$('recent');
      if (st.recent && st.recent.length) {
        rc.innerHTML = st.recent.map(t=>{
          const cls=t.pnl>=0?'pos':'neg';
          const base=(t.symbol||'').split('/')[0];
          return `<div class="trade"><div><b>${base}</b> <span class="rt">${t.buy_ex}→${t.sell_ex}</span></div>`+
                 `<div class="${cls}" style="text-align:right"><b>${sign(t.pnl)}</b><div class="rt">${(t.net_pct||0).toFixed(3)}%</div></div></div>`;
        }).join('');
      } else rc.innerHTML='<div class="muted">нет сделок</div>';
    } else {
      $('pnl').textContent='$0.00'; $('pnlSub').textContent='нет сделок';
      $('recent').innerHTML='<div class="muted">нет сделок</div>';
    }

    // balances
    const bc=$('balances');
    if (ba && !ba.empty && ba.exchanges && Object.keys(ba.exchanges).length) {
      let rows='';
      for (const [ex,d] of Object.entries(ba.exchanges)) {
        const assets=Object.entries(d.balances||{}).sort((a,b)=>b[1]-a[1]).slice(0,4)
          .map(([a,q])=>`${a} ${num(q)}`).join(' · ');
        rows += `<div class="exrow"><div><div class="exname">${ex}</div>`+
          `<div class="exsub">${d.error?'<span class=err>'+d.error+'</span>':(assets||'—')} · ${ago(d.updated_at)}</div></div>`+
          `<div class="exval"><div class="u">${usd(d.usdt||0)}</div><div class="a">USDT</div></div></div>`;
      }
      const totVal = (ba.spot_value_total!=null) ? ba.spot_value_total : (ba.usdt_total||0);
      rows += `<div class="exrow"><div class="exname gradient-text">Итого (спот)</div>`+
        `<div class="exval"><div class="u gradient-text">${usd(totVal)}</div><div class="a">всего</div></div></div>`;
      bc.innerHTML=rows;
    } else {
      bc.innerHTML='<div class="muted">балансы недоступны (бот в paper-режиме или ещё не запущен)</div>';
    }
    $('foot').textContent='обновлено '+new Date().toLocaleTimeString('ru-RU');
  } catch(e) {
    $('foot').textContent='ошибка загрузки: '+e;
  }
}
$('refreshBtn').addEventListener('click', ()=>{ if(tg&&tg.HapticFeedback)tg.HapticFeedback.impactOccurred('light'); load(); });
load();
setInterval(load, 20000);
</script>
</body>
</html>"""


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Eye Crypt — Bot Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
* { box-sizing: border-box; }
:root {
  --bg: #07060d;
  --bg2: #0e0a1c;
  --card: rgba(20, 16, 38, 0.6);
  --card-border: rgba(139, 92, 246, 0.18);
  --card-border-hov: rgba(217, 70, 239, 0.4);
  --text: #fafafa;
  --text-dim: #a1a1aa;
  --text-mute: #71717a;
  --violet: #8b5cf6;
  --violet-2: #a78bfa;
  --fuchsia: #d946ef;
  --fuchsia-2: #f0abfc;
  --green: #4ade80;
  --red: #f87171;
  --yellow: #fbbf24;
}
html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text); min-height: 100vh; }
body {
  font-family: 'Inter', -apple-system, system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
  background-image:
    radial-gradient(ellipse 80% 50% at 50% -10%, rgba(139,92,246,0.16), transparent),
    radial-gradient(ellipse 60% 50% at 90% 110%, rgba(217,70,239,0.10), transparent);
  background-attachment: fixed;
}
h1, h2, h3 { font-family: 'Space Grotesk', 'Inter', sans-serif; letter-spacing: -0.02em; margin: 0; }
code, .mono { font-family: 'JetBrains Mono', 'Consolas', monospace; }
.gradient-text {
  background: linear-gradient(135deg, var(--violet-2), var(--fuchsia-2));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}

/* Layout */
.app { max-width: 1280px; margin: 0 auto; padding: 24px; }

/* Top bar */
.topbar {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; padding: 14px 18px; margin-bottom: 18px;
  background: rgba(20,16,38,0.6); border: 1px solid var(--card-border);
  border-radius: 14px; backdrop-filter: blur(20px);
}
.brand { display: flex; align-items: center; gap: 12px; }
.brand-mark {
  width: 36px; height: 36px; border-radius: 9px;
  background: linear-gradient(135deg, var(--violet), var(--fuchsia));
  display: grid; place-items: center; font-weight: 800; color: white; font-size: 18px;
  box-shadow: 0 6px 24px -4px rgba(217,70,239,0.45);
}
.brand-name { font-family: 'Space Grotesk'; font-weight: 700; font-size: 17px; }
.brand-sub { color: var(--text-mute); font-size: 11px; font-family: 'JetBrains Mono'; }

.status-pill {
  display: inline-flex; align-items: center; gap: 8px;
  padding: 6px 12px; border-radius: 999px; font-size: 12px;
  background: rgba(139,92,246,0.1); border: 1px solid var(--card-border);
}
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot.live { background: var(--green); box-shadow: 0 0 0 0 rgba(74,222,128,0.7); animation: pulse 2s infinite; }
.dot.off { background: var(--text-mute); }
.dot.paused { background: var(--yellow); }
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 rgba(74,222,128,0.7); }
  70% { box-shadow: 0 0 0 10px rgba(74,222,128,0); }
  100% { box-shadow: 0 0 0 0 rgba(74,222,128,0); }
}

.topbar-meta { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }

/* Tabs */
.tabs {
  display: flex; gap: 4px; margin-bottom: 20px;
  background: rgba(20,16,38,0.5); border: 1px solid var(--card-border);
  border-radius: 12px; padding: 4px; backdrop-filter: blur(10px);
}
.tab-btn {
  background: transparent; border: none; color: var(--text-dim);
  padding: 9px 18px; font-size: 13px; font-weight: 600; cursor: pointer;
  border-radius: 8px; transition: all 0.2s; font-family: inherit;
}
.tab-btn:hover { color: var(--text); }
.tab-btn.active {
  background: linear-gradient(135deg, rgba(139,92,246,0.25), rgba(217,70,239,0.18));
  color: var(--text); box-shadow: inset 0 0 0 1px rgba(139,92,246,0.4);
}
.tab-pane { display: none; }
.tab-pane.active { display: block; animation: fadeIn 0.3s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

/* Cards & grid */
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
.card {
  background: var(--card); border: 1px solid var(--card-border); border-radius: 14px;
  padding: 18px; backdrop-filter: blur(12px); transition: border-color 0.25s;
}
.card:hover { border-color: var(--card-border-hov); }
.card h2 {
  font-size: 11px; color: var(--text-mute); text-transform: uppercase;
  letter-spacing: 0.12em; font-weight: 600; margin-bottom: 10px; font-family: 'Inter';
}
.metric { font-size: 30px; font-weight: 700; font-family: 'Space Grotesk'; line-height: 1.1; }
.metric.pos { color: var(--green); }
.metric.neg { color: var(--red); }
.metric.warn { color: var(--yellow); }
.metric.grad {
  background: linear-gradient(135deg, var(--violet-2), var(--fuchsia-2));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.sub { color: var(--text-mute); font-size: 12px; margin-top: 6px; }

/* Update banner */
.update-banner {
  background: linear-gradient(135deg, rgba(139,92,246,0.18), rgba(217,70,239,0.14));
  border: 1px solid rgba(217,70,239,0.4); border-radius: 14px;
  padding: 16px 20px; margin-bottom: 18px; display: flex;
  align-items: center; justify-content: space-between; gap: 16px;
}
.update-banner h3 { font-size: 15px; margin-bottom: 4px; }

/* Tables */
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--text-mute); padding: 8px 10px;
     font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em;
     border-bottom: 1px solid var(--card-border); }
td { padding: 9px 10px; border-bottom: 1px solid rgba(255,255,255,0.04); }
tr:last-child td { border-bottom: none; }
td.num { font-variant-numeric: tabular-nums; text-align: right; }
td.tag { color: var(--text-dim); font-family: 'JetBrains Mono'; font-size: 11px; }
.pos { color: var(--green); }
.neg { color: var(--red); }

/* Buttons & inputs */
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 10px 18px; border-radius: 10px; border: none; cursor: pointer;
  font-family: 'Inter'; font-weight: 600; font-size: 13px;
  background: linear-gradient(135deg, var(--violet), var(--fuchsia));
  color: white; transition: transform 0.15s, box-shadow 0.2s;
  box-shadow: 0 8px 24px -8px rgba(217,70,239,0.5);
}
.btn:hover { transform: translateY(-1px); box-shadow: 0 12px 32px -8px rgba(217,70,239,0.6); }
.btn:active { transform: translateY(0); }
.btn-sec {
  background: rgba(139,92,246,0.1); color: var(--text);
  box-shadow: inset 0 0 0 1px var(--card-border);
}
.btn-sec:hover { background: rgba(139,92,246,0.18); box-shadow: inset 0 0 0 1px var(--card-border-hov); }
.btn-danger { background: linear-gradient(135deg, #ef4444, #f87171); box-shadow: 0 8px 24px -8px rgba(239,68,68,0.5); }
.btn-sm { padding: 6px 12px; font-size: 12px; }

.field {
  width: 100%; padding: 10px 12px; border-radius: 9px;
  background: rgba(7,6,13,0.7); border: 1px solid var(--card-border);
  color: var(--text); font-family: 'Inter'; font-size: 13px;
  transition: border 0.2s;
}
.field:focus { outline: none; border-color: var(--fuchsia); }
.label { display: block; color: var(--text-mute); font-size: 11px;
         text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 6px; font-weight: 600; }
.field-group { margin-bottom: 16px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }

/* Toast */
.toast {
  position: fixed; bottom: 24px; right: 24px;
  background: rgba(20,16,38,0.95); border: 1px solid var(--card-border-hov);
  padding: 14px 20px; border-radius: 10px; backdrop-filter: blur(12px);
  font-size: 13px; opacity: 0; transform: translateY(20px); transition: all 0.3s;
  z-index: 100; max-width: 360px;
}
.toast.show { opacity: 1; transform: none; }
.toast.err { border-color: var(--red); }

/* Recent trades feed */
.feed { max-height: 380px; overflow-y: auto; }
.feed-row {
  display: grid; grid-template-columns: 1.2fr 1fr 0.8fr 0.8fr;
  gap: 8px; padding: 10px 4px; border-bottom: 1px solid rgba(255,255,255,0.04);
  font-size: 12px; align-items: center;
}
.feed-row:last-child { border-bottom: none; }
.feed-time { color: var(--text-mute); font-family: 'JetBrains Mono'; font-size: 11px; }
.feed-pnl { font-weight: 600; text-align: right; }

/* Bar viz */
.bar-row { display: flex; align-items: center; gap: 8px; padding: 5px 0; font-size: 12px; }
.bar-label { width: 90px; color: var(--text-dim); }
.bar-bg { flex: 1; height: 6px; background: rgba(255,255,255,0.04); border-radius: 3px; overflow: hidden; }
.bar-fill { height: 100%; background: linear-gradient(90deg, var(--violet), var(--fuchsia)); border-radius: 3px; }
.bar-val { width: 80px; text-align: right; font-variant-numeric: tabular-nums; }

/* Settings */
.settings-section { margin-bottom: 28px; }
.settings-section h3 { font-size: 14px; margin-bottom: 14px; }
.exchange-card { padding: 14px; border-radius: 10px; border: 1px solid var(--card-border); margin-bottom: 10px; }
.ex-header { display: flex; justify-content: space-between; align-items: center; }

@media (max-width: 720px) {
  .app { padding: 12px; }
  .row { grid-template-columns: 1fr; }
  .topbar { flex-direction: column; align-items: flex-start; }
  .feed-row { grid-template-columns: 1fr 1fr; }
}
</style>
</head>
<body>
<div class="app">

  <!-- Top bar -->
  <div class="topbar">
    <div class="brand">
      <div class="brand-mark">E</div>
      <div>
        <div class="brand-name gradient-text">Eye Crypt</div>
        <div class="brand-sub">Bot dashboard · v<span id="ver">…</span></div>
      </div>
    </div>
    <div class="topbar-meta">
      <select id="lang-sw" class="field" style="width:auto; padding:6px 10px; font-size:12px;">
        <option value="en">🇬🇧 EN</option>
        <option value="ru">🇷🇺 RU</option>
        <option value="es">🇪🇸 ES</option>
        <option value="zh">🇨🇳 ZH</option>
      </select>
      <span class="status-pill"><span id="status-dot" class="dot off"></span><span id="status-text">…</span></span>
      <span class="status-pill"><span class="mono" style="color:var(--violet-2);">MODE</span> <span id="status-mode">—</span></span>
      <span class="status-pill"><span class="mono" style="color:var(--violet-2);">LIC</span> <span id="status-lic">—</span></span>
      <button id="pause-btn" class="btn btn-sec btn-sm" onclick="togglePause()" data-i18n="pause">⏸ Pause</button>
    </div>
  </div>

  <!-- Update banner (shown only if update available) -->
  <div id="update-banner" class="update-banner" style="display:none;">
    <div>
      <h3 class="gradient-text"><span data-i18n="updateAvailable">🚀 Update available</span></h3>
      <div class="sub" id="update-info">v? → v?</div>
    </div>
    <div style="display:flex; gap:8px;">
      <button class="btn-sec btn btn-sm" onclick="dismissUpdate()" data-i18n="later">Later</button>
      <button class="btn btn-sm" onclick="doUpdate()" data-i18n="updateNow">Update now</button>
    </div>
  </div>

  <!-- Tabs -->
  <div class="tabs">
    <button class="tab-btn active" data-tab="dash" data-i18n="tabDash">Dashboard</button>
    <button class="tab-btn" data-tab="trades" data-i18n="tabTrades">Trades</button>
    <button class="tab-btn" data-tab="settings" data-i18n="tabSettings">Settings</button>
    <button class="tab-btn" data-tab="keys" data-i18n="tabKeys">API Keys</button>
  </div>

  <!-- Dashboard pane -->
  <div id="pane-dash" class="tab-pane active">
    <div id="dash-content"><div class="card"><h2>Loading…</h2></div></div>
  </div>

  <!-- Trades pane -->
  <div id="pane-trades" class="tab-pane">
    <div class="card">
      <h2 data-i18n="recentTrades">Recent trades (live)</h2>
      <div id="trade-feed" class="feed">…</div>
    </div>
  </div>

  <!-- Settings pane -->
  <div id="pane-settings" class="tab-pane">
    <div class="card" style="max-width:680px;">
      <h2 data-i18n="botConfig">Bot configuration</h2>
      <p class="sub" style="margin:-4px 0 18px;" data-i18n="configHelp">Changes save to local .env. Restart bot to apply.</p>

      <div class="settings-section">
        <h3 class="gradient-text" data-i18n="secLicense">License & connection</h3>
        <div class="field-group">
          <label class="label" data-i18n="licenseKey">License key</label>
          <input id="s-license" class="field mono" type="password" data-i18n-attr="placeholder:licensePh" placeholder="paste JWT from your cabinet"/>
          <div class="sub" style="margin-top:4px;">
            <span data-i18n="licenseFrom">From</span> <a href="https://eyecryptbot.com/cabinet" target="_blank" style="color:var(--fuchsia-2);">eyecryptbot.com/cabinet</a>
          </div>
        </div>
        <div class="field-group">
          <label class="label" data-i18n="backendUrl">Backend URL</label>
          <input id="s-backend" class="field mono" type="text" placeholder="https://api.eyecryptbot.com"/>
        </div>
      </div>

      <div class="settings-section">
        <h3 class="gradient-text" data-i18n="secTrading">Trading</h3>
        <div class="field-group">
          <label class="label" data-i18n="mode">Mode</label>
          <select id="s-mode" class="field">
            <option value="paper" data-i18n="modePaper">Paper (simulation — no real money)</option>
            <option value="live" data-i18n="modeLive">Live (real trading — needs API keys)</option>
          </select>
        </div>
        <div class="row">
          <div class="field-group">
            <label class="label" data-i18n="positionSize">Position size (USD)</label>
            <input id="s-pos" class="field" type="number" min="10" max="10000" step="10"/>
          </div>
          <div class="field-group">
            <label class="label" data-i18n="minSpread">Min net spread %</label>
            <input id="s-net" class="field" type="number" min="0.05" max="2" step="0.05"/>
          </div>
        </div>
      </div>

      <div style="display:flex; gap:10px; margin-top:8px;">
        <button class="btn" onclick="saveSettings()" data-i18n="save">Save</button>
        <button class="btn btn-sec" onclick="restartBot()" data-i18n="restartBot">↻ Restart bot</button>
      </div>
    </div>
  </div>

  <!-- API Keys pane -->
  <div id="pane-keys" class="tab-pane">
    <div class="card" style="max-width:680px;">
      <h2 data-i18n="exchangeKeys">Exchange API keys</h2>
      <p class="sub" style="margin:-4px 0 18px;" data-i18n-html="keysHelp">
        Needed only for <b>live</b> mode. Permissions: <b>Spot trade + read balance</b>.
        <span style="color:var(--red);">NEVER enable withdraw.</span>
      </p>
      <div id="keys-list">…</div>
    </div>
  </div>

</div>

<div id="toast" class="toast"></div>

<script>
// ─────────────── i18n ───────────────
const I18N = {
  en: {
    online:"ONLINE", offline:"OFFLINE", pause:"⏸ Pause", resume:"▶ Resume",
    updateAvailable:"🚀 Update available", later:"Later", updateNow:"Update now",
    tabDash:"Dashboard", tabTrades:"Trades", tabSettings:"Settings", tabKeys:"API Keys",
    pnlLive:"PnL (live run)", trades:"Trades", rate:"Rate", avgTrade:"Avg trade",
    proj24:"24h projection", latency:"Latency", stops:"Stops", openPos:"Open positions",
    best:"best", worst:"worst", wins:"wins", elapsed:"elapsed", perTrade:"per executed trade",
    wait:"wait", needMore:"need 30m + 10 trades", extrapolated:"extrapolated",
    oppAge:"opp age", totalLoss:"total loss", activeInv:"active inventory",
    topPairs:"Top pairs by PnL", symbol:"Symbol", route:"Route", pnl:"PnL", avg:"Avg",
    exchanges:"Exchanges", hourlyPnl:"Hourly PnL (last 24h)", noData:"no data yet",
    noTrades:"No trades yet", noTradesSub:"Start the bot and wait for the first arbitrage opportunity. Usually 1-5 minutes.",
    recentTrades:"Recent trades (live)", botConfig:"Bot configuration",
    configHelp:"Changes save to local .env. Restart bot to apply.",
    secLicense:"License & connection", licenseKey:"License key", licensePh:"paste JWT from your cabinet",
    licenseFrom:"From", backendUrl:"Backend URL",
    secTrading:"Trading", mode:"Mode",
    modePaper:"Paper (simulation — no real money)", modeLive:"Live (real trading — needs API keys)",
    positionSize:"Position size (USD)", minSpread:"Min net spread %",
    save:"Save", restartBot:"↻ Restart bot",
    exchangeKeys:"Exchange API keys",
    keysHelp:"Needed only for <b>live</b> mode. Permissions: <b>Spot trade + read balance</b>. <span style='color:var(--red);'>NEVER enable withdraw.</span>",
    configured:"configured", notSet:"— not set", replace:"Replace", add:"Add",
    promptKey:"API key:", promptSecret:"API secret:", promptPass:"API passphrase:",
    toastSaved:'Saved. Click "Restart bot" to apply.', toastFailed:"Save failed",
    toastBotPaused:"Bot paused", toastBotResumed:"Bot resumed",
    toastRestarting:"Bot restarting — scanner reconnects in ~60s",
    toastUpdateQueued:"Update queued — app restarts in 5s",
    toastNoUrl:"No download URL", toastUpdateFail:"Update failed",
    confirmRestart:"Restart bot? Takes ~60 seconds.",
    confirmUpdate:"Update to v{v}?\n\nThe app will close and restart.",
  },
  ru: {
    online:"ОНЛАЙН", offline:"ОФФЛАЙН", pause:"⏸ Пауза", resume:"▶ Запустить",
    updateAvailable:"🚀 Доступно обновление", later:"Позже", updateNow:"Обновить",
    tabDash:"Дашборд", tabTrades:"Сделки", tabSettings:"Настройки", tabKeys:"API ключи",
    pnlLive:"PnL (текущий запуск)", trades:"Сделок", rate:"Темп", avgTrade:"Средняя сделка",
    proj24:"Прогноз 24ч", latency:"Латенси", stops:"Стопы", openPos:"Открытые позиции",
    best:"лучшая", worst:"худшая", wins:"побед", elapsed:"прошло", perTrade:"на сделку",
    wait:"ждём", needMore:"нужно 30м + 10 сделок", extrapolated:"экстраполяция",
    oppAge:"возраст оппы", totalLoss:"общий убыток", activeInv:"активный инвентарь",
    topPairs:"Топ пар по PnL", symbol:"Символ", route:"Маршрут", pnl:"PnL", avg:"Средн",
    exchanges:"Биржи", hourlyPnl:"PnL по часам (24ч)", noData:"данных пока нет",
    noTrades:"Сделок пока нет", noTradesSub:"Запусти бота и подожди первую арб-возможность. Обычно 1-5 минут.",
    recentTrades:"Последние сделки (live)", botConfig:"Настройки бота",
    configHelp:"Изменения сохраняются в локальный .env. Перезапусти бота чтобы применить.",
    secLicense:"Лицензия и подключение", licenseKey:"Лицензионный ключ",
    licensePh:"вставь JWT из кабинета", licenseFrom:"Из", backendUrl:"URL бэкенда",
    secTrading:"Торговля", mode:"Режим",
    modePaper:"Paper (симуляция — без реальных денег)", modeLive:"Live (реальная торговля — нужны API ключи)",
    positionSize:"Размер позиции (USD)", minSpread:"Мин. чистый спред %",
    save:"Сохранить", restartBot:"↻ Перезапуск бота",
    exchangeKeys:"API ключи бирж",
    keysHelp:"Нужно только для <b>live</b> режима. Права: <b>Spot trade + чтение баланса</b>. <span style='color:var(--red);'>НИКОГДА не включай withdraw.</span>",
    configured:"настроено", notSet:"— не задано", replace:"Заменить", add:"Добавить",
    promptKey:"API ключ:", promptSecret:"API secret:", promptPass:"API passphrase:",
    toastSaved:'Сохранено. Жми "Перезапуск" чтобы применить.', toastFailed:"Ошибка сохранения",
    toastBotPaused:"Бот на паузе", toastBotResumed:"Бот возобновлён",
    toastRestarting:"Бот перезапускается — сканер подключится за ~60с",
    toastUpdateQueued:"Обновление готово — приложение перезапустится за 5с",
    toastNoUrl:"Нет URL загрузки", toastUpdateFail:"Ошибка обновления",
    confirmRestart:"Перезапустить бота? Займёт ~60 секунд.",
    confirmUpdate:"Обновить до v{v}?\n\nПриложение закроется и перезапустится.",
  },
  es: {
    online:"EN LÍNEA", offline:"OFFLINE", pause:"⏸ Pausar", resume:"▶ Reanudar",
    updateAvailable:"🚀 Actualización disponible", later:"Más tarde", updateNow:"Actualizar",
    tabDash:"Panel", tabTrades:"Trades", tabSettings:"Ajustes", tabKeys:"Claves API",
    pnlLive:"PnL (ejecución actual)", trades:"Trades", rate:"Tasa", avgTrade:"Trade medio",
    proj24:"Proyección 24h", latency:"Latencia", stops:"Stops", openPos:"Posiciones abiertas",
    best:"mejor", worst:"peor", wins:"ganadas", elapsed:"transcurrido", perTrade:"por trade",
    wait:"espera", needMore:"30m + 10 trades", extrapolated:"extrapolado",
    oppAge:"edad opp", totalLoss:"pérdida total", activeInv:"inventario activo",
    topPairs:"Top pares por PnL", symbol:"Símbolo", route:"Ruta", pnl:"PnL", avg:"Med",
    exchanges:"Exchanges", hourlyPnl:"PnL por hora (24h)", noData:"sin datos aún",
    noTrades:"Aún no hay trades", noTradesSub:"Inicia el bot y espera la primera oportunidad. Suele tardar 1-5 min.",
    recentTrades:"Trades recientes (live)", botConfig:"Configuración del bot",
    configHelp:"Los cambios se guardan en .env local. Reinicia el bot para aplicar.",
    secLicense:"Licencia y conexión", licenseKey:"Clave de licencia",
    licensePh:"pega JWT del panel", licenseFrom:"De", backendUrl:"URL del backend",
    secTrading:"Trading", mode:"Modo",
    modePaper:"Paper (simulación — sin dinero real)", modeLive:"Live (trading real — requiere claves API)",
    positionSize:"Tamaño de posición (USD)", minSpread:"Spread neto mínimo %",
    save:"Guardar", restartBot:"↻ Reiniciar bot",
    exchangeKeys:"Claves API de exchange",
    keysHelp:"Solo para modo <b>live</b>. Permisos: <b>Spot trade + lectura de balance</b>. <span style='color:var(--red);'>NUNCA habilites withdraw.</span>",
    configured:"configurado", notSet:"— sin configurar", replace:"Reemplazar", add:"Añadir",
    promptKey:"Clave API:", promptSecret:"API secret:", promptPass:"API passphrase:",
    toastSaved:'Guardado. Pulsa "Reiniciar bot" para aplicar.', toastFailed:"Error al guardar",
    toastBotPaused:"Bot pausado", toastBotResumed:"Bot reanudado",
    toastRestarting:"Bot reiniciando — scanner reconecta en ~60s",
    toastUpdateQueued:"Actualización lista — app reinicia en 5s",
    toastNoUrl:"Sin URL de descarga", toastUpdateFail:"Error de actualización",
    confirmRestart:"¿Reiniciar el bot? Tarda ~60 segundos.",
    confirmUpdate:"¿Actualizar a v{v}?\n\nLa app se cerrará y reiniciará.",
  },
  zh: {
    online:"在线", offline:"离线", pause:"⏸ 暂停", resume:"▶ 恢复",
    updateAvailable:"🚀 有可用更新", later:"稍后", updateNow:"立即更新",
    tabDash:"仪表盘", tabTrades:"交易", tabSettings:"设置", tabKeys:"API 密钥",
    pnlLive:"PnL（当前运行）", trades:"交易数", rate:"频率", avgTrade:"平均交易",
    proj24:"24小时预测", latency:"延迟", stops:"止损", openPos:"开仓",
    best:"最佳", worst:"最差", wins:"胜", elapsed:"已运行", perTrade:"每笔交易",
    wait:"等待", needMore:"需要 30分钟 + 10笔交易", extrapolated:"外推",
    oppAge:"机会年龄", totalLoss:"总亏损", activeInv:"活跃库存",
    topPairs:"按 PnL 排名的交易对", symbol:"符号", route:"路径", pnl:"PnL", avg:"平均",
    exchanges:"交易所", hourlyPnl:"每小时 PnL（24小时）", noData:"暂无数据",
    noTrades:"暂无交易", noTradesSub:"启动机器人，等待首次套利机会。通常需要 1-5 分钟。",
    recentTrades:"最近交易（实时）", botConfig:"机器人配置",
    configHelp:"更改保存到本地 .env。重启机器人以应用。",
    secLicense:"许可证和连接", licenseKey:"许可证密钥",
    licensePh:"从你的面板粘贴 JWT", licenseFrom:"来自", backendUrl:"后端 URL",
    secTrading:"交易", mode:"模式",
    modePaper:"Paper（模拟 — 无真实资金）", modeLive:"Live（真实交易 — 需要 API 密钥）",
    positionSize:"仓位大小 (USD)", minSpread:"最小净价差 %",
    save:"保存", restartBot:"↻ 重启机器人",
    exchangeKeys:"交易所 API 密钥",
    keysHelp:"仅 <b>live</b> 模式需要。权限：<b>现货交易 + 读取余额</b>。<span style='color:var(--red);'>绝不要启用提现。</span>",
    configured:"已配置", notSet:"— 未设置", replace:"替换", add:"添加",
    promptKey:"API 密钥:", promptSecret:"API secret:", promptPass:"API passphrase:",
    toastSaved:'已保存。点击"重启机器人"以应用。', toastFailed:"保存失败",
    toastBotPaused:"机器人已暂停", toastBotResumed:"机器人已恢复",
    toastRestarting:"机器人重启中 — 扫描器约 60 秒后重连",
    toastUpdateQueued:"更新已排队 — 应用将在 5 秒后重启",
    toastNoUrl:"无下载 URL", toastUpdateFail:"更新失败",
    confirmRestart:"重启机器人？需要约 60 秒。",
    confirmUpdate:"更新到 v{v}？\n\n应用将关闭并重启。",
  },
};
let LANG = localStorage.getItem("ec_lang") || (navigator.language || "en").slice(0,2);
if (!I18N[LANG]) LANG = "en";
const t = (k, vars) => {
  let s = (I18N[LANG] && I18N[LANG][k]) || I18N.en[k] || k;
  if (vars) for (const [n, v] of Object.entries(vars)) s = s.replaceAll("{"+n+"}", v);
  return s;
};
function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const k = el.dataset.i18n;
    if (k) el.textContent = t(k);
  });
  document.querySelectorAll('[data-i18n-html]').forEach(el => {
    el.innerHTML = t(el.dataset.i18nHtml);
  });
  document.querySelectorAll('[data-i18n-attr]').forEach(el => {
    const pairs = el.dataset.i18nAttr.split(',');
    for (const p of pairs) {
      const [attr, key] = p.split(':');
      if (attr && key) el.setAttribute(attr.trim(), t(key.trim()));
    }
  });
}
document.addEventListener('DOMContentLoaded', () => {
  const sw = document.getElementById('lang-sw');
  if (sw) {
    sw.value = LANG;
    sw.onchange = e => { LANG = e.target.value; localStorage.setItem("ec_lang", LANG); applyI18n(); loadDash(); };
  }
  applyI18n();
});

const fmt = (x, n=2) => x == null ? '—' : Number(x).toFixed(n);
const dollar = x => '$' + fmt(x, 2);
const pct = x => fmt(x, 2) + '%';
const cls = x => x > 0 ? 'pos' : x < 0 ? 'neg' : '';

// ─────────────── tabs ───────────────
for (const b of document.querySelectorAll('.tab-btn')) {
  b.onclick = () => {
    for (const x of document.querySelectorAll('.tab-btn')) x.classList.toggle('active', x === b);
    for (const p of document.querySelectorAll('.tab-pane')) p.classList.toggle('active', p.id === 'pane-' + b.dataset.tab);
    if (b.dataset.tab === 'settings') loadSettings();
    if (b.dataset.tab === 'keys') loadKeys();
  };
}

// ─────────────── toast ───────────────
function toast(msg, err=false) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.toggle('err', err);
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3400);
}

// ─────────────── status & version ───────────────
async function loadStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('ver').textContent = s.version;
    const dot = document.getElementById('status-dot');
    const txt = document.getElementById('status-text');
    if (s.scanner_alive) { dot.className = 'dot live'; txt.textContent = t('online'); }
    else { dot.className = 'dot off'; txt.textContent = t('offline'); }
    document.getElementById('status-mode').textContent = (s.mode || 'paper').toUpperCase();
    document.getElementById('status-lic').textContent = s.has_license ? '✓' : '—';
    const pb = document.getElementById('pause-btn');
    if (pb) {
      const key = s.paused ? 'resume' : 'pause';
      pb.setAttribute('data-i18n', key);
      pb.textContent = t(key);
    }
  } catch (e) {}
}

async function checkUpdate() {
  try {
    const r = await fetch('/api/version-check');
    const j = await r.json();
    if (j.update_available) {
      document.getElementById('update-banner').style.display = 'flex';
      document.getElementById('update-info').textContent = `v${j.current} → v${j.latest}`;
      window._updateInfo = j;
    }
  } catch (e) {}
}
function dismissUpdate() { document.getElementById('update-banner').style.display = 'none'; }
async function doUpdate() {
  if (!window._updateInfo?.download_url) { toast(t('toastNoUrl'), true); return; }
  if (!confirm(t('confirmUpdate', {v: window._updateInfo.latest}))) return;
  try {
    const r = await fetch('/api/control/update', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ download_url: window._updateInfo.download_url }),
    });
    const j = await r.json();
    if (!r.ok) return toast(j.error || t('toastUpdateFail'), true);
    toast(t('toastUpdateQueued'));
    setTimeout(() => window.close(), 4500);
  } catch (e) { toast(e.message, true); }
}

// ─────────────── dashboard ───────────────
async function loadDash() {
  const r = await fetch('/api/stats');
  const d = await r.json();
  const root = document.getElementById('dash-content');
  if (d.empty) {
    root.innerHTML = `<div class="card" style="text-align:center;"><h2>${t('noTrades')}</h2><div class="sub">${t('noTradesSub')}</div></div>`;
    return;
  }
  const s = d.summary;
  root.innerHTML = `
    <div class="grid">
      <div class="card"><h2>${t('pnlLive')}</h2>
        <div class="metric ${cls(s.pnl_usd)}">${dollar(s.pnl_usd)}</div>
        <div class="sub">${t('best')} ${dollar(s.best_trade)} · ${t('worst')} ${dollar(s.worst_trade)}</div>
      </div>
      <div class="card"><h2>${t('trades')}</h2>
        <div class="metric grad">${s.total_trades}</div>
        <div class="sub">${pct(s.win_rate_pct)} ${t('wins')} · ${s.wins} W</div>
      </div>
      <div class="card"><h2>${t('rate')}</h2>
        <div class="metric grad">${s.rate_per_hour == null ? '…' : fmt(s.rate_per_hour, 1) + '/h'}</div>
        <div class="sub">${fmt(s.elapsed_hours, 1)}h ${t('elapsed')}</div>
      </div>
      <div class="card"><h2>${t('avgTrade')}</h2>
        <div class="metric ${cls(s.avg_pnl_per_trade)}">${dollar(s.avg_pnl_per_trade)}</div>
        <div class="sub">${t('perTrade')}</div>
      </div>
      <div class="card"><h2>${t('proj24')}</h2>
        <div class="metric ${s.extrap_24h_pnl == null ? 'warn' : 'pos'}">${s.extrap_24h_pnl == null ? t('wait') : dollar(s.extrap_24h_pnl)}</div>
        <div class="sub">${s.extrap_24h_pnl == null ? t('needMore') : t('extrapolated')}</div>
      </div>
      <div class="card"><h2>${t('latency')}</h2>
        <div class="metric grad">${fmt(d.latency.avg_exec_ms, 0)}<span style="font-size:14px;color:var(--text-mute);"> ms</span></div>
        <div class="sub">${t('oppAge')} ${fmt(d.latency.avg_opp_age_ms, 0)}ms</div>
      </div>
      <div class="card"><h2>${t('stops')}</h2>
        <div class="metric ${d.stops.count > 0 ? 'warn' : ''}">${d.stops.count}</div>
        <div class="sub">${t('totalLoss')} ${dollar(d.stops.total_loss_usd)}</div>
      </div>
      <div class="card"><h2>${t('openPos')}</h2>
        <div class="metric grad">${d.inventory_count}</div>
        <div class="sub">${t('activeInv')}</div>
      </div>
    </div>

    <div class="grid" style="margin-top:14px; grid-template-columns: 2fr 1fr;">
      <div class="card">
        <h2>${t('topPairs')}</h2>
        <div class="table-wrap"><table>
          <tr><th>${t('symbol')}</th><th>${t('route')}</th><th class="num">${t('trades')}</th><th class="num">${t('pnl')}</th><th class="num">${t('avg')}</th></tr>
          ${d.top_pairs.map(p => `
            <tr><td><b>${p.symbol}</b></td>
                <td class="tag">${p.buy} → ${p.sell}</td>
                <td class="num">${p.count}</td>
                <td class="num ${cls(p.pnl)}">${dollar(p.pnl)}</td>
                <td class="num">${dollar(p.pnl/p.count)}</td></tr>
          `).join('')}
        </table></div>
      </div>
      <div class="card">
        <h2>${t('exchanges')}</h2>
        ${d.exchanges.map(e => {
          const maxP = Math.max(...d.exchanges.map(x => Math.abs(x.pnl)), 1);
          const w = Math.abs(e.pnl) / maxP * 100;
          return `<div class="bar-row">
            <span class="bar-label">${e.name}</span>
            <span class="bar-bg"><span class="bar-fill" style="width:${w}%;"></span></span>
            <span class="bar-val ${cls(e.pnl)}">${dollar(e.pnl)}</span>
          </div>`;
        }).join('')}
      </div>
    </div>

    <div class="card" style="margin-top:14px;">
      <h2>${t('hourlyPnl')}</h2>
      ${d.hourly.length === 0 ? `<div class="sub">${t('noData')}</div>` : ''}
      ${d.hourly.map(h => {
        const maxP = Math.max(...d.hourly.map(x => Math.abs(x.pnl)), 0.5);
        const w = Math.abs(h.pnl) / maxP * 100;
        return `<div class="bar-row">
          <span class="bar-label mono">${h.trades}t</span>
          <span class="bar-bg"><span class="bar-fill" style="width:${w}%;"></span></span>
          <span class="bar-val ${cls(h.pnl)}">${dollar(h.pnl)}</span>
        </div>`;
      }).join('')}
    </div>
  `;
  // also refresh trades feed
  document.getElementById('trade-feed').innerHTML = (d.recent || []).map(t => {
    const dt = new Date(t.ts * 1000);
    const time = dt.toLocaleTimeString();
    return `<div class="feed-row">
      <div><b>${t.symbol}</b> <span class="tag">${t.buy_ex}→${t.sell_ex}</span></div>
      <div class="feed-time">${time}</div>
      <div class="mono" style="text-align:right;color:var(--text-dim);">${pct(t.net_pct)}</div>
      <div class="feed-pnl ${cls(t.pnl)}">${dollar(t.pnl)}</div>
    </div>`;
  }).join('') || `<div class="sub">${t('noTrades')}</div>`;
}

// ─────────────── settings ───────────────
async function loadSettings() {
  try {
    const s = await (await fetch('/api/settings')).json();
    document.getElementById('s-license').value = s.license_key || '';
    document.getElementById('s-mode').value = s.mode || 'paper';
    document.getElementById('s-pos').value = s.position_usd;
    document.getElementById('s-net').value = s.min_net_pct;
    document.getElementById('s-backend').value = s.backend_url;
  } catch (e) { toast(t('toastFailed'), true); }
}
async function saveSettings() {
  const body = {
    license_key: document.getElementById('s-license').value,
    mode: document.getElementById('s-mode').value,
    position_usd: parseFloat(document.getElementById('s-pos').value),
    min_net_pct: parseFloat(document.getElementById('s-net').value),
    backend_url: document.getElementById('s-backend').value,
  };
  try {
    const r = await fetch('/api/settings', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) return toast(j.error || t('toastFailed'), true);
    toast(t('toastSaved'));
    loadStatus();
  } catch (e) { toast(e.message, true); }
}
async function restartBot() {
  if (!confirm(t('confirmRestart'))) return;
  try {
    await fetch('/api/control/restart', { method: 'POST' });
    toast(t('toastRestarting'));
  } catch (e) { toast(e.message, true); }
}
async function togglePause() {
  try {
    const r = await (await fetch('/api/control/pause', { method: 'POST' })).json();
    toast(r.paused ? t('toastBotPaused') : t('toastBotResumed'));
    loadStatus();
  } catch (e) { toast(e.message, true); }
}

// ─────────────── exchange keys ───────────────
async function loadKeys() {
  try {
    const list = await (await fetch('/api/exchange-keys')).json();
    document.getElementById('keys-list').innerHTML = list.map(k => `
      <div class="exchange-card">
        <div class="ex-header">
          <div>
            <b style="text-transform:uppercase;">${k.exchange}</b>
            ${k.configured ? `<span style="color:var(--green); margin-left:8px;">✓ ${t('configured')}</span> <span class="mono" style="color:var(--text-mute); font-size:11px;">${k.key_preview}</span>` : `<span style="color:var(--text-mute); margin-left:8px;">${t('notSet')}</span>`}
          </div>
          <button class="btn btn-sm btn-sec" onclick="editKey('${k.exchange}', ${k.needs_passphrase})">${k.configured ? t('replace') : t('add')}</button>
        </div>
      </div>
    `).join('');
  } catch (e) { toast('Failed to load keys', true); }
}
async function editKey(exchange, needsPass) {
  const api_key = prompt(`${exchange.toUpperCase()} — ${t('promptKey')}`);
  if (!api_key) return;
  const api_secret = prompt(`${exchange.toUpperCase()} — ${t('promptSecret')}`);
  if (!api_secret) return;
  let api_passphrase = null;
  if (needsPass) {
    api_passphrase = prompt(`${exchange.toUpperCase()} — ${t('promptPass')}`);
    if (!api_passphrase) return;
  }
  try {
    const r = await fetch('/api/exchange-keys', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ exchange, api_key, api_secret, api_passphrase }),
    });
    if (r.ok) { toast(`${exchange} saved`); loadKeys(); }
    else toast('Save failed', true);
  } catch (e) { toast(e.message, true); }
}

// ─────────────── boot ───────────────
loadStatus(); loadDash();
setInterval(loadStatus, 4000);
setInterval(loadDash, 5000);
checkUpdate();
setInterval(checkUpdate, 60 * 60 * 1000);   // every hour
</script>
</body>
</html>
"""


if __name__ == "__main__":
    log.info(f"Eye Crypt dashboard v{VERSION} → http://localhost:8765")
    # Bind to loopback only — the dashboard has no auth, so it must never be
    # reachable from the LAN. Local access only (http://localhost:8765).
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
