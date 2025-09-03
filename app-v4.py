import os
import json
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────
APP_TZ = ZoneInfo("Asia/Kolkata")
SECRET = os.getenv("TV_TEST_SECRET", "MY_ULTRA_SECRET")
DB_PATH = os.getenv("TV_DB_PATH", "trades.db")
START_BALANCE = float(os.getenv("TV_START_BALANCE", "1000000"))  # ₹10L
ALLOCATION_PCT = float(os.getenv("TV_ALLOC_PCT", "0.5"))         # 50%

app = FastAPI()
env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html", "xml"])
)

# ─────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # events: raw webhook stream
    cur.execute("""
    CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT NOT NULL,
        event TEXT NOT NULL,
        symbol TEXT,
        side TEXT,
        tf TEXT,
        price REAL,
        tag TEXT,
        raw TEXT
    );
    """)

    # trades: consolidated by tag (one row per trade lifecycle)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tag TEXT UNIQUE
    );
    """)

    # wallet: single row
    cur.execute("""
    CREATE TABLE IF NOT EXISTS wallet(
        id INTEGER PRIMARY KEY CHECK (id = 1),
        balance REAL NOT NULL,
        updated_utc TEXT NOT NULL
    );
    """)

    # ledger: cash movements
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ledger(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc TEXT NOT NULL,
        type TEXT NOT NULL,
        trade_tag TEXT,
        side TEXT,
        qty REAL,
        price REAL,
        amount REAL NOT NULL,
        balance_after REAL NOT NULL
    );
    """)

    # seed wallet
    cur.execute("SELECT COUNT(1) FROM wallet;")
    if cur.fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        cur.execute("INSERT INTO wallet(id, balance, updated_utc) VALUES (1, ?, ?);",
                    (START_BALANCE, now))
        cur.execute("""INSERT INTO ledger(ts_utc, type, trade_tag, side, qty, price, amount, balance_after)
                       VALUES (?, 'RESET', NULL, NULL, NULL, NULL, ?, ?);""",
                    (now, START_BALANCE, START_BALANCE))

    conn.commit()
    conn.close()

def ensure_columns(conn, table, spec: dict):
    cur = conn.cursor()
    existing = {row["name"] for row in cur.execute(f"PRAGMA table_info({table});")}
    for col, ddl in spec.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl};")
    conn.commit()

def migrate_schema():
    conn = get_conn()
    # trades expected columns
    ensure_columns(conn, "trades", {
        "ts_entry_utc": "TEXT",
        "symbol": "TEXT",
        "tf": "TEXT",
        "side": "TEXT",
        "candle": "TEXT",
        "entry": "REAL",
        "sig_high": "REAL",
        "sig_low": "REAL",
        "long_t1": "REAL",
        "long_t2": "REAL",
        "long_sl": "REAL",
        "short_t1": "REAL",
        "short_t2": "REAL",
        "short_sl": "REAL",
        "t1_price": "REAL",
        "t1_time_utc": "TEXT",
        "t2_price": "REAL",
        "t2_time_utc": "TEXT",
        "sl_price": "REAL",
        "sl_time_utc": "TEXT",
        "status": "TEXT",
        "qty": "REAL",
        "spent": "REAL",
        "realized_pnl": "REAL DEFAULT 0"
    })
    # events already OK; wallet/ledger created in init_db
    conn.close()

def col(row, name, default=None):
    try:
        return row[name]
    except Exception:
        return default

init_db()
migrate_schema()

# ─────────────────────────────────────────────────────────────────────
# Wallet ops
# ─────────────────────────────────────────────────────────────────────
def wallet_balance(conn) -> float:
    r = conn.execute("SELECT balance FROM wallet WHERE id=1").fetchone()
    return r["balance"] if r else 0.0

def wallet_apply(conn, delta: float, ltype: str, tag: str|None,
                 side: str|None, qty: float|None, price: float|None) -> float:
    cur = conn.cursor()
    bal = wallet_balance(conn)
    new_bal = bal + float(delta)
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("UPDATE wallet SET balance=?, updated_utc=? WHERE id=1;", (new_bal, now))
    cur.execute("""INSERT INTO ledger(ts_utc, type, trade_tag, side, qty, price, amount, balance_after)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?);""",
                (now, ltype, tag, side, qty, price, delta, new_bal))
    return new_bal

def allocate_for_entry(conn, price: float) -> tuple[float, float]:
    bal = wallet_balance(conn)
    alloc_cash = bal * ALLOCATION_PCT
    if price <= 0 or alloc_cash <= 0:
        return (0.0, 0.0)
    qty = alloc_cash / price
    spend = qty * price
    return (qty, spend)

# ─────────────────────────────────────────────────────────────────────
# Utils
# ─────────────────────────────────────────────────────────────────────
def ist(ts_utc: str|None) -> str:
    if not ts_utc:
        return ""
    try:
        return datetime.fromisoformat(ts_utc).astimezone(APP_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""

def ok():
    return JSONResponse({"ok": True})

# ─────────────────────────────────────────────────────────────────────
# Webhook
# ─────────────────────────────────────────────────────────────────────
@app.post("/tv-webhook")
async def tv_webhook(request: Request, user_agent: str = Header(None)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad json")

    if str(body.get("secret", "")) != SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    event  = str(body.get("event", "")).upper().strip()
    side   = str(body.get("side", "")).upper().strip()        # BUY/SELL or ""
    symbol = str(body.get("symbol", ""))
    tf     = str(body.get("tf", ""))
    price  = float(body.get("price", 0) or 0)
    tag    = str(body.get("tag", ""))

    sigH   = body.get("sigHigh", None)
    sigL   = body.get("sigLow", None)
    t1     = body.get("t1", None)
    t2     = body.get("t2", None)
    sl     = body.get("sl", None)
    candle = body.get("candle", "") or ("White (BUY)" if side=="BUY" else "Yellow (SELL)" if side=="SELL" else "")

    now_utc = datetime.now(timezone.utc).isoformat()
    conn = get_conn()
    cur = conn.cursor()

    # Always log event
    cur.execute("""INSERT INTO events(ts_utc, event, symbol, side, tf, price, tag, raw)
                   VALUES (?,?,?,?,?,?,?,?);""",
                (now_utc, event, symbol, side, tf, price, tag, json.dumps(body)))

    # ENTRY → create/update trade + cash allocation
    if event == "ENTRY":
        qty, spend = allocate_for_entry(conn, price)
        # Deduct cash for both BUY/SELL (simple sim)
        if qty > 0 and spend > 0:
            wallet_apply(conn, -spend, f"ENTRY_{side or 'NA'}", tag, side or None, qty, price)

        # Upsert trade
        cur.execute("SELECT * FROM trades WHERE tag=?;", (tag,))
        exists = cur.fetchone() is not None

        cols = dict(
            tag=tag,
            ts_entry_utc=now_utc,
            symbol=symbol,
            tf=tf,
            side=side or None,
            candle=candle,
            entry=price if price else None,
            sig_high=sigH,
            sig_low=sigL,
            long_t1=t1 if (side=="BUY") else None,
            long_t2=t2 if (side=="BUY") else None,
            long_sl=sl if (side=="BUY") else None,
            short_t1=t1 if (side=="SELL") else None,
            short_t2=t2 if (side=="SELL") else None,
            short_sl=sl if (side=="SELL") else None,
            status="OPEN",
            qty=qty if qty else None,
            spent=spend if spend else None,
            realized_pnl=0.0
        )

        if exists:
            placeholders = ",".join([f"{k}=:{k}" for k in cols.keys() if k != "tag"])
            cur.execute(f"UPDATE trades SET {placeholders} WHERE tag=:tag;", cols)
        else:
            cur.execute("""
                INSERT INTO trades(tag, ts_entry_utc, symbol, tf, side, candle, entry, sig_high, sig_low,
                                   long_t1, long_t2, long_sl, short_t1, short_t2, short_sl,
                                   status, qty, spent, realized_pnl)
                VALUES (:tag, :ts_entry_utc, :symbol, :tf, :side, :candle, :entry, :sig_high, :sig_low,
                        :long_t1, :long_t2, :long_sl, :short_t1, :short_t2, :short_sl,
                        :status, :qty, :spent, :realized_pnl);
            """, cols)

        conn.commit(); conn.close()
        return ok()

    # For non-entry events we need an existing trade
    cur.execute("SELECT * FROM trades WHERE tag=?;", (tag,))
    trade = cur.fetchone()
    if not trade:
        conn.commit(); conn.close()
        return JSONResponse({"ok": False, "msg": "unknown tag"}, status_code=200)

    qty_total = col(trade, "qty", 0.0) or 0.0
    entry     = col(trade, "entry", 0.0) or 0.0
    realized  = col(trade, "realized_pnl", 0.0) or 0.0
    t1_done   = col(trade, "t1_price", None) is not None
    side_tr   = col(trade, "side", None)

    def realize(part: float, exit_price: float, ltype: str, set_cols: dict):
        nonlocal realized
        q = max(0.0, qty_total * part)
        proceeds = q * exit_price
        # PnL calc
        if side_tr == "BUY":
            pnl = (exit_price - entry) * q
        else:
            pnl = (entry - exit_price) * q
        wallet_apply(conn, proceeds, ltype, tag, side_tr, q, exit_price)
        realized += pnl
        set_cols["realized_pnl"] = realized
        placeholders = ",".join([f"{k}=:{k}" for k in set_cols.keys()])
        set_cols["tag"] = tag
        cur.execute(f"UPDATE trades SET {placeholders} WHERE tag=:tag;", set_cols)

    # TARGET1 → close 50%
    if event == "TARGET1" and price > 0:
        if not t1_done:
            realize(0.5, price, "EXIT_T1", {"t1_price": price, "t1_time_utc": now_utc, "status": "PARTIAL"})
        conn.commit(); conn.close()
        return ok()

    # TARGET2 → close remaining (50% if T1 hit, otherwise 100%)
    if event == "TARGET2" and price > 0:
        part = 0.5 if t1_done else 1.0
        realize(part, price, "EXIT_T2", {"t2_price": price, "t2_time_utc": now_utc, "status": "CLOSED_T2"})
        conn.commit(); conn.close()
        return ok()

    # STOPLOSS → close remaining (50% if T1 hit, otherwise 100%)
    if event == "STOPLOSS" and price > 0:
        part = 0.5 if t1_done else 1.0
        realize(part, price, "EXIT_SL", {"sl_price": price, "sl_time_utc": now_utc, "status": "CLOSED_SL"})
        conn.commit(); conn.close()
        return ok()

    conn.commit(); conn.close()
    return ok()

# ─────────────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard", status_code=302)

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request,
                    symbol: str | None = None,
                    side: str | None = None,
                    status: str | None = None,
                    q: str | None = None,
                    sort: str | None = "-ts_entry_utc"):
    conn = get_conn()

    # Wallet & ledger
    bal = wallet_balance(conn)
    ledger_rows = conn.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT 200;").fetchall()

    # Filters
    where = []
    params = {}
    if symbol:
        where.append("symbol = :symbol")
        params["symbol"] = symbol
    if side:
        where.append("side = :side")
        params["side"] = side.upper()
    if status:
        where.append("status = :status")
        params["status"] = status.upper()
    if q:
        params["q"] = f"%{q}%"
        where.append("(tag LIKE :q OR symbol LIKE :q OR tf LIKE :q)")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    valid_sorts = {
        "ts_entry_utc": "ts_entry_utc",
        "-ts_entry_utc": "ts_entry_utc DESC",
        "symbol": "symbol",
        "-symbol": "symbol DESC",
        "entry": "entry",
        "-entry": "entry DESC",
        "status": "status",
        "-status": "status DESC",
    }
    order_by = valid_sorts.get(sort or "-ts_entry_utc", "ts_entry_utc DESC")

    trade_rows = conn.execute(
        f"SELECT * FROM trades {where_sql} ORDER BY {order_by} LIMIT 200;",
        params
    ).fetchall()

    event_rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT 300;"
    ).fetchall()

    conn.close()

    # Build context
    def row_trade(r):
        return {
            "tag": col(r,"tag"),
            "ts_entry_ist": ist(col(r,"ts_entry_utc")),
            "symbol": col(r,"symbol"),
            "tf": col(r,"tf"),
            "side": col(r,"side"),
            "candle": col(r,"candle"),
            "entry": col(r,"entry"),
            "sig_high": col(r,"sig_high"),
            "sig_low": col(r,"sig_low"),
            "t1_price": col(r,"t1_price"),
            "t1_time_ist": ist(col(r,"t1_time_utc")),
            "t2_price": col(r,"t2_price"),
            "t2_time_ist": ist(col(r,"t2_time_utc")),
            "sl_price": col(r,"sl_price"),
            "sl_time_ist": ist(col(r,"sl_time_utc")),
            "status": col(r,"status") or "",
            "qty": col(r,"qty"),
            "spent": col(r,"spent"),
            "realized_pnl": col(r,"realized_pnl", 0.0),
        }

    def row_event(r):
        return {
            "ts_ist": ist(col(r,"ts_utc")),
            "event": col(r,"event"),
            "symbol": col(r,"symbol"),
            "side": col(r,"side"),
            "tf": col(r,"tf"),
            "price": col(r,"price"),
            "tag": col(r,"tag"),
            "raw": col(r,"raw"),
        }

    template = env.get_template("dashboard.html")
    html = template.render(dict(
        request=request,
        secret_hint=SECRET[:2] + "…" + SECRET[-2:],
        wallet=dict(balance=bal),
        ledger=[{
            "ts_ist": ist(col(l,"ts_utc")),
            "type": col(l,"type"),
            "trade_tag": col(l,"trade_tag"),
            "side": col(l,"side"),
            "qty": col(l,"qty"),
            "price": col(l,"price"),
            "amount": col(l,"amount"),
            "balance_after": col(l,"balance_after"),
        } for l in ledger_rows],
        trades=[row_trade(r) for r in trade_rows],
        events=[row_event(e) for e in event_rows],
        filters=dict(symbol=symbol or "", side=side or "", status=status or "", q=q or "", sort=sort or "")
    ))
    return HTMLResponse(html)

# ─────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────
@app.post("/clear-db")
async def clear_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM trades;")
    cur.execute("DELETE FROM events;")
    conn.commit(); conn.close()
    return ok()

@app.get("/healthz")
async def healthz():
    return PlainTextResponse("ok")
