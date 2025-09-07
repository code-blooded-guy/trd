# TradingView Paper Trading Logger v7

Complete backend upgrade for Pine Script v7 compatibility with improved wallet management and atomic transactions.

## 🆕 What's New in v7

### Pine Script Changes
- **Entry Logic**: Entry at close of first "non-touch then break" candle (untouched candle logic)
- **Fixed T1**: T1 is fixed per symbol+timeframe mapping (user-provided in Pine script)
- **T2 Removed**: T2 is optional/nullable (as requested)
- **Stop Loss**: Uses signal low/high ± SL buffer points
- **Alerts**: Chart-only alerts for signal candles + webhook alerts for ENTRY/STOPLOSS

### Backend Improvements
- **15% Allocation**: Each ENTRY uses exactly 15% of current wallet balance
- **Consolidated View**: Single row per trade instead of separate ENTRY/EXIT rows
- **Advanced Filtering**: Date range, status, side filters with sorting options
- **12-Hour Time Format**: IST timestamps in AM/PM format
- **Complete Ledger**: All wallet transactions without limits
- **Atomic Transactions**: All database operations are atomic (either all succeed or all rollback)
- **Nullable Fields**: Supports nullable t2, t3, sigH, sigL, raw fields
- **Success Rate Tracking**: Profitable trades percentage calculation

## 🚀 Quick Deployment

### Option 1: Automated Deployment (Recommended)
```bash
# Make deployment script executable
chmod +x deploy_v7.sh

# Run deployment (creates backup automatically)
./deploy_v7.sh
```

### Option 2: Manual Steps
```bash
# 1. Backup current system
mkdir backup_$(date +%Y%m%d_%H%M%S)
cp app.py backup_*/app.py.bak
cp trades.db backup_*/trades.db.bak

# 2. Stop existing service
pkill -f "uvicorn.*app:app"

# 3. Run database migration
python3 migrate_db_v7.py

# 4. Update application files
cp app_v7.py app.py
cp templates/dashboard_v7.html templates/dashboard.html

# 5. Start new service
nohup python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload > server.log 2>&1 &
```

## 🧪 Testing

### Run Webhook Tests
```bash
# Make test script executable
chmod +x test_webhook_v7.sh

# Run comprehensive tests
./test_webhook_v7.sh
```

### Manual Test Examples

**ENTRY Event:**
```bash
curl -X POST http://localhost:8000/tv-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "secret":"MY_ULTRA_SECRET",
    "event":"ENTRY",
    "side":"BUY",
    "symbol":"BTCUSDT",
    "tf":"3",
    "price":111000.12,
    "sigHigh":111200.0,
    "sigLow":110900.0,
    "sl":110895.0,
    "t1":111800.0,
    "t2":null,
    "tag":"buy-1234567890"
  }'
```

**TARGET1 Event:**
```bash
curl -X POST http://localhost:8000/tv-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "secret":"MY_ULTRA_SECRET",
    "event":"TARGET1",
    "side":"BUY",
    "symbol":"BTCUSDT",
    "tf":"3",
    "price":111800.0,
    "tag":"buy-1234567890"
  }'
```

**STOPLOSS Event:**
```bash
curl -X POST http://localhost:8000/tv-webhook \
  -H "Content-Type: application/json" \
  -d '{
    "secret":"MY_ULTRA_SECRET",
    "event":"STOPLOSS",
    "side":"BUY",
    "symbol":"BTCUSDT",
    "tf":"3",
    "price":110895.0,
    "tag":"buy-1234567890"
  }'
```

## 📊 Wallet Logic

### ENTRY Event
- Allocates **exactly 15%** of current wallet balance
- Calculates quantity: `qty = (balance * 0.15) / price`
- Reduces wallet balance by spent amount
- Records transaction in wallet_ledger

### EXIT Events (TARGET1, STOPLOSS)
- Calculates P&L: `(exit_price - entry_price) * qty` for BUY, opposite for SELL
- Credits back: `original_spent + P&L`
- Updates wallet balance
- Marks trade as CLOSED

### Example Flow
```
Initial Balance: ₹1,000,000

ENTRY BUY @ ₹100:
- Allocation: ₹150,000 (15%)
- Qty: 1,500
- New Balance: ₹850,000

TARGET1 @ ₹110:
- P&L: (110-100) * 1500 = ₹15,000
- Credit: ₹150,000 + ₹15,000 = ₹165,000
- New Balance: ₹1,015,000
```

## 🗄️ Database Schema

### New Tables
- **wallet**: Single-row table with current balance
- **wallet_ledger**: Transaction history with amounts and balance snapshots
- **trades**: Enhanced with qty, spent, realized_pnl, status columns
- **events**: Audit trail for unknown/debug events

### Migration
The migration script (`migrate_db_v7.py`):
- Creates new tables if they don't exist
- Migrates existing wallet data to new schema
- Makes nullable columns for t2, t3, sigH, sigL, raw
- Preserves all existing trade data

## 🎯 TradingView Setup

### Pine Script Alert Setup
1. Create alert using condition **"Any alert() function call"**
2. Set your webhook URL in that alert
3. The alert will receive JSON from `alert(...)` calls (ENTRY and STOPLOSS)

### Chart Alerts (Optional)
Create separate alerts for:
- "Signal candle (white)" - for white signal notifications
- "Signal candle (yellow)" - for yellow signal notifications
- **Do NOT set webhook URLs** for these (chart notifications only)

### Pine Script Variables to Match
```javascript
secret = input.string("MY_ULTRA_SECRET", "Webhook Secret")  // Must match backend
```

## 📈 Dashboard Features

### New in v7
- **Consolidated Trade View**: Single row per trade instead of separate ENTRY/EXIT rows
- **Advanced Filtering**: Date range, status (open/closed), side (buy/sell) filters
- **Smart Sorting**: Sort by date, symbol, P&L, or status with ascending/descending order
- **12-Hour Time Format**: IST timestamps in AM/PM format for better readability
- **Complete Ledger**: All wallet transactions displayed without limits
- **Success Rate KPI**: Shows profitable trades percentage
- **Enhanced Summary Stats**: Total, open, closed trades with total P&L
- **Improved KPIs**: Shows 15% allocation, Pine script version

### URLs
- Dashboard: `http://localhost:8000/dashboard`
- Health Check: `http://localhost:8000/health`
- Webhook: `http://localhost:8000/tv-webhook`

## 🔧 Configuration

### Environment Variables
Update in `app_v7.py`:
```python
WEBHOOK_SECRET = "MY_ULTRA_SECRET"  # Match your Pine script
DB_PATH = "trades.db"
```

### Symbol Precision
The system automatically sets quantity precision based on symbol:
- Crypto (BTC, ETH): 6 decimal places
- Indian Equities (NSE:, BSE:, NIFTY): 2 decimal places
- Default: 4 decimal places

## 🚨 Error Handling

### Common Issues & Solutions

**"Database is locked"**
- The v7 system uses WAL mode and proper timeouts
- Should be resolved with the improved connection handling

**"Invalid secret"**
- Ensure Pine script `secret` input matches `WEBHOOK_SECRET` in backend
- Check for extra spaces or quotes

**"No ENTRY trade found"**
- Ensure EXIT events (TARGET1, STOPLOSS) use the same `tag` as the ENTRY
- Check that ENTRY event was successfully processed

**Missing/null fields**
- v7 gracefully handles nullable fields (t2, t3, sigH, sigL, raw)
- Only price, side, symbol, event, tag are required

## 🔍 Monitoring

### Logs
```bash
# View real-time logs
tail -f server.log

# Search for specific events
grep "ENTRY" server.log
grep "TARGET1" server.log
grep "ERROR" server.log
```

### Health Monitoring
```bash
# Check service status
curl http://localhost:8000/health

# Check if process is running
ps aux | grep uvicorn
```

## 🔄 Key Changes Summary

### What's New:
- **15% Allocation**: Conservative risk management with 15% per trade (vs previous 30%)
- **Consolidated View**: Single row shows complete trade lifecycle (ENTRY → EXIT)
- **Advanced Filtering**: Date range, status, side filters with sorting options
- **12-Hour Time**: IST timestamps in AM/PM format for better readability
- **Complete Ledger**: All wallet transactions without 100-entry limit
- **Success Tracking**: Profitable trades percentage in KPIs
- **Enhanced UX**: Better dashboard with summary stats and improved navigation

### Technical Improvements:
- **Atomic Transactions**: All database operations are atomic
- **Nullable Fields**: Supports Pine script v7 optional fields (t2, t3, etc.)
- **Better Error Handling**: Graceful handling of missing/null webhook fields
- **Performance**: Optimized SQL queries with proper indexing

## 📁 File Structure

```
/Users/apple/Documents/trd/
├── app_v7.py              # New v7 application
├── migrate_db_v7.py       # v7 database migration
├── deploy_v7.sh           # Automated deployment script
├── test_webhook_v7.sh     # Webhook testing script
├── templates/
│   └── dashboard_v7.html  # Updated dashboard template
├── trades.db             # Database (will be migrated)
├── server.log            # Application logs
└── backup_*/             # Automatic backups
```

## 🎉 Success Verification

After deployment, verify:

1. **Service Running**: `curl http://localhost:8000/health`
2. **Database Migrated**: Check dashboard shows wallet balance
3. **Webhook Working**: Run `./test_webhook_v7.sh`
4. **Dashboard Loading**: Visit `http://localhost:8000/dashboard`

## 📞 Support

If you encounter issues:

1. Check `server.log` for error messages
2. Verify database migration completed: `python3 migrate_db_v7.py`
3. Test webhook with curl commands above
4. Ensure Pine script secret matches backend configuration

---

## 🔄 Rollback (if needed)

If you need to revert to the previous version:

```bash
# Stop v7 service
pkill -f "uvicorn.*app:app"

# Restore from backup (replace with your backup directory)
cp backup_20240115_120000/app.py.bak app.py
cp backup_20240115_120000/dashboard.html.bak templates/dashboard.html

# Restore database if needed
cp backup_20240115_120000/trades.db.bak trades.db

# Start old service
python3 -m uvicorn app:app --host 0.0.0.0 --port 8000
```
