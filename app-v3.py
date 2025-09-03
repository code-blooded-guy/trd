import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# ───────────────────────────────────────────────────────────────────────────
# Config via environment
TV_TEST_SECRET = os.getenv("TV_TEST_SECRET", "changeme")
DB_PATH = os.getenv("DB_PATH", os.path.abspath("./trades.db"))

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

# ───────────────────────────────────────────────────────────────────────────
# FastAPI app + templates
app = FastAPI(title="TradingView Paper Logger", version="1.1.0")
templates = Jinja2Templates(directory="templates")

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# ───────────────────────────────────────────────────────────────────────────
# Database helpers

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init() -> None:
    conn = db_conn()
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
          id       INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_utc   TEXT NOT NULL,  -- ISO8601 UTC time we received the alert
          event    TEXT NOT NULL,  -- ENTRY | TARGET1 | TARGET2 | STOPLOSS | DEBUG
          symbol   TEXT NOT NULL,
          tf       TEXT NOT NULL,
          side     TEXT,
          price    REAL,
          sigHigh  REAL,
          sigLow   REAL,
          sl       REAL,
          t1       REAL,
          t2       REAL,
          tag      TEXT,
          raw      TEXT NOT NULL
        );
        """
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_trades_time   ON trades(ts_utc);")
    c.execute("CREATE INDEX IF NOT EXISTS idx_trades_sym_tf ON trades(symbol, tf);")
    conn.commit()
    conn.close()

db_init()

# ───────────────────────────────────────────────────────────────────────────
# Payload model (matches our Pine JSON)

class TVAlert(BaseModel):
    secret: str = Field(..., description="Must match TV_TEST_SECRET")
    event: str  = Field(..., description="ENTRY | TARGET1 | TARGET2 | STOPLOSS | DEBUG")
    side: Optional[str] = ""
    symbol: str
    tf: str = "1"
    price: Optional[float]  = None
    sigHigh: Optional[float] = None
    sigLow: Optional[float]  = None
    sl: Optional[float]      = None
    t1: Optional[float]      = None
    t2: Optional[float]      = None
    tag: Optional[str]       = ""

# ───────────────────────────────────────────────────────────────────────────
# Routes

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard", status_code=302)

@app.get("/healthz")
async def healthz():
    try:
        conn = db_conn()
        conn.execute("SELECT 1")
        conn.close()
        return {"ok": True, "db": "ready"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

@app.post("/tv-webhook")
async def tv_webhook(payload: TVAlert, request: Request):
    # Secret check
    if payload.secret != TV_TEST_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    # Prefer exact raw body as logged, else dump model
    try:
        raw_bytes = await request.body()
        raw_str = raw_bytes.decode("utf-8") if raw_bytes else payload.model_dump_json()
    except Exception:
        raw_str = payload.model_dump_json()

    ts_utc = datetime.now(tz=UTC).isoformat()

    conn = db_conn()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO trades
          (ts_utc, event, symbol, tf, side, price, sigHigh, sigLow, sl, t1, t2, tag, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ts_utc,
            payload.event.upper(),
            payload.symbol,
            payload.tf,
            (payload.side or "").upper(),
            payload.price,
            payload.sigHigh,
            payload.sigLow,
            payload.sl,
            payload.t1,
            payload.t2,
            payload.tag or "",
            raw_str,
        ),
    )
    conn.commit()
    conn.close()

    return {"ok": True}

@app.post("/clear")
async def clear_db(secret: str):
    if secret != TV_TEST_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")
    conn = db_conn()
    conn.execute("DELETE FROM trades")
    conn.commit()
    conn.close()
    return {"ok": True, "cleared": True}

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    # Pull last 500 events
    conn = db_conn()
    c = conn.cursor()
    c.execute(
        """
        SELECT id, ts_utc, event, symbol, tf, side, price, sigHigh, sigLow, sl, t1, t2, tag, raw
        FROM trades
        ORDER BY id DESC
        LIMIT 500
        """
    )
    rows = c.fetchall()
    conn.close()

    # Build raw stream (IST)
    events = []
    for r in rows:
        ts_ist = datetime.fromisoformat(r["ts_utc"]).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
        events.append({
            "id": r["id"],
            "ts_ist": ts_ist,
            "event": r["event"],
            "symbol": r["symbol"],
            "tf": r["tf"],
            "side": r["side"] or "",
            "price": r["price"],
            "tag": r["tag"] or "",
            "raw": r["raw"],
        })

    # Build trades table: latest ENTRY per tag with current status
    latest_by_tag = {}
    for r in rows:
        tag = (r["tag"] or f"untagged-{r['id']}")
        if tag not in latest_by_tag:
            latest_by_tag[tag] = r  # most recent row for this tag

    trades = []
    for tag, last in latest_by_tag.items():
        # Find the ENTRY row for this tag in our slice (oldest→newest)
        entry_row = None
        for r in reversed(rows):
            if (r["tag"] or f"untagged-{r['id']}") == tag and r["event"] == "ENTRY":
                entry_row = r
                break
        if not entry_row:
            continue

        entry_ts_ist = datetime.fromisoformat(entry_row["ts_utc"]).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
        candle = "White (BUY)" if (entry_row["side"] or "") == "BUY" else "Yellow (SELL)"

        # Scan status updates for this tag
        t1_show = t2_show = sl_show = "—"
        status = "OPEN"
        for r in rows:
            if (r["tag"] or f"untagged-{r['id']}") != tag:
                continue
            ts_ist = datetime.fromisoformat(r["ts_utc"]).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
            if r["event"] == "TARGET1":
                t1_show = f'{(r["price"] or 0):.2f} @ {ts_ist}'
                status = "T1 HIT"
            elif r["event"] == "TARGET2":
                t2_show = f'{(r["price"] or 0):.2f} @ {ts_ist}'
                status = "CLOSED T2"
            elif r["event"] == "STOPLOSS":
                sl_show = f'{(r["price"] or 0):.2f} @ {ts_ist}'
                status = "CLOSED SL"

        trades.append({
            "entry_time": entry_ts_ist,
            "symbol": entry_row["symbol"],
            "tf": entry_row["tf"],
            "candle": candle,
            "entry_price": entry_row["price"],
            "sigH": entry_row["sigHigh"],
            "sigL": entry_row["sigLow"],
            "t1": t1_show,
            "t2": t2_show,
            "sl": sl_show,
            "status": status,
            "tag": tag,
        })

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "trades": trades[:100],
            "events": events[:500],
            "tz_label": "IST (Asia/Kolkata)",
        },
    )
