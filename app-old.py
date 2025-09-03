from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base, Session
import json, os
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

BASE_DIR = Path(__file__).parent
env = Environment(loader=FileSystemLoader(str(BASE_DIR / "templates")))
# ---- config ----
TEST_SECRET = os.getenv("TV_TEST_SECRET", "TEST_SECRET_123")   # must match Pine
DB_URL = os.getenv("DB_URL", f"sqlite:///{(BASE_DIR / 'trades.db')}")
TITLE = "TradingView Paper Logger"

# ---- db ----
engine = create_engine(DB_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()

class TradeEvent(Base):
    __tablename__ = "trade_events"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=datetime.utcnow, index=True)
    event = Column(String, index=True)        # ENTRY | TARGET1 | STOPLOSS | TARGET2 (optional)
    side = Column(String, nullable=True)      # BUY | SELL
    symbol = Column(String, index=True)
    tf = Column(String, nullable=True)
    price = Column(Float, nullable=True)
    sig_high = Column(Float, nullable=True)
    sig_low = Column(Float, nullable=True)
    tag = Column(String, index=True)          # idempotency / grouping
    raw = Column(Text)                        # store full payload

Base.metadata.create_all(engine)

# ---- web ----
app = FastAPI(title=TITLE)
env = Environment(loader=FileSystemLoader("."))

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class TVAlert(BaseModel):
    secret: str
    event: str
    symbol: str
    tf: str | None = None
    side: str | None = None
    price: float | None = None
    sigHigh: float | None = None
    sigLow: float | None = None
    tag: str | None = None

@app.post("/tv-webhook")
async def tv_webhook(alert: TVAlert, db: Session = Depends(get_db)):
    if alert.secret != TEST_SECRET:
        raise HTTPException(status_code=401, detail="bad secret")

    # simple idempotency by (event, symbol, tag)
    if alert.tag:
        exists = db.query(TradeEvent).filter(
            TradeEvent.event==alert.event.upper(),
            TradeEvent.symbol==alert.symbol,
            TradeEvent.tag==alert.tag
        ).first()
        if exists:
            return {"ok": True, "dup": True, "id": exists.id}

    row = TradeEvent(
        event=alert.event.upper(),
        side=(alert.side or "").upper() if alert.side else None,
        symbol=alert.symbol,
        tf=alert.tf,
        price=alert.price,
        sig_high=alert.sigHigh,
        sig_low=alert.sigLow,
        tag=alert.tag,
        raw=json.dumps(alert.model_dump())
    )
    db.add(row); db.commit(); db.refresh(row)
    return {"ok": True, "id": row.id}

@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse(url="/dashboard")

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(db: Session = Depends(get_db)):
    rows = db.query(TradeEvent).order_by(TradeEvent.ts.desc()).limit(500).all()
    entries = db.query(TradeEvent).filter(TradeEvent.event=="ENTRY")\
             .order_by(TradeEvent.ts.desc()).limit(100).all()
    groups = []
    for e in entries:
        t1 = db.query(TradeEvent).filter(TradeEvent.symbol==e.symbol, TradeEvent.ts>e.ts, TradeEvent.event=="TARGET1")\
             .order_by(TradeEvent.ts.asc()).first()
        sl = db.query(TradeEvent).filter(TradeEvent.symbol==e.symbol, TradeEvent.ts>e.ts, TradeEvent.event=="STOPLOSS")\
             .order_by(TradeEvent.ts.asc()).first()
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

    tmpl = env.get_template("dashboard.html")
    return tmpl.render(rows=rows, groups=groups, title=TITLE)

@app.post("/admin/clear")
def clear(db: Session = Depends(get_db)):
    db.query(TradeEvent).delete(); db.commit()
    return {"ok": True}
