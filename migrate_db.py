#!/usr/bin/env python3
import os, sqlite3, time
from datetime import datetime, timezone

DB_PATH = os.getenv("DB_PATH", "trades.db")

def connect_db():
    # timeout: how long sqlite waits on lock; isolation_level=None => autocommit
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    # Pragmas to reduce locking issues
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")  # 5s per statement
    return conn

def with_retry(fn, *args, retries=5, delay=1.0, **kwargs):
    for i in range(retries):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "database is locked" in msg or "busy" in msg:
                if i == retries - 1:
                    raise
                time.sleep(delay)
            else:
                raise

def add_column_if_missing(conn, table, column_def):
    col_name = column_def.split()[0]
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col_name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_def}")
        print(f"[+] {table}: added column {column_def}")

def create_tables(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS raw_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts_utc TEXT NOT NULL,
      event TEXT NOT NULL,
      symbol TEXT NOT NULL,
      tf TEXT,
      side TEXT,
      price REAL,
      tag TEXT,
      raw_json TEXT
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts_entry_utc TEXT,
      symbol TEXT,
      tf TEXT,
      side TEXT,
      candle TEXT,
      entry REAL,
      sig_high REAL,
      sig_low REAL,
      t1 REAL,
      t2 REAL,
      sl REAL,
      ts_t1_utc TEXT,
      ts_t2_utc TEXT,
      ts_sl_utc TEXT,
      status TEXT DEFAULT 'OPEN',
      tag TEXT,
      qty REAL,
      spent REAL,
      realized_pl REAL DEFAULT 0
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS wallet (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      balance REAL NOT NULL,
      base_currency TEXT NOT NULL DEFAULT 'INR',
      created_utc TEXT NOT NULL,
      updated_utc TEXT NOT NULL
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS ledger (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts_utc TEXT NOT NULL,
      type TEXT NOT NULL,
      trade_id INTEGER,
      side TEXT,
      qty REAL,
      price REAL,
      amount REAL,
      balance_after REAL,
      tag TEXT,
      FOREIGN KEY (trade_id) REFERENCES trades(id)
    )""")

def add_missing_columns(conn):
    for col in [
        "ts_entry_utc TEXT","symbol TEXT","tf TEXT","side TEXT","candle TEXT",
        "entry REAL","sig_high REAL","sig_low REAL","t1 REAL","t2 REAL","sl REAL",
        "ts_t1_utc TEXT","ts_t2_utc TEXT","ts_sl_utc TEXT","status TEXT DEFAULT 'OPEN'",
        "tag TEXT","qty REAL","spent REAL","realized_pl REAL DEFAULT 0"
    ]: add_column_if_missing(conn, "trades", col)

    for col in [
        "ts_utc TEXT","event TEXT","symbol TEXT","tf TEXT","side TEXT","price REAL","tag TEXT","raw_json TEXT"
    ]: add_column_if_missing(conn, "raw_events", col)

    for col in ["balance REAL","base_currency TEXT","created_utc TEXT","updated_utc TEXT"]:
        add_column_if_missing(conn, "wallet", col)

    for col in [
        "ts_utc TEXT","type TEXT","trade_id INTEGER","side TEXT","qty REAL","price REAL","amount REAL","balance_after REAL","tag TEXT"
    ]: add_column_if_missing(conn, "ledger", col)

def create_indexes(conn):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_events(ts_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_evt ON raw_events(event)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts_entry_utc)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol_tf ON trades(symbol, tf)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger(ts_utc)")

def seed_wallet(conn):
    cur = conn.execute("SELECT id, balance FROM wallet WHERE id = 1")
    row = cur.fetchone()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not row:
        conn.execute(
            "INSERT INTO wallet (id, balance, base_currency, created_utc, updated_utc) VALUES (1, ?, 'INR', ?, ?)",
            (1_000_000.00, now, now),
        )
        print("[+] Seeded wallet with ₹1,000,000.00")
    else:
        print(f"[i] Wallet exists with balance ₹{row[1]:,.2f}")

def main():
    print(f"[i] Migrating DB: {DB_PATH}")
    conn = with_retry(connect_db)
    try:
        with_retry(create_tables, conn)
        with_retry(add_missing_columns, conn)
        with_retry(create_indexes, conn)
        with_retry(seed_wallet, conn)
        print("[✓] Migration completed successfully.")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
