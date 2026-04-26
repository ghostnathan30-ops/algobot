"""
AlgoBot -- Dashboard Server  (secured)
========================================
FastAPI backend with:
  - JWT authentication (httpOnly cookie, SameSite=Strict)
  - Security headers (CSP, X-Frame-Options, HSTS, etc.)
  - CORS locked to localhost only
  - 127.0.0.1 bind (set in start command)
  - Backtest trigger endpoint (run replay from browser)

Start:
    conda run -n algobot_env uvicorn dashboard.server:app --host 127.0.0.1 --port 8000
    Open: http://localhost:8000

First time setup (create login):
    conda run -n algobot_env python scripts/setup_dashboard_auth.py
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, Form, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.auth import (
    TOKEN_COOKIE, auth_configured, check_credentials,
    create_token, verify_token,
)
from dashboard.bot_state import (
    get_state, set_risk_mode, set_account_override,
    set_bot_running, compute_position_size, RISK_MODES,
    set_strategy_flags, get_strategy_flags,
    set_trading_mode, get_trading_mode,
)

CACHE_FILE = Path(__file__).parent / "cache" / "trades.json"
STATIC_DIR = Path(__file__).parent / "static"
DB_PATH    = PROJECT_ROOT / "data" / "trades.db"

app = FastAPI(title="AlgoBot Dashboard", docs_url=None, redoc_url=None)

# ── CORS: localhost only ───────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization", "Accept"],
)

# ── Security headers + auth middleware ────────────────────────────────────────

PUBLIC_PATHS = ("/login", "/auth/", "/static/", "/api/reload", "/ws/", "/api/webhook/", "/api/health/")


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    path = request.url.path

    # Auth check
    is_public = any(path.startswith(p) for p in PUBLIC_PATHS) or path == "/favicon.ico"
    if not is_public:
        token = request.cookies.get(TOKEN_COOKIE)
        user  = verify_token(token) if token else None
        if not user:
            if path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return RedirectResponse("/login", status_code=302)

    response = await call_next(request)

    # Security headers on every response
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.plot.ly https://fonts.googleapis.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )
    response.headers["Content-Security-Policy"]    = csp
    response.headers["X-Content-Type-Options"]     = "nosniff"
    response.headers["X-Frame-Options"]            = "DENY"
    response.headers["X-XSS-Protection"]           = "1; mode=block"
    response.headers["Referrer-Policy"]            = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]         = "geolocation=(), microphone=(), camera=()"
    response.headers["Cache-Control"]              = "no-store"
    return response


# ── Static files (login page assets) ─────────────────────────────────────────
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
async def login_page():
    f = STATIC_DIR / "login.html"
    return FileResponse(f) if f.exists() else Response("Login page not found", 404)


@app.post("/auth/login")
async def do_login(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
):
    if not auth_configured():
        return JSONResponse(
            {"error": "Auth not configured. Run setup_dashboard_auth.py first."},
            status_code=503,
        )
    if not check_credentials(username, password):
        return JSONResponse({"error": "Invalid username or password."}, status_code=401)

    token = create_token(username)
    resp  = JSONResponse({"ok": True})
    resp.set_cookie(
        key=TOKEN_COOKIE,
        value=token,
        httponly=True,
        samesite="strict",
        secure=False,    # set True when HTTPS is enabled
        max_age=36000,   # 10 hours
        path="/",
    )
    return resp


@app.post("/auth/logout")
async def do_logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(TOKEN_COOKIE, path="/")
    return resp


# ── Main page ─────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    f = STATIC_DIR / "index.html"
    if not f.exists():
        raise Exception("index.html not found")
    return FileResponse(f)


# ── Cache helpers ─────────────────────────────────────────────────────────────

_cache: dict = {}
_cache_lock  = threading.Lock()


def _load_cache() -> dict:
    global _cache
    with _cache_lock:
        if _cache:
            return _cache
        if not CACHE_FILE.exists():
            return {}
        _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return _cache


def _reload_cache():
    global _cache
    with _cache_lock:
        _cache = {}
    return _load_cache()


def _get_trades() -> list[dict]:
    return _load_cache().get("trades", [])


def _get_daily() -> list[dict]:
    return _load_cache().get("daily", [])


# ── Metrics ───────────────────────────────────────────────────────────────────

def _safe(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def compute_metrics(trades: list[dict], daily: list[dict]) -> dict:
    if not trades or not daily:
        return {}
    df   = pd.DataFrame(trades)
    n    = len(df)
    nw   = int((df["pnl_net"] > 0).sum())
    nl   = int((df["pnl_net"] < 0).sum())
    wr   = nw / n * 100 if n > 0 else 0
    gw   = float(df[df["pnl_net"] > 0]["pnl_net"].sum())
    gl   = float(abs(df[df["pnl_net"] < 0]["pnl_net"].sum()))
    pf   = gw / gl if gl > 0 else 999.0
    dp   = pd.DataFrame(daily).set_index("date")["pnl"].astype(float)
    nd   = len(dp)
    tot  = float(dp.sum())
    avg  = float(dp.mean())
    eq   = dp.cumsum()
    peak = eq.cummax()
    dd   = eq - peak
    mdd  = float(dd.min())
    mddp = float(mdd / peak.max() * 100) if peak.max() > 0 else 0
    base = 100_000.0
    dr   = dp / base
    dn   = dr[dr < 0]
    sh   = _safe(float(dr.mean() / dr.std() * np.sqrt(252))) if dr.std() > 0 else 0
    so   = _safe(float(dr.mean() / dn.std() * np.sqrt(252))) if len(dn) > 1 and dn.std() > 0 else 0
    ann  = tot * (252 / nd) if nd > 0 else 0
    cal  = _safe(float(ann / abs(mdd))) if mdd != 0 else 0
    aw   = float(df[df["pnl_net"] > 0]["pnl_net"].mean()) if nw > 0 else 0
    al   = float(df[df["pnl_net"] < 0]["pnl_net"].mean()) if nl > 0 else 0
    exp  = wr / 100 * aw + (1 - wr / 100) * al
    return {
        "total_pnl": round(tot, 2), "n_trades": n, "n_wins": nw, "n_losses": nl,
        "win_rate": round(wr, 1), "profit_factor": round(pf, 2),
        "gross_win": round(gw, 2), "gross_loss": round(gl, 2),
        "avg_win": round(aw, 2), "avg_loss": round(al, 2),
        "sharpe": round(sh or 0, 2), "sortino": round(so or 0, 2),
        "calmar": round(cal or 0, 2), "max_dd_usd": round(mdd, 2),
        "max_dd_pct": round(mddp, 1), "avg_daily_pnl": round(avg, 2),
        "ann_pnl": round(avg * 252, 2), "expectancy": round(exp, 2),
        "n_trading_days": nd, "best_trade": round(float(df["pnl_net"].max()), 2),
        "worst_trade": round(float(df["pnl_net"].min()), 2),
        "best_day": round(float(dp.max()), 2), "worst_day": round(float(dp.min()), 2),
    }


# ── TradingView webhook helpers ───────────────────────────────────────────────

_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

def _load_webhook_secret() -> str:
    """Load webhook_secret from config/config.yaml tv_paper section."""
    try:
        import yaml  # type: ignore
        cfg = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
        return str(cfg.get("tv_paper", {}).get("webhook_secret", ""))
    except Exception:
        return ""


# Rate limit: track per-IP request timestamps (last 60s window)
_webhook_rate: dict[str, list[float]] = defaultdict(list)
_WEBHOOK_RATE_MAX = 10   # max requests per minute per IP


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    data = _load_cache()
    return {
        "cache_ready": CACHE_FILE.exists(),
        "db_ready": DB_PATH.exists(),
        "server_time": datetime.now().isoformat(timespec="seconds"),
        "cache": {
            "generated_at": data.get("generated_at", ""),
            "period_start": data.get("period_start", ""),
            "period_end":   data.get("period_end", ""),
            "n_trades":     len(data.get("trades", [])),
            "n_days":       len(data.get("daily", [])),
        } if data else {},
    }


@app.get("/api/summary")
async def api_summary():
    t = _get_trades()
    d = _get_daily()
    if not t:
        return {"error": "No data. Run: python scripts/generate_dashboard_data.py"}
    return compute_metrics(t, d)


@app.get("/api/equity")
async def api_equity():
    daily = _get_daily()
    if not daily:
        return []
    ACCOUNT = 50_000.0
    dp    = pd.DataFrame(daily).sort_values("date")
    pnl   = dp["pnl"].astype(float)
    eq    = pnl.cumsum()                          # cumulative P&L from 0
    bal   = ACCOUNT + eq                          # account balance
    peak  = bal.cummax()
    dd    = (bal - peak) / peak * 100             # drawdown % relative to account
    dd    = dd.fillna(0)
    return [{"date": str(r["date"]),
             "equity":    round(float(eq.iloc[i]), 2),
             "balance":   round(float(bal.iloc[i]), 2),
             "dd_pct":    round(float(dd.iloc[i]), 2),
             "daily_pnl": round(float(r["pnl"]), 2)}
            for i, (_, r) in enumerate(dp.iterrows())]


@app.get("/api/monthly")
async def api_monthly():
    daily = _get_daily()
    if not daily:
        return {}
    dp = pd.DataFrame(daily)
    dp["date"]  = pd.to_datetime(dp["date"])
    dp["year"]  = dp["date"].dt.year
    dp["month"] = dp["date"].dt.month
    pivot  = dp.groupby(["year", "month"])["pnl"].sum().unstack(fill_value=None)
    years  = sorted(pivot.index.tolist())
    mnames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    matrix, text = [], []
    for y in years:
        rv, rt = [], []
        for m in range(1, 13):
            v = pivot.loc[y, m] if y in pivot.index and m in pivot.columns else None
            rv.append(round(float(v), 0) if v is not None and not math.isnan(float(v)) else None)
            rt.append(f"${float(v):+,.0f}" if v is not None and not math.isnan(float(v)) else "")
        matrix.append(rv); text.append(rt)
    return {"years": [str(y) for y in years], "months": mnames, "values": matrix, "text": text}


@app.get("/api/daily")
async def api_daily():
    return _get_daily()


@app.get("/api/by_strategy")
async def api_by_strategy():
    trades = _get_trades()
    if not trades:
        return {}
    df  = pd.DataFrame(trades)
    out = {}
    for s in df["strategy"].unique():
        sub = df[df["strategy"] == s]
        n   = len(sub)
        nw  = int((sub["pnl_net"] > 0).sum())
        gw  = float(sub[sub["pnl_net"] > 0]["pnl_net"].sum())
        gl  = float(abs(sub[sub["pnl_net"] < 0]["pnl_net"].sum()))
        out[s] = {"n_trades": n, "win_rate": round(nw/n*100,1) if n else 0,
                  "total_pnl": round(float(sub["pnl_net"].sum()),2),
                  "profit_factor": round(gw/gl,2) if gl>0 else 999.0,
                  "avg_pnl": round(float(sub["pnl_net"].mean()),2)}
    return out


@app.get("/api/by_market")
async def api_by_market():
    trades = _get_trades()
    if not trades:
        return {}
    df  = pd.DataFrame(trades)
    out = {}
    for m in df["market"].unique():
        sub = df[df["market"] == m]
        n   = len(sub)
        nw  = int((sub["pnl_net"] > 0).sum())
        gw  = float(sub[sub["pnl_net"] > 0]["pnl_net"].sum())
        gl  = float(abs(sub[sub["pnl_net"] < 0]["pnl_net"].sum()))
        out[m] = {"n_trades": n, "win_rate": round(nw/n*100,1) if n else 0,
                  "total_pnl": round(float(sub["pnl_net"].sum()),2),
                  "profit_factor": round(gw/gl,2) if gl>0 else 999.0,
                  "avg_pnl": round(float(sub["pnl_net"].mean()),2)}
    return out


@app.get("/api/trades")
async def api_trades(limit: int = 200, offset: int = 0,
                     strategy: str = "", market: str = "", direction: str = ""):
    tr = _get_trades()
    if strategy:  tr = [t for t in tr if t["strategy"]  == strategy.upper()]
    if market:    tr = [t for t in tr if t["market"]    == market.upper()]
    if direction: tr = [t for t in tr if t["direction"] == direction.upper()]
    tr = sorted(tr, key=lambda x: x["date"], reverse=True)
    return {"total": len(tr), "offset": offset, "limit": limit,
            "trades": tr[offset: offset + limit]}


@app.get("/api/distribution")
async def api_distribution():
    pnls = [t["pnl_net"] for t in _get_trades()]
    return {"all": sorted(pnls), "wins": sorted(p for p in pnls if p > 0),
            "losses": sorted(p for p in pnls if p < 0)}


# ── Backtest trigger ──────────────────────────────────────────────────────────
_backtest_running = False


@app.post("/api/run_backtest")
async def run_backtest():
    """Trigger data regeneration from the browser. Runs in background."""
    global _backtest_running
    if _backtest_running:
        return {"status": "running", "message": "Backtest already in progress."}
    _backtest_running = True

    async def _run():
        global _backtest_running
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "generate_dashboard_data.py"),
                cwd=str(PROJECT_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await proc.communicate()
            _reload_cache()
        finally:
            _backtest_running = False

    asyncio.create_task(_run())
    return {"status": "started", "message": "Backtest started. Refresh in ~60 seconds."}


@app.get("/api/backtest_status")
async def backtest_status():
    return {"running": _backtest_running}


@app.post("/api/reload")
async def reload_cache():
    """Bust the in-memory cache so the next request reads fresh data from disk.
    Called automatically by run_signal_replay.py after it writes new data."""
    _reload_cache()
    data = _load_cache()
    return {
        "ok": True,
        "n_trades": len(data.get("trades", [])),
        "generated_at": data.get("generated_at", ""),
    }


# ── /control → SPA (control is a tab in index.html) ──────────────────────────

@app.get("/control")
async def control_page():
    return RedirectResponse("/", status_code=302)


# ── Bot state API ─────────────────────────────────────────────────────────────

@app.get("/api/bot/status")
async def bot_status():
    """Return full bot state including risk mode config and daily P&L."""
    return get_state()


@app.post("/api/bot/mode")
async def set_mode(request: Request):
    body = await request.json()
    mode = body.get("mode", "")
    if mode not in RISK_MODES:
        return JSONResponse({"error": f"Invalid mode: {mode!r}"}, status_code=400)
    state = set_risk_mode(mode)
    return {"ok": True, "risk_mode": mode, "config": state["active_mode_config"]}


@app.post("/api/bot/account")
async def set_account(request: Request):
    body    = await request.json()
    balance = body.get("balance")          # None clears override
    if balance is not None:
        try:
            balance = float(balance)
            if balance < 1000:
                return JSONResponse({"error": "Balance must be at least $1,000"}, status_code=400)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Invalid balance value"}, status_code=400)
    state = set_account_override(balance)
    return {"ok": True, "account_override": state["account_override"]}


@app.post("/api/bot/start")
async def bot_start():
    state = set_bot_running(True)
    return {"ok": True, "bot_running": state["bot_running"]}


@app.post("/api/bot/stop")
async def bot_stop():
    state = set_bot_running(False)
    return {"ok": True, "bot_running": state["bot_running"]}


@app.get("/api/bot/flags")
async def get_flags():
    """Return current strategy plugin flag settings."""
    return get_strategy_flags()


@app.post("/api/bot/flags")
async def update_flags(request: Request):
    """
    Update one or more strategy plugin flags.
    Body: { "vix_filter": true, "econ_filter": false, ... }
    Only the supplied keys are updated; others remain unchanged.
    """
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "Expected JSON object of flag -> bool"}, status_code=400)
    state = set_strategy_flags(body)
    return {"ok": True, "strategy_flags": state["strategy_flags"]}


# ── Trading mode (ibkr ↔ tv_paper) ───────────────────────────────────────────

@app.post("/api/bot/set_mode")
async def set_trading_mode_endpoint(request: Request):
    """Switch between 'ibkr' (TWS) and 'tv_paper' (TradingView webhook paper trading)."""
    body = await request.json()
    mode = body.get("mode", "")
    if mode not in ("ibkr", "tv_paper"):
        return JSONResponse(
            {"error": f"Invalid trading mode {mode!r}. Choose 'ibkr' or 'tv_paper'."},
            status_code=400,
        )
    state = set_trading_mode(mode)
    return {"ok": True, "trading_mode": mode, "state": state}


# ── TradingView webhook receiver ──────────────────────────────────────────────

_REQUIRED_SIGNAL_FIELDS = {"market", "strategy", "direction", "entry", "stop", "target"}


@app.post("/api/webhook/signal")
async def webhook_signal(request: Request):
    """
    Receive a TradingView Pine Script JSON alert.

    Security:
      - Rate limited to 10 requests/minute per IP
      - Secret validated via hmac.compare_digest (constant-time)
      - Must be in tv_paper mode with bot_running=True

    Expected payload:
      { "secret": "...", "market": "ES", "strategy": "ORB",
        "direction": "LONG", "entry": 5820.25, "stop": 5802.00,
        "target": 5856.75, "size_mult": 1.0, "gls_score": 75,
        "htf_bias": "BULL", "risk_mode": "safe", "max_contracts": 1 }
    """
    # 1. Rate limiting (per client IP)
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = _webhook_rate[client_ip]
    _webhook_rate[client_ip] = [t for t in window if now - t < 60]
    if len(_webhook_rate[client_ip]) >= _WEBHOOK_RATE_MAX:
        return JSONResponse({"error": "rate_limit_exceeded"}, status_code=429)
    _webhook_rate[client_ip].append(now)

    # 2. Parse body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    # 3. Secret validation
    cfg_secret = _load_webhook_secret()
    incoming_secret = str(body.get("secret", ""))
    if not cfg_secret or not hmac.compare_digest(
        hashlib.sha256(incoming_secret.encode()).hexdigest(),
        hashlib.sha256(cfg_secret.encode()).hexdigest(),
    ):
        return JSONResponse({"error": "forbidden"}, status_code=403)

    # 4. Validate required fields
    missing = _REQUIRED_SIGNAL_FIELDS - body.keys()
    if missing:
        return JSONResponse(
            {"error": f"missing_fields: {sorted(missing)}"},
            status_code=422,
        )

    # 5. Check trading mode
    state = get_state()
    if state.get("trading_mode") != "tv_paper":
        return JSONResponse(
            {"error": "bot_not_in_tv_paper_mode"},
            status_code=409,
        )

    # 6. Check bot is running
    if not state.get("bot_running"):
        return JSONResponse({"error": "bot_not_running"}, status_code=409)

    # 7. Enqueue signal (file IPC — run_tv_paper_trading.py drains this)
    from dashboard.bot_state import STATE_FILE, _lock as _bs_lock
    import threading as _threading

    with _bs_lock:
        try:
            s = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            s = {}
        queue = s.get("pending_tv_signals", [])
        # Cap queue at 10 to prevent runaway accumulation
        if len(queue) >= 10:
            return JSONResponse({"error": "queue_full"}, status_code=503)
        # Strip secret before storing
        signal = {k: v for k, v in body.items() if k != "secret"}
        signal["queued_at"] = datetime.now().isoformat(timespec="seconds")
        queue.append(signal)
        s["pending_tv_signals"] = queue
        STATE_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")

    return {"ok": True, "queued": True, "queue_depth": len(queue)}


# ── IBKR live account balance ─────────────────────────────────────────────────

@app.get("/api/ibkr/balance")
async def ibkr_balance():
    """
    Attempt to pull live NetLiquidation from IBKR TWS (paper port 7497).
    Returns {balance, currency, connected} — never throws; returns connected=false on error.
    """
    try:
        from ib_insync import IB
        ib = IB()
        ib.connect("127.0.0.1", 7497, clientId=99, timeout=5)
        acct = ib.accountValues()
        net_liq = next(
            (float(a.value) for a in acct
             if a.tag == "NetLiquidation" and a.currency == "USD"),
            None,
        )
        currency = next(
            (a.currency for a in acct if a.tag == "NetLiquidation"),
            "USD",
        )
        ib.disconnect()
        if net_liq is not None:
            return {"connected": True, "balance": round(net_liq, 2), "currency": currency}
        return {"connected": True, "balance": None, "currency": "USD"}
    except Exception as exc:
        return {"connected": False, "balance": None, "error": str(exc)}


# ── Position size calculator ───────────────────────────────────────────────────

@app.get("/api/bot/position_calc")
async def position_calc(
    account: float = 150000.0,
    market: str    = "ES",
    atr: float     = 20.0,
):
    """
    Return suggested contract sizes for all three modes.
    Default ATR is a typical ES ATR in index points.
    Point values: ES=50, NQ=20, GC=100, CL=1000, ZB=1000.
    """
    POINT_VALUES = {"ES": 50.0, "NQ": 20.0, "GC": 100.0, "CL": 1000.0, "ZB": 1000.0, "6E": 125000.0}
    pv = POINT_VALUES.get(market.upper(), 50.0)
    sizes = compute_position_size(account, market.upper(), atr, pv)
    return {"account": account, "market": market.upper(), "atr": atr, "sizes": sizes}


# ── Extensive backtest trigger ────────────────────────────────────────────────

_extensive_running = False


@app.post("/api/run_extensive_backtest")
async def run_extensive_backtest():
    """Trigger the comprehensive backtest (walk-forward + Monte Carlo + sensitivity)."""
    global _extensive_running
    if _extensive_running:
        return {"status": "running", "message": "Backtest already in progress."}
    _extensive_running = True

    async def _run():
        global _extensive_running
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "run_comprehensive_backtest.py"),
                cwd=str(PROJECT_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
            )
            await proc.communicate()
            _reload_cache()
        finally:
            _extensive_running = False

    asyncio.create_task(_run())
    return {"status": "started", "message": "Comprehensive backtest started. Takes ~3–5 min. Dashboard will auto-update."}


@app.get("/api/extensive_backtest_status")
async def extensive_backtest_status():
    return {"running": _extensive_running}


# ── Bot process manager ────────────────────────────────────────────────────────
# Manages the paper trading loop as a real OS subprocess so the dashboard
# can actually start and stop the bot, not just flip a JSON flag.

_bot_proc: "subprocess.Popen | None" = None


@app.post("/api/bot/launch")
async def bot_launch():
    """
    Launch the trading loop as a background process.
    - trading_mode == 'ibkr'     → scripts/run_paper_trading.py  (requires TWS)
    - trading_mode == 'tv_paper' → scripts/run_tv_paper_trading.py (no TWS needed)
    Output is written to logs/bot_YYYY-MM-DD.log.
    Returns 409 if a managed process is already alive.
    """
    global _bot_proc
    if _bot_proc is not None and _bot_proc.poll() is None:
        return JSONResponse(
            {"ok": False, "error": f"Bot already running (PID {_bot_proc.pid}). Stop it first."},
            status_code=409,
        )

    trading_mode = get_trading_mode()
    if trading_mode == "tv_paper":
        script_name = "run_tv_paper_trading.py"
    else:
        script_name = "run_paper_trading.py"

    script_path = PROJECT_ROOT / "scripts" / script_name
    if not script_path.exists():
        return JSONResponse(
            {"ok": False, "error": f"Script not found: {script_name}"},
            status_code=500,
        )

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"bot_{datetime.now().strftime('%Y-%m-%d')}.log"
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    with open(log_path, "a", encoding="utf-8") as lf:
        _bot_proc = subprocess.Popen(
            [sys.executable, str(script_path)],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=lf,
            stderr=lf,
        )
    set_bot_running(True)
    return {
        "ok":           True,
        "pid":          _bot_proc.pid,
        "log":          log_path.name,
        "trading_mode": trading_mode,
        "script":       script_name,
    }


@app.post("/api/bot/kill")
async def bot_kill():
    """Terminate the managed paper trading process gracefully, then force-kill if needed."""
    global _bot_proc
    if _bot_proc is None or _bot_proc.poll() is not None:
        set_bot_running(False)
        return {"ok": True, "message": "No running process found. State reset to stopped."}
    _bot_proc.terminate()
    try:
        _bot_proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        _bot_proc.kill()
        _bot_proc.wait(timeout=3)
    set_bot_running(False)
    pid = _bot_proc.pid
    _bot_proc = None
    return {"ok": True, "message": f"Process {pid} terminated."}


@app.get("/api/bot/proc")
async def bot_proc_status():
    """Return liveness of the managed bot subprocess."""
    global _bot_proc
    if _bot_proc is None:
        return {"managed": False, "running": False, "pid": None}
    alive = _bot_proc.poll() is None
    if not alive:
        set_bot_running(False)   # sync state if process died unexpectedly
    return {"managed": True, "running": alive, "pid": _bot_proc.pid if alive else None}


# ── Script registry & terminal WebSocket ──────────────────────────────────────

# reloads        = True  → server reloads the trades.json cache on completion
# reloads_client = list  → client reloads these data sources and re-renders relevant tabs
#   values: "summary" | "strategies" | "validation" | "cl_trades" | "sc_results"
_SCRIPTS: dict[str, dict] = {
    "comprehensive": {
        "path":           "scripts/run_comprehensive_backtest.py",
        "label":          "Comprehensive Backtest",
        "desc":           "Walk-forward + Monte Carlo + Sensitivity — FHB (NQ/MNQ) + GC Fade (MGC) on all available data",
        "duration":       "~3–5 min",
        "reloads":        False,
        "reloads_client": ["comprehensive"],
        "result_tab":     "validation",
    },
    "sc_backtest": {
        "path":           "scripts/run_sc_backtest.py",
        "label":          "SC Real-Data OOS Check",
        "desc":           "FHB on Sierra Charts live futures (NQ, GC, MGC) — quick out-of-sample confirmation",
        "duration":       "~2 min",
        "reloads":        False,
        "reloads_client": ["sc_results"],
        "result_tab":     "esnq",
    },
    "fhb": {
        "path":           "scripts/run_fhb_backtest.py",
        "label":          "FHB Backtest (Yahoo Finance)",
        "desc":           "First Hour Breakout — NQ, full Yahoo Finance history (730d intraday)",
        "duration":       "~90s",
        "reloads":        False,
        "reloads_client": ["fhb_latest"],
        "result_tab":     "esnq",
    },
    "validation_suite": {
        "path":           "scripts/run_validation_suite.py",
        "label":          "Legacy Validation Suite",
        "desc":           "10-check validation suite (legacy — prefer Comprehensive Backtest above)",
        "duration":       "~3min",
        "reloads":        False,
        "reloads_client": ["validation"],
        "result_tab":     "validation",
    },
    "dashboard_data": {
        "path":           "scripts/generate_dashboard_data.py",
        "label":          "Refresh Dashboard Data",
        "desc":           "Regenerate trade cache for Overview tab charts",
        "duration":       "~90s",
        "reloads":        True,
        "reloads_client": ["summary", "strategies"],
        "result_tab":     "overview",
    },
    "replay": {
        "path":           "scripts/run_signal_replay.py",
        "label":          "Signal Replay",
        "desc":           "Replay last 60 days of FHB signals, update dashboard cache",
        "duration":       "~120s",
        "reloads":        True,
        "reloads_client": ["summary", "strategies"],
        "result_tab":     "overview",
    },
    "backup": {
        "path":           "scripts/create_backup.py",
        "label":          "Create Backup",
        "desc":           "Encrypt and backup project files",
        "duration":       "~10s",
        "reloads":        False,
        "reloads_client": [],
        "result_tab":     None,
    },
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHFABCDJn]")

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@app.get("/api/scripts")
async def list_scripts():
    """Return available scripts with metadata for the terminal UI."""
    result = {}
    for key, meta in _SCRIPTS.items():
        p = PROJECT_ROOT / meta["path"]
        if p.exists():
            result[key] = {k: v for k, v in meta.items() if k != "path"}
    return result


@app.websocket("/ws/terminal")
async def terminal_ws(websocket: WebSocket):
    """
    WebSocket: stream live script output to the browser terminal.
    Client sends:  {"script": "fhb"}
    Server sends:  {"type": "start"|"line"|"done"|"error", ...}
    """
    # Auth via cookie (WebSocket handshake carries cookies)
    token = websocket.cookies.get(TOKEN_COOKIE)
    user  = verify_token(token) if token else None
    if not user:
        await websocket.close(code=4001)
        return

    await websocket.accept()
    proc = None
    try:
        data       = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        script_key = data.get("script", "")

        if script_key not in _SCRIPTS:
            await websocket.send_json({"type": "error", "text": f"Unknown script: {script_key!r}"})
            return

        meta        = _SCRIPTS[script_key]
        script_path = PROJECT_ROOT / meta["path"]
        if not script_path.exists():
            await websocket.send_json({"type": "error", "text": f"Script not found: {script_path.name}"})
            return

        await websocket.send_json({
            "type":     "start",
            "script":   script_key,
            "label":    meta["label"],
            "duration": meta["duration"],
        })

        env  = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script_path),
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )

        async for raw_line in proc.stdout:
            text = _strip_ansi(raw_line.decode("utf-8", errors="replace").rstrip("\n\r"))
            if text:
                await websocket.send_json({"type": "line", "text": text})

        exit_code = await proc.wait()

        if meta.get("reloads"):
            _reload_cache()

        await websocket.send_json({
            "type":           "done",
            "exit_code":      exit_code,
            "script":         script_key,
            "label":          meta["label"],
            "reloads_client": meta.get("reloads_client", []),
            "result_tab":     meta.get("result_tab"),
        })

    except asyncio.TimeoutError:
        try:
            await websocket.send_json({"type": "error", "text": "Timed out waiting for script selection."})
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "text": str(exc)})
        except Exception:
            pass
    finally:
        if proc and proc.returncode is None:
            try:
                proc.terminate()
            except Exception:
                pass


# ── Sierra Charts backtest results ────────────────────────────────────────────

SC_LATEST       = PROJECT_ROOT / "reports" / "backtests" / "sc_backtest_latest.json"
BACKTEST_DIR    = PROJECT_ROOT / "reports" / "backtests"
CL_CSV          = BACKTEST_DIR / "cl_fhb_trades.csv"


@app.get("/api/validation/latest")
async def validation_latest():
    """Return the most recent validation suite results (10-check per-strategy report)."""
    jsons = sorted(BACKTEST_DIR.glob("validation_*.json"))
    if not jsons:
        return JSONResponse(
            {"error": "No validation results found. Run the Validation Suite from Terminal."},
            status_code=404,
        )
    try:
        data = json.loads(jsons[-1].read_text(encoding="utf-8"))
        return data
    except Exception as e:
        return JSONResponse({"error": f"Failed to read validation results: {e}"}, status_code=500)


@app.get("/api/cl/trades")
async def cl_trades_api():
    """Return CL crude oil trades from the latest backtest CSV."""
    if not CL_CSV.exists():
        return JSONResponse(
            {"error": "No CL trades found. Run CL Oil Backtest from Terminal."},
            status_code=404,
        )
    try:
        df = pd.read_csv(CL_CSV)
        return df.fillna("").to_dict(orient="records")
    except Exception as e:
        return JSONResponse({"error": f"Failed to read CL trades: {e}"}, status_code=500)


@app.get("/api/cl/backtest_latest")
async def cl_backtest_latest():
    """Return aggregate metrics from the most recent CL oil backtest JSON."""
    p = BACKTEST_DIR / "cl_backtest_latest.json"
    if not p.exists():
        return JSONResponse(
            {"error": "No CL backtest results. Run CL Oil Backtest from Terminal."},
            status_code=404,
        )
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": f"Failed to read CL backtest: {e}"}, status_code=500)


_validation_running = False


@app.get("/api/validation_status")
async def validation_status():
    return {"running": _validation_running}


@app.get("/api/sc/results")
async def sc_results():
    """
    Return SC backtest results as a flat ``results`` array.
    Transforms the grouped fhb_by_market / orb_by_market dicts into the
    flat list the dashboard JS expects: [{market, strategy, ...metrics}].
    """
    if not SC_LATEST.exists():
        return JSONResponse(
            {"error": "No Sierra Charts backtest results found. Run 'SC Real-Data Backtest' from the terminal."},
            status_code=404,
        )
    try:
        payload = json.loads(SC_LATEST.read_text(encoding="utf-8"))
        results = []
        for strategy_key, by_market in [
            ("FHB", payload.get("fhb_by_market", {})),
            ("ORB", payload.get("orb_by_market", {})),
        ]:
            if isinstance(by_market, dict):
                for market, metrics in by_market.items():
                    if isinstance(metrics, dict) and metrics.get("total_trades", 0) > 0:
                        results.append({"market": market, "strategy": strategy_key, **metrics})
        return {
            "results":      results,
            "generated_at": payload.get("generated_at"),
            "data_source":  payload.get("data_source", "Sierra Charts"),
        }
    except Exception as e:
        return JSONResponse({"error": f"Failed to read SC results: {e}"}, status_code=500)


# ── Comprehensive backtest results ─────────────────────────────────────────────

COMPREHENSIVE_FILE = BACKTEST_DIR / "comprehensive_latest.json"


@app.get("/api/comprehensive/latest")
async def comprehensive_latest():
    """Return the most recent comprehensive backtest results (walk-forward, MC, sensitivity)."""
    if not COMPREHENSIVE_FILE.exists():
        return JSONResponse(
            {"error": "No comprehensive results. Run 'Comprehensive Backtest' from Terminal."},
            status_code=404,
        )
    try:
        return json.loads(COMPREHENSIVE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": f"Failed to read comprehensive results: {e}"}, status_code=500)


# ── FHB / ORB latest backtest results ─────────────────────────────────────────

def _csv_metrics(df: pd.DataFrame, pnl_col: str = "pnl_net") -> dict:
    """Compute summary metrics from a trade dataframe."""
    if df.empty:
        return {}
    pnl  = df[pnl_col].astype(float)
    n    = len(pnl)
    wins = pnl[pnl > 0]
    loss = pnl[pnl < 0]
    nw, nl = len(wins), len(loss)
    gw = float(wins.sum()) if nw else 0.0
    gl = float(abs(loss.sum())) if nl else 0.0
    return {
        "n_trades":     n,
        "n_wins":       nw,
        "n_losses":     nl,
        "win_rate":     round(nw / n * 100, 1) if n else 0.0,
        "profit_factor": round(gw / gl, 2) if gl > 0 else 999.0,
        "total_pnl":    round(float(pnl.sum()), 2),
        "avg_win":      round(float(wins.mean()), 2) if nw else 0.0,
        "avg_loss":     round(float(loss.mean()), 2) if nl else 0.0,
        "best_trade":   round(float(pnl.max()), 2),
        "worst_trade":  round(float(pnl.min()), 2),
    }


def _latest_csv_response(glob_pattern: str, label: str):
    csvs = sorted(BACKTEST_DIR.glob(glob_pattern))
    if not csvs:
        return JSONResponse(
            {"error": f"No {label} results found. Run {label} Backtest from Terminal."},
            status_code=404,
        )
    try:
        df  = pd.read_csv(csvs[-1])
        out = {
            "file":      csvs[-1].name,
            "n_files":   len(csvs),
            "metrics":   _csv_metrics(df),
            "by_market": {},
        }
        if "market" in df.columns:
            for mkt in sorted(df["market"].unique()):
                out["by_market"][str(mkt)] = _csv_metrics(df[df["market"] == mkt])
        return out
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/fhb/latest")
async def fhb_latest():
    """Return summary stats from the most recent FHB backtest CSV."""
    return _latest_csv_response("fhb_5d_improved_*.csv", "FHB")


@app.get("/api/orb/latest")
async def orb_latest():
    """Return summary stats from the most recent ORB backtest CSV."""
    return _latest_csv_response("orb_backtest_*.csv", "ORB")


# ── Live trading endpoints ─────────────────────────────────────────────────────

@app.get("/api/live/trades")
async def live_trades(token: str = None, request: Request = None):
    """
    Return today's closed trades from the live TradeDB (SQLite).
    Populated in real time as IBKR fills arrive during paper trading.
    """
    if not DB_PATH.exists():
        return {"trades": [], "summary": None, "message": "No trade database found. Start paper trading first."}
    try:
        import sqlite3
        from datetime import date as _date
        today = str(_date.today())
        conn  = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur   = conn.execute(
            "SELECT * FROM trades WHERE DATE(exit_time) = ? ORDER BY exit_time DESC",
            (today,)
        )
        rows  = [dict(r) for r in cur.fetchall()]
        conn.close()

        wins   = sum(1 for r in rows if float(r.get("pnl_net", 0) or 0) > 0)
        losses = sum(1 for r in rows if float(r.get("pnl_net", 0) or 0) < 0)
        total_pnl = sum(float(r.get("pnl_net", 0) or 0) for r in rows)
        gw  = sum(float(r.get("pnl_net", 0) or 0) for r in rows if float(r.get("pnl_net", 0) or 0) > 0)
        gl  = abs(sum(float(r.get("pnl_net", 0) or 0) for r in rows if float(r.get("pnl_net", 0) or 0) < 0))
        pf  = round(gw / gl, 2) if gl > 0 else None

        return {
            "date":   today,
            "trades": rows,
            "summary": {
                "total":     len(rows),
                "wins":      wins,
                "losses":    losses,
                "total_pnl": round(total_pnl, 2),
                "profit_factor": pf,
            }
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/live/positions")
async def live_positions():
    """
    Return current open positions from bot_state.json plus account state
    (trailing DD, consecutive losses, size multiplier) from account_state.json.
    Updated every ~5 seconds by the paper trading loop.
    """
    try:
        state = get_state()
        positions = state.get("open_positions", [])

        # Read persistent account state (trailing DD, consecutive losses)
        acct = {}
        acct_path = PROJECT_ROOT / "data" / "account_state.json"
        if acct_path.exists():
            try:
                acct = json.loads(acct_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        peak    = float(acct.get("peak_balance",    50_000.0))
        start   = float(acct.get("starting_balance", 50_000.0))
        cum_pnl = float(acct.get("cumulative_pnl",   0.0))
        current = start + cum_pnl
        dd_used = max(peak - current, 0.0)
        streak  = int(acct.get("consecutive_losses", 0))
        if streak >= 3:
            size_mult = 0.25
        elif streak == 2:
            size_mult = 0.50
        else:
            size_mult = 1.0

        return {
            "positions":         positions,
            "count":             len(positions),
            "bot_running":       state.get("bot_running", False),
            "daily_pnl":         state.get("daily_pnl", 0.0),
            "daily_trades":      state.get("daily_trades", 0),
            "daily_wins":        state.get("daily_wins", 0),
            "daily_losses":      state.get("daily_losses", 0),
            "risk_mode":         state.get("risk_mode", "safe"),
            "last_updated":      state.get("last_updated", ""),
            "account_state": {
                "peak_balance":      round(peak, 2),
                "current_balance":   round(current, 2),
                "cumulative_pnl":    round(cum_pnl, 2),
                "trailing_dd_used":  round(dd_used, 2),
                "trailing_dd_limit": 1_800.0,
                "consecutive_losses": streak,
                "size_mult":          size_mult,
            },
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/account/state")
async def account_state_endpoint():
    """Return persistent account state (trailing DD, consecutive losses, size mult)."""
    acct_path = PROJECT_ROOT / "data" / "account_state.json"
    if not acct_path.exists():
        return {
            "peak_balance": 50_000.0, "current_balance": 50_000.0,
            "cumulative_pnl": 0.0, "trailing_dd_used": 0.0,
            "trailing_dd_limit": 1_800.0, "consecutive_losses": 0, "size_mult": 1.0,
        }
    try:
        acct    = json.loads(acct_path.read_text(encoding="utf-8"))
        peak    = float(acct.get("peak_balance",    50_000.0))
        start   = float(acct.get("starting_balance", 50_000.0))
        cum_pnl = float(acct.get("cumulative_pnl",   0.0))
        current = start + cum_pnl
        dd_used = max(peak - current, 0.0)
        streak  = int(acct.get("consecutive_losses", 0))
        size_mult = 0.25 if streak >= 3 else (0.50 if streak == 2 else 1.0)
        return {
            "peak_balance":       round(peak, 2),
            "current_balance":    round(current, 2),
            "cumulative_pnl":     round(cum_pnl, 2),
            "trailing_dd_used":   round(dd_used, 2),
            "trailing_dd_limit":  1_800.0,
            "consecutive_losses": streak,
            "size_mult":          size_mult,
            "last_updated":       acct.get("last_updated", ""),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── System health ──────────────────────────────────────────────────────────────

@app.get("/api/system/health")
async def system_health():
    """Fast system health check: TWS socket probe (ibkr mode only), cache, DB, bot state."""
    import socket as _sock

    state        = get_state()
    trading_mode = state.get("trading_mode", "ibkr")

    # TWS port probe — skip entirely in tv_paper mode (no TWS required)
    if trading_mode == "tv_paper":
        tws_info = {"connected": None, "port": 7497, "skipped": True,
                    "reason": "tv_paper mode — TWS not required"}
    else:
        tws_ok = False
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(0.8)
            tws_ok = s.connect_ex(("127.0.0.1", 7497)) == 0
            s.close()
        except Exception:
            pass
        tws_info = {"connected": tws_ok, "port": 7497, "skipped": False}

    data      = _load_cache()
    cache_ok  = CACHE_FILE.exists()
    n_trades  = len(data.get("trades", []))
    gen_at    = data.get("generated_at")

    return {
        "tws": tws_info,
        "cache": {
            "ready":        cache_ok,
            "n_trades":     n_trades,
            "generated_at": gen_at,
        },
        "database": {
            "ready": DB_PATH.exists(),
            "path":  str(DB_PATH.name),
        },
        "bot": {
            "running":      state.get("bot_running", False),
            "risk_mode":    state.get("risk_mode", "safe"),
            "paper_mode":   state.get("paper_mode", True),
            "daily_pnl":    state.get("daily_pnl", 0.0),
            "trading_mode": trading_mode,
        },
        "backtest_running": _backtest_running or _extensive_running,
        "server_time": datetime.now().isoformat(timespec="seconds"),
    }


# ── Monitor / bot process health ──────────────────────────────────────────────

@app.get("/api/health/monitor")
async def monitor_health():
    """
    Lightweight health check for the paper trading monitor loop.
    Returns liveness signal based on bot_state.json recency and open positions.
    Safe to poll frequently (reads only from the JSON state file — no DB hit).
    """
    state       = get_state()
    bot_running = state.get("bot_running", False)
    last_updated_raw = state.get("last_updated", "")
    open_positions   = state.get("open_positions", [])
    pending_signals  = len(state.get("pending_tv_signals", []))
    trading_mode     = state.get("trading_mode", "ibkr")

    # Compute staleness of last_updated timestamp
    staleness_s: int | None = None
    if last_updated_raw:
        try:
            last_dt = datetime.fromisoformat(last_updated_raw)
            staleness_s = int((datetime.now() - last_dt).total_seconds())
        except Exception:
            pass

    # Heuristic: monitor is "healthy" if bot claims to be running and
    # bot_state was updated within the last 2 minutes (two loop cycles).
    monitor_healthy = (
        bot_running
        and trading_mode == "tv_paper"
        and staleness_s is not None
        and staleness_s < 120
    )

    # Check webhook log for most recent received signal
    webhook_log = PROJECT_ROOT / "logs" / "webhook_signals.jsonl"
    last_signal_at: str | None = None
    if webhook_log.exists():
        try:
            lines = webhook_log.read_text(encoding="utf-8").splitlines()
            if lines:
                last_entry = json.loads(lines[-1])
                last_signal_at = last_entry.get("drained_at")
        except Exception:
            pass

    return {
        "monitor": {
            "healthy":       monitor_healthy,
            "bot_running":   bot_running,
            "trading_mode":  trading_mode,
            "staleness_s":   staleness_s,
            "stale_threshold_s": 120,
        },
        "positions": {
            "open_count":    len(open_positions),
            "open":          open_positions,
        },
        "queue": {
            "pending_signals": pending_signals,
            "last_signal_at":  last_signal_at,
        },
        "last_updated": last_updated_raw,
        "server_time":  datetime.now().isoformat(timespec="seconds"),
    }

