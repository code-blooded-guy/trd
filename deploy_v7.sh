#!/bin/bash
# Deploy Pine Script v7 Compatible Backend
# Run this script to update your trading system with v7 compatibility

set -e  # Exit on any error

echo "🚀 Deploying TradingView Paper Trading Logger v7..."
echo ""

# Backup current system
echo "📁 Creating backup of current system..."
BACKUP_DIR="backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Backup existing files
[ -f app.py ] && cp app.py "$BACKUP_DIR/app.py.bak"
[ -f trades.db ] && cp trades.db "$BACKUP_DIR/trades.db.bak"
[ -f templates/dashboard.html ] && cp templates/dashboard.html "$BACKUP_DIR/dashboard.html.bak"

echo "✅ Backup created in $BACKUP_DIR/"

# Stop existing service (if running)
echo ""
echo "🛑 Stopping existing service..."
if pgrep -f "uvicorn.*app:app" > /dev/null; then
    pkill -f "uvicorn.*app:app" || true
    echo "✅ Stopped existing uvicorn processes"
    sleep 2
else
    echo "ℹ️  No existing uvicorn processes found"
fi

# Run database migration
echo ""
echo "🗄️  Running database migration..."
python3 migrate_db_v7.py

# Replace application files
echo ""
echo "📝 Updating application files..."
cp app_v7.py app.py
cp templates/dashboard_v7.html templates/dashboard.html

echo "✅ Application files updated"

# Install dependencies (if requirements.txt exists)
if [ -f requirements.txt ]; then
    echo ""
    echo "📦 Installing dependencies..."
    pip3 install -r requirements.txt
fi

# Start new service
echo ""
echo "🚀 Starting new service..."
nohup python3 -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload > server.log 2>&1 &
SERVER_PID=$!

# Wait a moment for startup
sleep 3

# Check if service started successfully
if ps -p $SERVER_PID > /dev/null; then
    echo "✅ Service started successfully (PID: $SERVER_PID)"
    echo "📊 Dashboard: http://localhost:8000/dashboard"
    echo "🔗 Webhook: http://localhost:8000/tv-webhook"
    echo "📜 Logs: tail -f server.log"
else
    echo "❌ Service failed to start. Check server.log for errors."
    exit 1
fi

echo ""
echo "🎉 Pine Script v7 deployment complete!"
echo ""
echo "What's new in v7:"
echo "  • Entry at close of untouched candle"
echo "  • Fixed T1 per symbol/timeframe"
echo "  • T2 removed (nullable in webhook)"
echo "  • 15% wallet allocation per trade"
echo "  • Consolidated single-row trade view"
echo "  • Advanced sorting & filtering"
echo "  • 12-hour IST time format"
echo "  • Complete ledger (no limits)"
echo ""
echo "Next steps:"
echo "1. Update your TradingView Pine script to v7"
echo "2. Test webhook with: bash test_webhook_v7.sh"
echo "3. Monitor logs: tail -f server.log"
echo ""
