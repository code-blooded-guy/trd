# app.py
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional
import json
import os

from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from jinja2 import Environment, FileSystemLoader, TemplateNotFound

# ──────────────────────────────
# Paths & Config (absolute)
# ──────────────────────────────
BASE_DIR: Path = Path(__file__).resolve().parent
TEMPLATES_DIR: Path = BASE_DIR / "templates"
DB_PATH: Path = BASE_DIR / "trades.db"

# Jinja environment points to templates/
env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))

# Secret must match what Pine sends in alert JSON
TEST_SECRET = os.getenv("TV_TEST_SECRET", "TEST_SECRET_123")

# SQLite URL (absolute path so it works no matter the working dir)
DB_URL = f"sqlite:///{DB_PATH}"

TITLE = "TradingView Paper Logger"

# ──────────────────────────────
# Database (SQLAlchemy)
# ──────────────────────────────
engine = create_engine(DB_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


class TradeEvent(Base):
    __tablename__ = "trade_events"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    event = Column(String, index=True)        # ENTRY | TARGET1 | TARGET2 | STOPLOSS
    side = Column(String, nullable=True)      # BUY | SELL (ENTRY only)
    symbol = Column(String, index=True)
    tf = Column(String, nullable=True)
    price = Column(Float, nullable=True)
    sig_high = Column(Float, nullable=True)
    sig_low = Column(Float, nullable=True)
    tag = Column(String, index=True)          # idempotency / grouping
    raw = Column(Text)                        # full payload

# Create tables on import
Base.metadata.create_all(engine)

# ──────────────────────────────
# FastAPI app
# ──────────────────────────────
app = FastAPI(title=TITLE)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Pydantic model (Py3.9+ compatible typing)
class TVAlert(BaseModel):
    secret: str
    event: str
    symbol: str
    tf: Optional[str] = None
    side: Optional[str] = None
    price: Optional[float] = None
    sigHigh: Optional[float] = None
    sigLow: Optional[float] = None
    tag: Optional[str] = None


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/_debug")
def debug():
    try:
        template_list = [p.name for p in TEMPLATES_DIR.glob("*")]
    except Exception as e:
        template_list = [f"ERR: {e}"]
    return {
        "cwd": os.getcwd(),
        "base_dir": str(BASE_DIR),
        "templates_dir": str(TEMPLATES_DIR),
        "templates_list": template_list,
        "db_path": str(DB_PATH),
        "db_exists": DB_PATH.exists(),
        "secret_is_set": TEST_SECRET != "TEST_SECRET_123",
        "title": TITLE,
    }


@app.post("/tv-webhook")
async def tv_webhook(alert: TVAlert, db: Session = Depends(get_db)):
    TEST_SECRET = "MY_ULTRA_SECRET"
    if alert.secret != TEST_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    # Simple idempotency: skip if an identical (event, symbol, tag) already exists
    if alert.tag:
        existing = (
            db.query(TradeEvent)
            .filter(
                TradeEvent.event == alert.event.upper(),
                TradeEvent.symbol == alert.symbol,
                TradeEvent.tag == alert.tag,
            )
            .first()
        )
        if existing:
            return {"ok": True, "dup": True, "id": existing.id}

    row = TradeEvent(
        event=alert.event.upper(),
        side=(alert.side or "").upper() if alert.side else None,
        symbol=alert.symbol,
        tf=alert.tf,
        price=alert.price,
        sig_high=alert.sigHigh,
        sig_low=alert.sigLow,
        tag=alert.tag,
        raw=json.dumps(alert.model_dump()),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "id": row.id}


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(db: Session = Depends(get_db)):
    # recent stream (limit to keep page light)
    rows = db.query(TradeEvent).order_by(TradeEvent.ts.desc()).limit(500).all()

    # pair basic outcomes for the latest entries
    entries = (
        db.query(TradeEvent)
        .filter(TradeEvent.event == "ENTRY")
        .order_by(TradeEvent.ts.desc())
        .limit(100)
        .all()
    )
    groups = []
    for e in entries:
        t1 = (
            db.query(TradeEvent)
            .filter(
                TradeEvent.symbol == e.symbol,
                TradeEvent.ts > e.ts,
                TradeEvent.event == "TARGET1",
            )
            .order_by(TradeEvent.ts.asc())
            .first()
        )
        sl = (
            db.query(TradeEvent)
            .filter(
                TradeEvent.symbol == e.symbol,
                TradeEvent.ts > e.ts,
                TradeEvent.event == "STOPLOSS",
            )
            .order_by(TradeEvent.ts.asc())
            .first()
        )
        outcome, outcome_at = "OPEN", None
        if t1 and sl:
            if t1.ts < sl.ts:
                outcome, outcome_at = "FIRST_EVENT_T1", t1.ts
            else:
                outcome, outcome_at = "FIRST_EVENT_SL", sl.ts
        elif t1:
            outcome, outcome_at = "T1_REACHED", t1.ts
        elif sl:
            outcome, outcome_at = "SL_HIT", sl.ts
        groups.append({"e": e, "outcome": outcome, "outcome_at": outcome_at})

    # Render template; fallback inline HTML if template missing
    try:
        tmpl = env.get_template("dashboard.html")
        return tmpl.render(rows=rows, groups=groups, title=TITLE)
    except TemplateNotFound:
        # Minimal fallback so /dashboard still loads and tells you what's wrong
        return HTMLResponse(
            f"""
<!doctype html>
<html><head><meta charset="utf-8"><title>{TITLE} – Fallback</title></head>
<body>
  <h2>{TITLE} – Fallback</h2>
  <p><b>Template not found.</b> Create: <code>{TEMPLATES_DIR}/dashboard.html</code></p>
  <p>Templates dir contents: {list(p.name for p in TEMPLATES_DIR.glob("*"))}</p>
  <h3>Latest events (first 10 raw):</h3>
  <pre style="font-family:monospace; white-space:pre-wrap;">
{os.linesep.join((r.raw or "") for r in rows[:10])}
  </pre>
</body></html>
""",
            status_code=200,
        )
