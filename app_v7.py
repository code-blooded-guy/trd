#!/usr/bin/env python3
"""
TradingView Paper Trading Logger - Pine Script v7 Compatible
- Updated for Pine Script v7 Auto (fixed T1, entry on untouched close, webhook ENTRY/STOP)
- 50% wallet allocation per ENTRY
- Proper atomic transactions
- Supports nullable fields (t2, t3, sigH, sigL, raw)
- Wallet with single balance + transaction ledger
"""
import sqlite3
import json
import time
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from contextlib import contextmanager

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# ======================================================
# Configuration
# ======================================================
app = FastAPI(title="TradingView Paper Trading Logger v7")
templates = Jinja2Templates(directory="templates")

DB_PATH = "trades.db"
WEBHOOK_SECRET = "MY_ULTRA_SECRET"  # Match your Pine script input
IST = timezone(timedelta(hours=5, minutes=30))

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ======================================================
# Database Helpers
# ======================================================

_db_lock = threading.RLock()

def get_conn():
    """Get database connection with proper settings for concurrency"""
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    return conn

@contextmanager
def db_transaction():
    """Context manager for safe database transactions"""
    with _db_lock:
        conn = None
        try:
            conn = get_conn()
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.commit()
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except:
                    pass
            raise
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass

# ======================================================
# Utility Functions
# ======================================================

def safe_fnum(value):
    """Safely convert value to float, return None if invalid"""
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None

def now_ts_utc():
    """Get current UTC timestamp as integer"""
    return int(time.time())

def utc_timestamp_to_ist_str(ts):
    """Convert UTC timestamp to IST string"""
    if not ts:
        return None
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)

# ======================================================
# Wallet Functions
# ======================================================

def fetch_wallet(conn):
    """Get current wallet state"""
    cur = conn.execute("SELECT id, balance, currency FROM wallet WHERE id=1")
    row = cur.fetchone()
    if row:
        return {"id": row[0], "balance": float(row[1]), "currency": row[2]}
    
    # Initialize wallet if not exists
    ts = now_ts_utc()
    conn.execute(
        "INSERT INTO wallet(id, balance, currency, updated_ts_utc) VALUES (1, 1000000.0, 'INR', ?)",
        (ts,)
    )
    conn.execute(
        "INSERT INTO wallet_ledger(ts_utc, type, amount, balance_after, raw) VALUES (?, 'INITIAL_DEPOSIT', 1000000.0, 1000000.0, 'Auto-created wallet')",
        (ts,)
    )
    return {"id": 1, "balance": 1000000.0, "currency": "INR"}

def get_symbol_precision(symbol):
    """Get quantity precision based on symbol"""
    symbol_lower = symbol.lower()
    if any(x in symbol_lower for x in ['btc', 'eth', 'crypto']):
        return 6  # 6 decimal places for crypto
    elif any(x in symbol_lower for x in ['nifty', 'bank', 'nse:', 'bse:']):
        return 2  # 2 decimal places for Indian equities
    else:
        return 4  # Default 4 decimal places

# ======================================================
# Trading Logic
# ======================================================

def handle_entry_event(conn, payload: Dict[str, Any]):
    """
    Handle ENTRY event with 50% wallet allocation
    """
    price = safe_fnum(payload.get("price"))
    side = payload.get("side")
    symbol = payload.get("symbol")
    tag = payload.get("tag")
    t1 = safe_fnum(payload.get("t1"))
    t2 = safe_fnum(payload.get("t2"))
    tf = payload.get("tf")
    sigH = safe_fnum(payload.get("sigHigh"))
    sigL = safe_fnum(payload.get("sigLow"))
    sl = safe_fnum(payload.get("sl"))

    if not all([price, side, symbol, tag]):
        raise ValueError("Missing required fields for ENTRY")

    wallet = fetch_wallet(conn)
    balance = wallet["balance"]

    # 15% allocation as specified
    allocation = round(balance * 0.15, 8)
    
    # Calculate quantity with appropriate precision
    precision = get_symbol_precision(symbol)
    qty = round(allocation / price, precision) if price and price > 0 else 0.0
    spent = round(qty * price, 8)

    # Update wallet balance
    new_balance = round(balance - spent, 8)
    ts = now_ts_utc()

    # Record wallet ledger entry (negative for spend)
    conn.execute("""
        INSERT INTO wallet_ledger(ts_utc, type, tag, side, qty, price, amount, balance_after, raw)
        VALUES (?, 'ENTRY', ?, ?, ?, ?, ?, ?, ?)
    """, (ts, tag, side, qty, price, -spent, new_balance, json.dumps(payload)))

    # Update wallet balance
    conn.execute("UPDATE wallet SET balance=?, updated_ts_utc=? WHERE id=1", (new_balance, ts))

    # Insert trade record
    conn.execute("""
        INSERT INTO trades (ts_utc, event, side, symbol, tf, price, sigH, sigL, sl, t1, t2, tag, qty, spent, status, entry_price, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
    """, (ts, "ENTRY", side, symbol, tf, price, sigH, sigL, sl, t1, t2, tag, qty, spent, price, json.dumps(payload)))

    logger.info(f"[ENTRY] {side} {symbol} at {price} | Qty: {qty} | Spent: ₹{spent:,.2f} | Balance: ₹{new_balance:,.2f}")

def handle_exit_event(conn, payload: Dict[str, Any]):
    """
    Handle TARGET1, STOPLOSS, or other exit events
    """
    tag = payload.get("tag")
    event = payload.get("event", "").upper()
    price = safe_fnum(payload.get("price"))
    side = payload.get("side")
    
    if not all([tag, event, price, side]):
        raise ValueError("Missing required fields for exit event")

    ts = now_ts_utc()

    # Find the corresponding ENTRY trade
    cur = conn.execute("""
        SELECT id, qty, entry_price, spent FROM trades 
        WHERE tag=? AND event='ENTRY' AND status='OPEN' 
        ORDER BY ts_utc DESC LIMIT 1
    """, (tag,))
    
    trade_row = cur.fetchone()
    if not trade_row:
        raise ValueError(f"No open ENTRY trade found for tag: {tag}")

    trade_id = trade_row[0]
    qty = float(trade_row[1])
    entry_price = float(trade_row[2])
    spent = float(trade_row[3])

    # Calculate P&L
    if side == "BUY":
        pnl = round((price - entry_price) * qty, 8)
    else:  # SELL
        pnl = round((entry_price - price) * qty, 8)

    # Credit back to wallet: original spent amount + P&L
    credit = round(spent + pnl, 8)
    
    wallet = fetch_wallet(conn)
    balance_before = wallet["balance"]
    new_balance = round(balance_before + credit, 8)

    # Record wallet ledger entry (positive for credit)
    conn.execute("""
        INSERT INTO wallet_ledger(ts_utc, type, tag, side, qty, price, amount, balance_after, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (ts, event, tag, side, qty, price, credit, new_balance, json.dumps(payload)))

    # Update wallet balance
    conn.execute("UPDATE wallet SET balance=?, updated_ts_utc=? WHERE id=1", (new_balance, ts))

    # Update trade status
    conn.execute("""
        UPDATE trades SET status='CLOSED', exit_price=?, exit_ts_utc=?, realized_pnl=?
        WHERE id=?
    """, (price, ts, pnl, trade_id))

    # Insert exit event record
    conn.execute("""
        INSERT INTO trades (ts_utc, event, side, symbol, tf, price, tag, qty, realized_pnl, status, raw)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?)
    """, (ts, event, side, payload.get("symbol"), payload.get("tf"), price, tag, qty, pnl, json.dumps(payload)))

    logger.info(f"[{event}] {side} {tag} at {price} | P&L: ₹{pnl:+,.2f} | Balance: ₹{new_balance:,.2f}")

def process_webhook(payload: Dict[str, Any]):
    """
    Main webhook processing function with atomic transactions
    """
    # Validate secret
    if payload.get("secret") != WEBHOOK_SECRET:
        raise ValueError("Invalid secret")

    event = payload.get("event", "").upper()
    
    with db_transaction() as conn:
        if event == "ENTRY":
            handle_entry_event(conn, payload)
        elif event in ("TARGET1", "STOPLOSS", "TARGET2", "TARGET3"):
            handle_exit_event(conn, payload)
        else:
            # Log unknown events for audit
            ts = now_ts_utc()
            conn.execute("""
                INSERT INTO events(ts_utc, event, symbol, side, tf, price, tag, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, event, payload.get("symbol"), payload.get("side"), 
                  payload.get("tf"), payload.get("price"), payload.get("tag"), json.dumps(payload)))
            logger.warning(f"[UNKNOWN EVENT] {event} - logged to events table")

# ======================================================
# Routes
# ======================================================

@app.get("/")
def root():
    return {"status": "TradingView Paper Trading Logger v7", "health": "ok"}

@app.get("/health")
def health():
    try:
        with db_transaction() as conn:
            wallet = fetch_wallet(conn)
        return {"status": "ok", "wallet_balance": wallet["balance"], "currency": wallet["currency"]}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    """Handle TradingView webhook alerts"""
    try:
        body = await request.body()
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        logger.error(f"[Webhook] Invalid JSON: {e}")
        return JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)

    try:
        process_webhook(data)
        event = data.get("event", "UNKNOWN")
        symbol = data.get("symbol", "")
        tag = data.get("tag", "")
        
        return {"status": "ok", "event": event, "symbol": symbol, "tag": tag}
        
    except ValueError as e:
        logger.error(f"[Webhook] Validation error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except sqlite3.IntegrityError as e:
        if "UNIQUE constraint failed" in str(e):
            logger.info(f"[Webhook] Duplicate ignored: {data.get('tag')}/{data.get('event')}")
            return {"status": "ok", "message": "duplicate_ignored"}
        else:
            logger.error(f"[Webhook] Database integrity error: {e}")
            return JSONResponse({"status": "error", "message": "Database error"}, status_code=500)
    except Exception as e:
        logger.error(f"[Webhook] Processing error: {e}")
        return JSONResponse({"status": "error", "message": "Internal server error"}, status_code=500)

@app.post("/clear-trades")
async def clear_trades(request: Request):
    """Clear all trades but keep wallet history"""
    try:
        with db_transaction() as conn:
            wallet = fetch_wallet(conn)
            balance = wallet["balance"]
            
            # Clear trades table
            conn.execute("DELETE FROM trades;")
            
            # Log the clear action
            ts = now_ts_utc()
            conn.execute("""
                INSERT INTO wallet_ledger(ts_utc, type, amount, balance_after, raw)
                VALUES (?, 'CLEAR_TRADES', 0, ?, 'All trades cleared via API')
            """, (ts, balance))
        
        logger.info(f"[API] All trades cleared, wallet balance preserved: ₹{balance:,.2f}")
        return {"status": "ok", "message": "All trades cleared", "wallet_balance": balance}
        
    except Exception as e:
        logger.error(f"[API] Clear trades error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/add-funds")
async def add_funds(request: Request):
    """Add funds to wallet"""
    try:
        body = await request.body()
        data = json.loads(body.decode("utf-8"))
        
        amount = safe_fnum(data.get("amount"))
        reason = data.get("reason", "MANUAL_DEPOSIT")
        
        if not amount or amount <= 0:
            return JSONResponse({"status": "error", "message": "Invalid amount"}, status_code=400)
        
        with db_transaction() as conn:
            wallet = fetch_wallet(conn)
            balance_before = wallet["balance"]
            balance_after = balance_before + amount
            
            ts = now_ts_utc()
            
            # Update wallet
            conn.execute("UPDATE wallet SET balance=?, updated_ts_utc=? WHERE id=1", (balance_after, ts))
            
            # Log ledger entry
            conn.execute("""
                INSERT INTO wallet_ledger(ts_utc, type, amount, balance_after, raw)
                VALUES (?, ?, ?, ?, ?)
            """, (ts, reason, amount, balance_after, json.dumps(data)))
        
        logger.info(f"[API] Funds added: ₹{amount:,.2f} - Balance: ₹{balance_before:,.2f} → ₹{balance_after:,.2f}")
        return {
            "status": "ok", 
            "message": f"₹{amount:,.2f} added successfully",
            "balance_before": balance_before,
            "balance_after": balance_after,
            "amount_added": amount
        }
        
    except json.JSONDecodeError:
        return JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)
    except Exception as e:
        logger.error(f"[API] Add funds error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    from_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    to_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    sort_by: Optional[str] = Query("date", description="Sort by: date, symbol, pnl, status"),
    sort_order: Optional[str] = Query("desc", description="Sort order: asc, desc"),
    filter_status: Optional[str] = Query(None, description="Filter by status: open, closed, all"),
    filter_side: Optional[str] = Query(None, description="Filter by side: buy, sell, all")
):
    """Render trading dashboard with filtering, sorting, and consolidated trade view"""
    try:
        # Build date filter for SQL
        date_filter = ""
        params = []
        
        if from_date:
            try:
                from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=IST)
                from_utc = int(from_dt.astimezone(timezone.utc).timestamp())
                date_filter += " AND e.ts_utc >= ?"
                params.append(from_utc)
            except ValueError:
                pass  # Ignore invalid dates
        
        if to_date:
            try:
                to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=IST)
                to_utc = int(to_dt.astimezone(timezone.utc).timestamp())
                date_filter += " AND e.ts_utc <= ?"
                params.append(to_utc)
            except ValueError:
                pass  # Ignore invalid dates

        # Build additional filters
        status_filter = ""
        if filter_status and filter_status != "all":
            if filter_status == "open":
                status_filter = " AND (x.exit_ts_utc IS NULL)"
            elif filter_status == "closed":
                status_filter = " AND (x.exit_ts_utc IS NOT NULL)"
        
        side_filter = ""
        if filter_side and filter_side != "all":
            side_filter = f" AND LOWER(e.side) = '{filter_side.lower()}'"

        # Build sort clause
        sort_column = "e.ts_utc"
        if sort_by == "symbol":
            sort_column = "e.symbol"
        elif sort_by == "pnl":
            sort_column = "COALESCE(x.realized_pnl, 0)"
        elif sort_by == "status":
            sort_column = "CASE WHEN x.exit_ts_utc IS NULL THEN 'OPEN' ELSE 'CLOSED' END"
        
        sort_direction = "DESC" if sort_order == "desc" else "ASC"

        with db_transaction() as conn:
            # Get wallet
            wallet = fetch_wallet(conn)
            
            # Consolidated trades query - one row per trade tag
            consolidated_sql = f"""
            SELECT 
                e.tag,
                e.ts_utc,
                e.side,
                e.symbol,
                e.tf,
                e.price as entry_price,
                e.qty,
                e.spent,
                e.sigH,
                e.sigL,
                e.sl,
                e.t1,
                e.t2,
                x.exit_price,
                x.exit_ts_utc,
                x.realized_pnl,
                CASE WHEN x.exit_ts_utc IS NULL THEN 'OPEN' ELSE 'CLOSED' END as status,
                w_after.balance_after as wallet_after_entry,
                w_exit.balance_after as wallet_after_exit
            FROM trades e
            LEFT JOIN (
                SELECT tag, price as exit_price, exit_ts_utc, realized_pnl
                FROM trades 
                WHERE event IN ('TARGET1', 'TARGET2', 'STOPLOSS') 
                  AND exit_ts_utc IS NOT NULL
            ) x ON e.tag = x.tag
            LEFT JOIN wallet_ledger w_after ON e.tag = w_after.tag AND w_after.type = 'ENTRY'
            LEFT JOIN wallet_ledger w_exit ON e.tag = w_exit.tag AND w_exit.type IN ('TARGET1', 'TARGET2', 'STOPLOSS')
            WHERE e.event = 'ENTRY' {date_filter} {status_filter} {side_filter}
            ORDER BY {sort_column} {sort_direction}
            """
            
            trades_raw = conn.execute(consolidated_sql, params).fetchall()
            
            trades = []
            for trade in trades_raw:
                trade_dict = dict(trade)
                # Convert timestamps to IST 12-hour format
                if trade_dict["ts_utc"]:
                    dt = datetime.fromtimestamp(trade_dict["ts_utc"], tz=timezone.utc).astimezone(IST)
                    trade_dict["ts_ist_12h"] = dt.strftime("%Y-%m-%d %I:%M:%S %p")
                else:
                    trade_dict["ts_ist_12h"] = "—"
                
                if trade_dict["exit_ts_utc"]:
                    dt = datetime.fromtimestamp(trade_dict["exit_ts_utc"], tz=timezone.utc).astimezone(IST)
                    trade_dict["exit_ts_ist_12h"] = dt.strftime("%Y-%m-%d %I:%M:%S %p")
                else:
                    trade_dict["exit_ts_ist_12h"] = "—"
                
                trades.append(trade_dict)
            
            # Get all wallet ledger entries (no limit)
            ledger_sql = f"SELECT * FROM wallet_ledger WHERE 1=1 {date_filter.replace('e.ts_utc', 'ts_utc')} ORDER BY ts_utc DESC"
            ledger_raw = conn.execute(ledger_sql, params).fetchall()
            
            ledger = []
            for entry in ledger_raw:
                ledger_dict = dict(entry)
                if ledger_dict["ts_utc"]:
                    dt = datetime.fromtimestamp(ledger_dict["ts_utc"], tz=timezone.utc).astimezone(IST)
                    ledger_dict["ts_ist_12h"] = dt.strftime("%Y-%m-%d %I:%M:%S %p")
                else:
                    ledger_dict["ts_ist_12h"] = "—"
                ledger.append(ledger_dict)

        # Calculate summary statistics
        open_trades = [t for t in trades if t["status"] == "OPEN"]
        closed_trades = [t for t in trades if t["status"] == "CLOSED"]
        total_pnl = sum([t["realized_pnl"] or 0 for t in closed_trades])

        context = {
            "request": request,
            "wallet": wallet,
            "trades": trades,
            "ledger": ledger,
            "secret_hint": WEBHOOK_SECRET[:8] + "..." if len(WEBHOOK_SECRET) > 8 else WEBHOOK_SECRET,
            "from_date": from_date or "",
            "to_date": to_date or "",
            "sort_by": sort_by,
            "sort_order": sort_order,
            "filter_status": filter_status or "all",
            "filter_side": filter_side or "all",
            "total_trades": len(trades),
            "open_trades": len(open_trades),
            "closed_trades": len(closed_trades),
            "total_pnl": total_pnl,
            "ledger_count": len(ledger)
        }
        
        return templates.TemplateResponse("dashboard_v7.html", context)
        
    except Exception as e:
        logger.error(f"[Dashboard] Error: {e}")
        return HTMLResponse(f"<h1>Dashboard Error</h1><p>{str(e)}</p>", status_code=500)

# ======================================================
# Initialize database on startup
# ======================================================

@app.on_event("startup")
async def startup_event():
    """Initialize database on startup"""
    try:
        # Import and run migration
        import migrate_db_v7
        migrate_db_v7.main()
        logger.info("[Startup] Database v7 initialized successfully")
    except Exception as e:
        logger.error(f"[Startup] Database initialization failed: {e}")
        raise

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
