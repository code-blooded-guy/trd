#!/usr/bin/env python3
import sqlite3
import time
from datetime import datetime, timezone

DB_PATH = "trades.db"

def with_retry(fn, *args, retries=5, delay=0.2, **kwargs):
    """Execute function with exponential backoff retry on database lock"""
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < retries - 1:
                time.sleep(delay * (2 ** attempt))
            else:
                raise

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrency
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def create_tables():
    """Create tables exactly as per master brief"""
    conn = get_conn()
    try:
        # Trades table (core table as per brief)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc INTEGER NOT NULL,
            event TEXT NOT NULL,
            side TEXT NOT NULL,
            symbol TEXT NOT NULL,
            tf TEXT,
            price REAL NOT NULL,
            sigH REAL,
            sigL REAL,
            sl REAL,
            t1 REAL,
            t2 REAL,
            tag TEXT,
            wallet_after REAL,

            raw TEXT,
            UNIQUE(tag, event)
        );
        """)
        
        # Wallet table (as per brief)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc INTEGER,
            balance REAL,
            change REAL,
            reason TEXT,
            trade_id INTEGER,
            FOREIGN KEY (trade_id) REFERENCES trades(id)
        );
        """)
        
        # Create indexes for performance
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts_utc);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_tag ON trades(tag);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_event ON trades(event);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_ts ON wallet(ts_utc);")
        
        conn.commit()
        print("[✓] Tables created successfully")
        
    finally:
        conn.close()

def seed_wallet():
    """Seed initial wallet balance of ₹1,000,000"""
    conn = get_conn()
    try:
        # Check if wallet has any entries
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM wallet;")
        count = cur.fetchone()[0]
        
        if count == 0:
            ts_utc = int(datetime.now(timezone.utc).timestamp())
            cur.execute("""
                INSERT INTO wallet (ts_utc, balance, change, reason, trade_id) 
                VALUES (?, ?, ?, ?, NULL);
            """, (ts_utc, 1000000.0, 1000000.0, "INITIAL_DEPOSIT"))
            conn.commit()
            print("[✓] Seeded wallet with ₹1,000,000")
        else:
            # Get current balance
            cur.execute("SELECT balance FROM wallet ORDER BY id DESC LIMIT 1;")
            balance = cur.fetchone()[0]
            print(f"[i] Wallet exists with balance ₹{balance:,.2f}")
            
    finally:
        conn.close()

def main():
    print(f"[i] Migrating database: {DB_PATH}")
    try:
        with_retry(create_tables)
        with_retry(seed_wallet)
        print("[✓] Migration completed successfully")
    except Exception as e:
        print(f"[✗] Migration failed: {e}")
        raise

if __name__ == "__main__":
    main()