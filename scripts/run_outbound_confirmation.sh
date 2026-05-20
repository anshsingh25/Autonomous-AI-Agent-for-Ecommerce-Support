#!/usr/bin/env bash
# Outbound order confirmation via Shopify tag check + Exotel dial.
# Usage:
#   ./scripts/run_outbound_confirmation.sh pending
#   ./scripts/run_outbound_confirmation.sh scan          # dry-run
#   ./scripts/run_outbound_confirmation.sh scan-dial     # place calls
#   ./scripts/run_outbound_confirmation.sh call 1588 98XXXXXXXX

set -euo pipefail
BASE="${BASE_URL:-http://127.0.0.1:7860}"

cmd="${1:-pending}"
case "$cmd" in
  pending)
    curl -s "$BASE/api/shopify/confirmation/pending?limit=20" | python3 -m json.tool
    ;;
  scan)
    curl -s -X POST "$BASE/api/shopify/confirmation/scan-unfulfilled?limit=20&dry_run=true" \
      | python3 -m json.tool
    ;;
  scan-dial)
    curl -s -X POST "$BASE/api/shopify/confirmation/scan-unfulfilled?limit=20" \
      | python3 -m json.tool
    ;;
  call)
    ORDER="${2:?order id required}"
    PHONE="${3:-}"
    if [[ -n "$PHONE" ]]; then
      curl -s -X POST "$BASE/api/outbound/confirm-order" \
        -H "Content-Type: application/json" \
        -d "{\"order_id\": \"$ORDER\", \"phone\": \"$PHONE\"}" | python3 -m json.tool
    else
      curl -s -X POST "$BASE/api/outbound/confirm-order" \
        -H "Content-Type: application/json" \
        -d "{\"order_id\": \"$ORDER\"}" | python3 -m json.tool
    fi
    ;;
  *)
    echo "Usage: $0 {pending|scan|scan-dial|call ORDER [PHONE]}"
    exit 1
    ;;
esac
