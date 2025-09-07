#!/usr/bin/env python3
"""
Database migration for Pine Script v7 compatibility
- Adds wallet_ledger table for proper transaction tracking
- Makes nullable columns for t2, t3, sigH, sigL, raw
- Ensures proper schema for 50% allocation wallet logic
"""
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
                print(f"[retry] Database locked, retrying... (attempt {attempt + 1})")
            else:
                raise

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrency
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA wal_autocheckpoint=1000;")
    return conn

def create_tables():
    """Create or update tables for Pine Script v7"""
    conn = get_conn()
    try:
        # Create new wallet table (single row approach for current balance)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            balance REAL NOT NULL DEFAULT 1000000.0,
            currency TEXT NOT NULL DEFAULT 'INR',
            updated_ts_utc INTEGER NOT NULL
        );
        """)
        
        # Create wallet_ledger table (transaction history)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS wallet_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc INTEGER NOT NULL,
            type TEXT NOT NULL, -- ENTRY/TARGET1/STOPLOSS/RESET/DEPOSIT
            tag TEXT,
            side TEXT,  -- BUY/SELL
            qty REAL,
            price REAL,
            amount REAL,  -- amount withdrawn (-) or credited (+)
            balance_after REAL NOT NULL,
            raw TEXT
        );
        """)
        
        # Check if trades table exists and add missing columns
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
        
        if 'trades' not in tables:
            # Create new trades table
            conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc INTEGER NOT NULL,
                event TEXT NOT NULL,
                side TEXT NOT NULL,
                symbol TEXT NOT NULL,
                tf TEXT,
                price REAL NOT NULL,
                sigH REAL,  -- nullable
                sigL REAL,  -- nullable  
                sl REAL,
                t1 REAL,
                t2 REAL,    -- nullable (Pine v7 may omit)
                t3 REAL,    -- nullable
                tag TEXT,
                qty REAL,
                spent REAL,
                realized_pnl REAL,
                status TEXT DEFAULT 'OPEN',
                entry_price REAL,
                exit_price REAL,
                exit_ts_utc INTEGER,
                raw TEXT,   -- nullable
                UNIQUE(tag, event)
            );
            """)
        else:
            # Add missing columns to existing trades table
            existing_cols = [row[1] for row in conn.execute("PRAGMA table_info(trades);").fetchall()]
            
            columns_to_add = [
                ("qty", "REAL"),
                ("spent", "REAL"), 
                ("realized_pnl", "REAL"),
                ("status", "TEXT DEFAULT 'OPEN'"),
                ("entry_price", "REAL"),
                ("exit_price", "REAL"),
                ("exit_ts_utc", "INTEGER"),
                ("t3", "REAL")
            ]
            
            for col_name, col_type in columns_to_add:
                if col_name not in existing_cols:
                    try:
                        conn.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type};")
                        print(f"[✓] Added column '{col_name}' to trades table")
                    except sqlite3.OperationalError as e:
                        if "duplicate column name" not in str(e).lower():
                            print(f"[!] Warning: Could not add column '{col_name}': {e}")
                        # Continue with other columns
        
        # Create events table for audit trail
        conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc INTEGER NOT NULL,
            event TEXT NOT NULL,
            symbol TEXT,
            side TEXT,
            tf TEXT,
            price REAL,
            tag TEXT,
            raw TEXT
        );
        """)
        
        # Create indexes for performance (safely check if columns exist first)
        # Check if trades table has all the new columns
        trades_cols = [row[1] for row in conn.execute("PRAGMA table_info(trades);").fetchall()]
        
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts_utc);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_tag ON trades(tag);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_event ON trades(event);")
        
        if 'status' in trades_cols:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);")
        
        # Check if wallet_ledger table exists
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
        if 'wallet_ledger' in tables:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_ledger_ts ON wallet_ledger(ts_utc);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_ledger_type ON wallet_ledger(type);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_ledger_tag ON wallet_ledger(tag);")
        
        if 'events' in tables:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts_utc);")
        
        conn.commit()
        print("[✓] Tables created/updated successfully")
        
    finally:
        conn.close()

def migrate_existing_data():
    """Migrate existing wallet data to new schema"""
    conn = get_conn()
    try:
        # Check if old wallet table exists and has data
        tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='wallet';").fetchall()
        
        if tables:
            # Check if it's the old schema (has 'change' column)
            old_wallet_cols = conn.execute("PRAGMA table_info(wallet);").fetchall()
            col_names = [col[1] for col in old_wallet_cols]
            
            if 'change' in col_names and 'currency' not in col_names:
                print("[i] Migrating from old wallet schema...")
                
                # Get the latest balance from old schema
                latest = conn.execute("SELECT balance FROM wallet ORDER BY id DESC LIMIT 1;").fetchone()
                balance = latest[0] if latest else 1000000.0
                
                # Rename old table
                conn.execute("ALTER TABLE wallet RENAME TO wallet_old;")
                
                # Create new wallet table
                conn.execute("""
                CREATE TABLE wallet (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    balance REAL NOT NULL DEFAULT 1000000.0,
                    currency TEXT NOT NULL DEFAULT 'INR',
                    updated_ts_utc INTEGER NOT NULL
                );
                """)
                
                # Insert current balance
                ts_utc = int(datetime.now(timezone.utc).timestamp())
                conn.execute(
                    "INSERT INTO wallet(id, balance, currency, updated_ts_utc) VALUES (1, ?, 'INR', ?)",
                    (balance, ts_utc)
                )
                
                # Migrate old wallet data to wallet_ledger
                old_data = conn.execute("SELECT * FROM wallet_old ORDER BY id;").fetchall()
                for row in old_data:
                    row_dict = dict(row)
                    conn.execute("""
                        INSERT INTO wallet_ledger(ts_utc, type, amount, balance_after, raw)
                        VALUES (?, 'MIGRATED', ?, ?, ?)
                    """, (row_dict['ts_utc'], row_dict.get('change', 0), row_dict['balance'], f"Migrated from old schema: {row_dict.get('reason', 'Unknown')}"))
                
                conn.commit()
                print(f"[✓] Migrated wallet data with balance ₹{balance:,.2f}")
        
        # Initialize wallet if empty
        wallet_count = conn.execute("SELECT COUNT(*) FROM wallet;").fetchone()[0]
        if wallet_count == 0:
            ts_utc = int(datetime.now(timezone.utc).timestamp())
            conn.execute(
                "INSERT INTO wallet(id, balance, currency, updated_ts_utc) VALUES (1, 1000000.0, 'INR', ?)",
                (ts_utc,)
            )
            conn.execute("""
                INSERT INTO wallet_ledger(ts_utc, type, amount, balance_after, raw)
                VALUES (?, 'INITIAL_DEPOSIT', 1000000.0, 1000000.0, 'Initial wallet setup')
            """, (ts_utc,))
            conn.commit()
            print("[✓] Initialized wallet with ₹1,000,000")
        
    finally:
        conn.close()

def verify_schema():
    """Verify that all required tables and columns exist"""
    conn = get_conn()
    try:
        # Check required tables
        required_tables = ['wallet', 'wallet_ledger', 'trades', 'events']
        existing_tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
        
        for table in required_tables:
            if table in existing_tables:
                print(f"[✓] Table '{table}' exists")
            else:
                print(f"[✗] Missing table '{table}'")
                return False
        
        # Check wallet table has correct schema
        wallet_cols = [col[1] for col in conn.execute("PRAGMA table_info(wallet);").fetchall()]
        required_wallet_cols = ['id', 'balance', 'currency', 'updated_ts_utc']
        
        for col in required_wallet_cols:
            if col in wallet_cols:
                print(f"[✓] Wallet column '{col}' exists")
            else:
                print(f"[✗] Missing wallet column '{col}'")
                return False
        
        # Check current balance
        balance_row = conn.execute("SELECT balance FROM wallet WHERE id=1;").fetchone()
        if balance_row:
            print(f"[✓] Current wallet balance: ₹{balance_row[0]:,.2f}")
        else:
            print("[✗] No wallet balance found")
            return False
        
        return True
        
    finally:
        conn.close()

def main():
    print(f"[i] Migrating database for Pine Script v7: {DB_PATH}")
    try:
        with_retry(create_tables)
        with_retry(migrate_existing_data)
        
        if verify_schema():
            print("[✓] Migration completed successfully")
            print("")
            print("Schema changes:")
            print("  • Added wallet_ledger table for transaction tracking")
            print("  • Updated wallet table (single row with current balance)")
            print("  • Made t2, t3, sigH, sigL, raw columns nullable in trades")
            print("  • Added qty, spent, realized_pnl, status columns to trades")
            print("  • Added events table for audit trail")
            print("")
        else:
            print("[✗] Migration verification failed")
            
    except Exception as e:
        print(f"[✗] Migration failed: {e}")
        raise

if __name__ == "__main__":
    main()
