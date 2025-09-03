import sqlite3
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# ======================================================
# FastAPI App
# ======================================================
app = FastAPI()
templates = Jinja2Templates(directory="templates")

DB_PATH = "trades.db"
IST = timezone(timedelta(hours=5, minutes=30))

# ======================================================
# Helpers
# ======================================================
def utc_str_to_ist_str(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # Trades Table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT NOT NULL,
        event TEXT NOT NULL,
        symbol TEXT,
        side TEXT,
        tf TEXT,
        price REAL,
        sigH REAL,
        sigL REAL,
        sl REAL,
        t1 REAL,
        t2 REAL,
        t3 REAL,
        tag TEXT,
        status TEXT,
        entry REAL,
        exit_price REAL,
        pnl REAL,
        qty REAL,
        ts_entry_utc TEXT,
        t1_time_utc TEXT,
        t2_time_utc TEXT,
        t3_time_utc TEXT,
        sl_time_utc TEXT,
        raw TEXT
    );
    """)

    # Wallet Table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS wallet (
        id INTEGER PRIMARY KEY CHECK (id=1),
        balance REAL
    );
    """)
    conn.commit()

    # Seed wallet if not exists
    cur.execute("SELECT balance FROM wallet WHERE id=1;")
    if not cur.fetchone():
        cur.execute("INSERT INTO wallet (id, balance) VALUES (1, ?);", (1000000,))
    conn.commit()
    conn.close()

def exec_write_many(stmts, retries=5, backoff=0.2):
    for attempt in range(retries):
        try:
            conn = get_conn()
            cur = conn.cursor()
            for sql, params in stmts:
                cur.execute(sql, params)
            conn.commit()
            conn.close()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e) and attempt < retries - 1:
                time.sleep(backoff * (2**attempt))
            else:
                raise

def query(sql, params=()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows

# ======================================================
# Wallet Functions
# ======================================================
def get_wallet_balance():
    row = query("SELECT balance FROM wallet WHERE id=1;")
    return row[0]["balance"] if row else 0

def update_wallet(new_balance: float):
    exec_write_many([("UPDATE wallet SET balance=? WHERE id=1;", (new_balance,))])

# ======================================================
# Routes
# ======================================================
@app.get("/health")
def health():
    return {"status": "ok", "balance": get_wallet_balance()}

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    rows = query("SELECT * FROM trades ORDER BY id DESC LIMIT 500;")

    trades = []
    for r in rows:
        d = dict(r)
        for k in ("ts_entry_utc","t1_time_utc","t2_time_utc","t3_time_utc","sl_time_utc","ts_utc"):
            if k in d:
                d[k] = utc_str_to_ist_str(d[k])
        trades.append(d)

    context = {
        "request": request,
        "trades": trades,
        "wallet": {"balance": get_wallet_balance()}
    }
    return templates.TemplateResponse("dashboard.html", context)

@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    body = await request.body()
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        return JSONResponse({"status": "error", "msg": "invalid JSON"}, status_code=400)

    ts_utc = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()

    # Required fields
    event = data.get("event")
    side  = data.get("side")
    price = float(data.get("price")) if data.get("price") not in [None,"null"] else None
    symbol= data.get("symbol")
    tf    = data.get("tf")
    tag   = data.get("tag")

    sigH  = _safe_float(data.get("sigHigh"))
    sigL  = _safe_float(data.get("sigLow"))
    sl    = _safe_float(data.get("sl"))
    t1    = _safe_float(data.get("t1"))
    t2    = _safe_float(data.get("t2"))
    t3    = _safe_float(data.get("t3"))

    entry = None
    qty   = None
    exit_price = None
    pnl   = None
    status= None

    # Wallet logic
    balance = get_wallet_balance()
    if event == "ENTRY" and price:
        qty = (balance * 0.5) / price
        entry = price
        status = "OPEN"
        update_wallet(balance - (qty * price))  # Deduct

    elif event in ("TARGET1","TARGET2","TARGET3","STOPLOSS") and price:
        # Find last OPEN trade with same tag
        open_trades = query("SELECT * FROM trades WHERE tag=? AND status='OPEN' ORDER BY id DESC LIMIT 1;", (tag,))
        if open_trades:
            ot = dict(open_trades[0])
            qty = ot["qty"]
            entry = ot["entry"]
            exit_price = price
            pnl = (exit_price - entry) * qty if side == "BUY" else (entry - exit_price) * qty
            status = event
            update_wallet(balance + (qty * price) + (pnl or 0))

    raw = json.dumps(data)

    sql = """INSERT INTO trades(
        ts_utc, event, symbol, side, tf, price, sigH, sigL, sl, t1, t2, t3, tag,
        status, entry, exit_price, pnl, qty, ts_entry_utc, t1_time_utc, t2_time_utc, t3_time_utc, sl_time_utc, raw
    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);"""

    params = (
        ts_utc, event, symbol, side, tf, price, sigH, sigL, sl, t1, t2, t3, tag,
        status, entry, exit_price, pnl, qty,
        ts_utc if event=="ENTRY" else None,
        ts_utc if event=="TARGET1" else None,
        ts_utc if event=="TARGET2" else None,
        ts_utc if event=="TARGET3" else None,
        ts_utc if event=="STOPLOSS" else None,
        raw
    )

    exec_write_many([(sql, params)])
    return {"status": "ok"}

# ======================================================
# Utilities
# ======================================================
def _safe_float(v):
    try:
        return float(v)
    except Exception:
        return None

# ======================================================
# Init DB on startup
# ======================================================
init_db()
