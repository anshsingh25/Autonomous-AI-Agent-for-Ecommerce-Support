#!/usr/bin/env bash
# Test the ecommerce voice assistant without placing a phone call.
set -euo pipefail

BASE="${1:-http://127.0.0.1:7860}"
echo "=== Testing base URL: $BASE ==="
echo

echo "1. Health"
curl -sf "$BASE/health" | python3 -m json.tool
echo

echo "2. Root (sample orders)"
curl -sf "$BASE/" | python3 -m json.tool
echo

echo "3. Twilio voice webhook (simulated incoming call)"
echo "   Expect TwiML with <Stream url=...>"
curl -sf -X POST "$BASE/voice/incoming" \
  -d "CallSid=CA_test_123&From=%2B919876543210&To=%2B12317902435&CallStatus=ringing"
echo
echo

echo "4. Order APIs"
for path in \
  "/api/orders/ORD-1001/status" \
  "/api/orders/ORD-1001/track" \
  "/api/orders/ORD-1003/refund"; do
  echo "   GET $path"
  curl -sf "$BASE$path" | python3 -m json.tool
  echo
done

echo "5. Cancel order (ORD-1002 — only if still cancellable)"
curl -sf -X POST "$BASE/api/orders/ORD-1002/cancel" \
  -H "Content-Type: application/json" \
  -d '{"reason":"Test cancellation"}' | python3 -m json.tool || echo "   (skipped if already cancelled)"
echo

echo "6. Browser voice UI (open in Chrome/Edge)"
echo "   $BASE/browser"
echo

echo "=== All HTTP tests passed ==="
echo
echo "Voice test in browser (no phone):"
echo "  1. Open http://127.0.0.1:7860/browser"
echo "  2. Click 'Start conversation' and allow microphone"
echo "  3. Ask about order ORD-1001"
echo
echo "Optional: test via public ngrok URL:"
echo "  ./scripts/test_without_call.sh https://YOUR-ID.ngrok-free.app"
