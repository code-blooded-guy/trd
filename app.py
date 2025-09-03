import sqlite3
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

# ======================================================
# Configuration
# ======================================================
app = FastAPI(title="TradingView Paper Trading Logger")
templates = Jinja2Templates(directory="templates")

DB_PATH = "trades.db"
SECRET_KEY = "MY_ULTRA_SECRET"
IST = timezone(timedelta(hours=5, minutes=30))

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ======================================================
# Database Helpers
# ======================================================
def get_conn():
    """Get database connection with proper settings"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def exec_with_retry(queries, retries=5, backoff=0.2):
    """Execute multiple queries with retry logic for database locks"""
    for attempt in range(retries):
        try:
            conn = get_conn()
            cur = conn.cursor()
            for sql, params in queries:
                cur.execute(sql, params)
            conn.commit()
            conn.close()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
                logger.warning(f"Database locked, retrying... (attempt {attempt + 1})")
            else:
                logger.error(f"Database operation failed after {retries} attempts: {e}")
                raise
        except Exception as e:
            logger.error(f"Database error: {e}")
            raise

def query_db(sql, params=()):
    """Query database with error handling"""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.error(f"Query error: {e}")
        raise

# ======================================================
# Wallet Functions
# ======================================================
def get_wallet_balance():
    """Get current wallet balance"""
    rows = query_db("SELECT balance FROM wallet ORDER BY id DESC LIMIT 1;")
    return rows[0]["balance"] if rows else 0.0

def log_wallet_change(balance_before, balance_after, reason, trade_id=None):
    """Log wallet change to wallet table"""
    ts_utc = int(datetime.now(timezone.utc).timestamp())
    change = balance_after - balance_before
    
    queries = [(
        "INSERT INTO wallet (ts_utc, balance, change, reason, trade_id) VALUES (?, ?, ?, ?, ?);",
        (ts_utc, balance_after, change, reason, trade_id)
    )]
    exec_with_retry(queries)
    logger.info(f"[Wallet] {reason}: {balance_before:,.2f} → {balance_after:,.2f} (Δ{change:+,.2f})")

# ======================================================
# Utility Functions
# ======================================================
def safe_float(value):
    """Safely convert value to float"""
    try:
        return float(value) if value is not None else None
    except (ValueError, TypeError):
        return None

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
# Routes
# ======================================================
@app.get("/")
def root():
    return {"status": "TradingView Paper Trading Logger", "health": "ok"}

@app.get("/health")
def health():
    balance = get_wallet_balance()
    return {"status": "ok", "wallet_balance": balance}

@app.post("/tv-webhook")
async def tv_webhook(request: Request):
    """Handle TradingView webhook alerts"""
    try:
        body = await request.body()
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        logger.error(f"[Webhook] Invalid JSON: {e}")
        return JSONResponse({"status": "error", "message": "Invalid JSON"}, status_code=400)

    # Validate secret
    if data.get("secret") != SECRET_KEY:
        logger.warning(f"[Webhook] Invalid secret from {request.client.host}")
        return JSONResponse({"status": "error", "message": "Unauthorized"}, status_code=401)

    # Extract and validate required fields
    try:
        event = data.get("event")
        side = data.get("side")
        symbol = data.get("symbol")
        price = safe_float(data.get("price"))
        tag = data.get("tag")

        if not all([event, side, symbol, price, tag]):
            raise ValueError("Missing required fields")

        if event not in ["ENTRY", "TARGET1", "TARGET2", "STOPLOSS"]:
            raise ValueError(f"Invalid event: {event}")

        if side not in ["BUY", "SELL"]:
            raise ValueError(f"Invalid side: {side}")

    except Exception as e:
        logger.error(f"[Webhook] Validation error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

    # Extract optional fields (map Pine Script field names)
    tf = data.get("tf")
    sigH = safe_float(data.get("sigHigh"))  # Pine Script sends "sigHigh"
    sigL = safe_float(data.get("sigLow"))   # Pine Script sends "sigLow"
    sl = safe_float(data.get("sl"))
    t1 = safe_float(data.get("t1"))
    t2 = safe_float(data.get("t2"))

    ts_utc = int(datetime.now(timezone.utc).timestamp())
    raw_json = json.dumps(data)

    try:
        if event == "ENTRY":
            # Handle new trade entry
            balance_before = get_wallet_balance()
            position_size = balance_before * 0.3  # Use 30% of balance (conservative)
            balance_after = balance_before - position_size
            
            # Insert trade record
            queries = [
                ("""INSERT INTO trades (ts_utc, event, side, symbol, tf, price, sigH, sigL, sl, t1, t2, tag, wallet_after, raw) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                 (ts_utc, event, side, symbol, tf, price, sigH, sigL, sl, t1, t2, tag, balance_after, raw_json))
            ]
            exec_with_retry(queries)
            
            # Get the trade ID for wallet logging
            trade_rows = query_db("SELECT id FROM trades WHERE tag = ? ORDER BY id DESC LIMIT 1;", (tag,))
            trade_id = trade_rows[0]["id"] if trade_rows else None
            
            # Log wallet change
            log_wallet_change(balance_before, balance_after, f"ENTRY_{side}_{symbol}", trade_id)
            
            logger.info(f"[Webhook] ENTRY {side} {symbol} at {price} - Position: ₹{position_size:,.2f} (30%), Tag: {tag}")

        elif event in ["TARGET1", "TARGET2", "STOPLOSS"]:
            # Handle trade exit
            # Find the original ENTRY trade
            entry_trades = query_db(
                "SELECT * FROM trades WHERE tag = ? AND event = 'ENTRY' ORDER BY id DESC LIMIT 1;", 
                (tag,)
            )
            
            if not entry_trades:
                logger.error(f"[Webhook] No ENTRY trade found for tag: {tag}")
                return JSONResponse({"status": "error", "message": f"No ENTRY trade found for tag: {tag}"}, status_code=400)

            try:
                entry_trade = dict(entry_trades[0])
                entry_price = safe_float(entry_trade["price"])
                
                if not entry_price:
                    logger.error(f"[Webhook] Invalid entry price for tag: {tag}")
                    return JSONResponse({"status": "error", "message": "Invalid entry price"}, status_code=400)

                # Calculate position size from wallet transaction for this ENTRY
                wallet_entries = query_db(
                    "SELECT change FROM wallet WHERE trade_id = ? AND reason LIKE 'ENTRY_%' ORDER BY id DESC LIMIT 1;",
                    (entry_trade["id"],)
                )
                if wallet_entries:
                    position_size = abs(wallet_entries[0]["change"])  # Make positive (was negative when deducted)
                    logger.info(f"[Webhook] Found position_size from wallet: ₹{position_size:,.2f}")
                else:
                    # Fallback: estimate 30% of what the balance likely was before entry
                    # Since we know wallet_after, estimate position_size
                    current_balance = get_wallet_balance()
                    estimated_balance_before = current_balance / 0.7  # If 70% remained, original was this
                    position_size = estimated_balance_before * 0.3
                    logger.info(f"[Webhook] Estimated position_size: ₹{position_size:,.2f}")
                
                # Calculate P&L
                if side == "BUY":
                    pnl = position_size * (price - entry_price) / entry_price
                else:  # SELL
                    pnl = position_size * (entry_price - price) / entry_price
                    
                balance_before = get_wallet_balance()
                balance_after = balance_before + position_size + pnl
                
                logger.info(f"[Webhook] {event} {side} {symbol} at {price} - Position: ₹{position_size:,.2f}, P&L: ₹{pnl:+,.2f}, Tag: {tag}")
                
            except Exception as e:
                logger.error(f"[Webhook] Error processing {event} for tag {tag}: {e}")
                return JSONResponse({"status": "error", "message": f"Processing error: {str(e)}"}, status_code=500)
            
            # Insert trade record
            queries = [
                ("""INSERT INTO trades (ts_utc, event, side, symbol, tf, price, sigH, sigL, sl, t1, t2, tag, wallet_after, raw) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                 (ts_utc, event, side, symbol, tf, price, sigH, sigL, sl, t1, t2, tag, balance_after, raw_json))
            ]
            exec_with_retry(queries)
            
            # Get the trade ID for wallet logging
            trade_rows = query_db("SELECT id FROM trades WHERE tag = ? ORDER BY id DESC LIMIT 1;", (tag,))
            trade_id = trade_rows[0]["id"] if trade_rows else None
            
            # Log wallet change
            log_wallet_change(balance_before, balance_after, f"{event}_{side}_{symbol}", trade_id)
            
            logger.info(f"[Webhook] {event} {side} {symbol} at {price} - P&L: ₹{pnl:+,.2f}, Tag: {tag}")

    except sqlite3.IntegrityError as e:
        if "UNIQUE constraint failed" in str(e):
            # Gracefully ignore duplicates as per Pine Script requirement
            logger.info(f"[Webhook] Duplicate ignored: {tag}/{event}")
            return {"status": "ok", "message": "duplicate_ignored", "event": event, "tag": tag}
        else:
            logger.error(f"[Webhook] Database integrity error: {e}")
            return JSONResponse({"status": "error", "message": "Database error"}, status_code=500)
    
    except Exception as e:
        logger.error(f"[Webhook] Processing error: {e}")
        return JSONResponse({"status": "error", "message": "Internal server error"}, status_code=500)

    return {"status": "ok", "event": event, "symbol": symbol, "tag": tag}

@app.post("/clear-trades")
async def clear_trades(request: Request):
    """Clear all trades but keep wallet history"""
    try:
        # Get current wallet balance before clearing
        current_balance = get_wallet_balance()
        
        # Clear trades table
        queries = [("DELETE FROM trades;", ())]
        exec_with_retry(queries)
        
        # Log the clear action
        log_wallet_change(current_balance, current_balance, "TRADES_CLEARED", None)
        
        logger.info(f"[API] All trades cleared, wallet balance preserved: ₹{current_balance:,.2f}")
        return {"status": "ok", "message": "All trades cleared", "wallet_balance": current_balance}
        
    except Exception as e:
        logger.error(f"[API] Clear trades error: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

@app.post("/add-funds")
async def add_funds(request: Request):
    """Add funds to wallet"""
    try:
        body = await request.body()
        data = json.loads(body.decode("utf-8"))
        
        amount = safe_float(data.get("amount"))
        reason = data.get("reason", "MANUAL_DEPOSIT")
        
        if not amount or amount <= 0:
            return JSONResponse({"status": "error", "message": "Invalid amount"}, status_code=400)
            
        # Get current balance and add funds
        balance_before = get_wallet_balance()
        balance_after = balance_before + amount
        
        # Log the fund addition
        log_wallet_change(balance_before, balance_after, reason, None)
        
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
def dashboard(request: Request):
    """Render trading dashboard"""
    try:
        # Get wallet balance
        wallet_balance = get_wallet_balance()
        
        # Get trades data
        trades_raw = query_db("SELECT * FROM trades ORDER BY ts_utc DESC LIMIT 200;")
        trades = []
        
        for trade in trades_raw:
            trade_dict = dict(trade)
            # Convert UTC timestamp to IST string
            trade_dict["ts_ist"] = utc_timestamp_to_ist_str(trade_dict["ts_utc"])
            trades.append(trade_dict)
        
        # Get wallet ledger
        ledger_raw = query_db("SELECT * FROM wallet ORDER BY ts_utc DESC LIMIT 100;")
        ledger = []
        
        for entry in ledger_raw:
            ledger_dict = dict(entry)
            # Convert UTC timestamp to IST string  
            ledger_dict["ts_ist"] = utc_timestamp_to_ist_str(ledger_dict["ts_utc"])
            ledger.append(ledger_dict)

        context = {
            "request": request,
            "wallet": {"balance": wallet_balance},
            "trades": trades,
            "ledger": ledger,
            "secret_hint": SECRET_KEY[:8] + "..." if len(SECRET_KEY) > 8 else SECRET_KEY
        }
        
        return templates.TemplateResponse("dashboard.html", context)
        
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
        import migrate_db
        migrate_db.main()
        logger.info("[Startup] Database initialized successfully")
    except Exception as e:
        logger.error(f"[Startup] Database initialization failed: {e}")
        raise

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)