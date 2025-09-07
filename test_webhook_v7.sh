#!/bin/bash
# Test Pine Script v7 Webhook Endpoints
# This script tests the webhook with realistic payloads matching the updated Pine script

BASE_URL="http://localhost:8000"
# Change to your actual webhook URL for production testing:
# BASE_URL="https://tv.ezboss.in"

echo "🧪 Testing Pine Script v7 Webhook Endpoints..."
echo "📡 Base URL: $BASE_URL"
echo ""

# Function to make API call and check response
test_webhook() {
    local name="$1"
    local payload="$2"
    
    echo "🔍 Testing: $name"
    echo "📤 Payload: $payload"
    
    response=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/tv-webhook" \
        -H "Content-Type: application/json" \
        -d "$payload")
    
    http_code=$(echo "$response" | tail -n1)
    body=$(echo "$response" | head -n -1)
    
    if [ "$http_code" = "200" ]; then
        echo "✅ Success ($http_code): $body"
    else
        echo "❌ Failed ($http_code): $body"
    fi
    echo ""
}

# Test 1: Valid ENTRY (BUY)
test_webhook "ENTRY BUY (Pine v7)" '{
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

# Wait a moment
sleep 1

# Test 2: TARGET1 for the same trade
test_webhook "TARGET1 (Pine v7)" '{
  "secret":"MY_ULTRA_SECRET",
  "event":"TARGET1",
  "side":"BUY",
  "symbol":"BTCUSDT",
  "tf":"3",
  "price":111800.0,
  "tag":"buy-1234567890"
}'

# Wait a moment
sleep 1

# Test 3: Valid ENTRY (SELL)
test_webhook "ENTRY SELL (Pine v7)" '{
  "secret":"MY_ULTRA_SECRET",
  "event":"ENTRY",
  "side":"SELL",
  "symbol":"ETHUSDT",
  "tf":"5",
  "price":3500.50,
  "sigHigh":3520.0,
  "sigLow":3480.0,
  "sl":3525.0,
  "t1":3350.0,
  "tag":"sell-1234567891"
}'

# Wait a moment
sleep 1

# Test 4: STOPLOSS for the sell trade
test_webhook "STOPLOSS (Pine v7)" '{
  "secret":"MY_ULTRA_SECRET",
  "event":"STOPLOSS",
  "side":"SELL",
  "symbol":"ETHUSDT",
  "tf":"5",
  "price":3525.0,
  "tag":"sell-1234567891"
}'

# Test 5: Invalid secret (should fail)
test_webhook "Invalid Secret (should fail)" '{
  "secret":"WRONG_SECRET",
  "event":"ENTRY",
  "side":"BUY",
  "symbol":"BTCUSDT",
  "price":50000,
  "tag":"test-invalid-secret"
}'

# Test 6: Missing required fields (should fail)
test_webhook "Missing Fields (should fail)" '{
  "secret":"MY_ULTRA_SECRET",
  "event":"ENTRY",
  "side":"BUY"
}'

# Test 7: Indian equity with different precision
test_webhook "ENTRY Indian Equity" '{
  "secret":"MY_ULTRA_SECRET",
  "event":"ENTRY",
  "side":"BUY",
  "symbol":"NSE:RELIANCE",
  "tf":"5",
  "price":2450.75,
  "sigHigh":2460.0,
  "sigLow":2440.0,
  "sl":2430.0,
  "t1":2480.0,
  "tag":"nse-reliance-123"
}'

echo "🔍 Health Check:"
health_response=$(curl -s "$BASE_URL/health")
echo "📊 Health: $health_response"
echo ""

echo "🎯 Test Summary:"
echo "• Tested ENTRY/TARGET1/STOPLOSS events"
echo "• Tested both BUY and SELL sides"
echo "• Tested crypto and equity symbols"
echo "• Tested error cases (invalid secret, missing fields)"
echo ""
echo "📊 View results in dashboard: $BASE_URL/dashboard"
echo "📜 Check logs: tail -f server.log"
echo ""
echo "💡 For production testing:"
echo "   1. Change BASE_URL to your actual webhook URL"
echo "   2. Update secret to match your Pine script"
echo "   3. Use real symbol names from your trading"
echo ""
