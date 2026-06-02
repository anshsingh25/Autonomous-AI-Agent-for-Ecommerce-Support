# Magento 2.4 — separate site (Shopify unchanged)

Run a **second deployment** of this app for your Magento store. Keep your existing Shopify `.env` as-is on the Shopify server; use `DATA_SOURCE=magento` only on the Magento instance.

## Multiple stores (Websites / Store Views)

Magento REST uses **one store view code per URL**: `https://your-site.com/rest/{store_view_code}/V1/...`

From **Stores → Settings → All Stores**, use the **Store View** code (right-hand column), for example:

| Store view | Code for API / `.env` |
|------------|------------------------|
| Ades Thamesmead | `thamesmead` |
| Ades Charlton | `charlton` |
| Ades Manchester | `manchester` |
| Ades Online Delivery | `ades` |
| Wholesale Store | `main_wholesale_store_view` |

Do **not** use Website codes (`base`, `website1`, `wholesale`, …) unless Magento docs for your install say otherwise.

**Option A — all stores (simplest):**

```bash
MAGENTO_STORE_CODE=all
```

**Option B — search each store view (explicit):**

```bash
MAGENTO_STORE_CODES=thamesmead,charlton,ades,manchester,main_wholesale_store_view
```

**Option C — one Aria deployment per brand** (separate `.env` each with a single `MAGENTO_STORE_CODE`).

In webhooks, send `"store_view_code": "charlton"` (or `ades`, etc.) so the app hits the right scope first.

## Requirements

- Magento **2.4.8** (tested target; REST API compatible with 2.4.x)
- Admin integration with access to:
  - `Magento_Sales::sales`
  - `Magento_Sales::sales_operation` (cancel)
  - `Magento_Sales::shipment` (tracking, optional)

Create under **System → Integrations → Add New Integration**, activate, and copy the **Access Token**.

## Environment (Magento instance only)

```bash
DATA_SOURCE=magento
MAGENTO_BASE_URL=https://your-magento-store.com
MAGENTO_ACCESS_TOKEN=your_integration_token
MAGENTO_STORE_CODE=all
# Multi-store (your screenshot — use Store View codes, not Website codes):
# MAGENTO_STORE_CODES=thamesmead,charlton,ades,manchester,main_wholesale_store_view
MAGENTO_WEBHOOK_SECRET=long-random-string
STORE_BRAND_NAME=Your Magento Store Name

# Same voice stack as Shopify site
GEMINI_API_KEY=...
TELEPHONY_PROVIDER=exotel
PUBLIC_WS_URL=wss://...

# Order desk is optional for Magento (API returns full PII). Prefer live Magento:
ORDER_DESK_ENABLED=false

# Install magento_module/Aria/VoiceOrder on Magento for address/qty edits during calls.
AUTO_CONFIRMATION_ENABLED=true
```

Do **not** set `SHOPIFY_*` on this deployment unless you intentionally share code paths.

## Confirmation markers (instead of Shopify tags)

Magento orders do not have Shopify-style tags. This app writes **status history comments** prefixed with `[AI]`, for example:

- `[AI] AI Call In Progress`
- `[AI] Proceed` / `[AI] AI Confirmed`
- `[AI] Retry Call`
- `[AI] Cancelled`

The order desk `tags` array still works for batch filters (`CONFIRMATION_WEBHOOK_REQUIRE_TAG`).

## Webhook (new order → auto-dial)

Magento has no built-in “orders/paid” webhook like Shopify. Point your Magento module or middleware to:

```http
POST https://YOUR-HOST/api/magento/webhooks/order-created
X-Magento-Webhook-Secret: <MAGENTO_WEBHOOK_SECRET>
Content-Type: application/json

{
  "increment_id": "000000123",
  "entity_id": 456,
  "store_view_code": "charlton",
  "telephone": "+447700900123"
}
```

At minimum send `increment_id` or `entity_id`. The app loads the full order from REST when needed.

## Manual / cron APIs

| Endpoint | Purpose |
|----------|---------|
| `POST /api/outbound/confirm-order` | Dial one order (same as Shopify site) |
| `GET /api/magento/confirmation/pending` | List open orders needing a call |
| `POST /api/magento/confirmation/scan-open?dry_run=true` | Preview cron batch dial |
| `POST /api/magento/confirmation/finalize/{session_id}` | Apply outcome after call |

## Verify connection

```bash
PYTHONPATH=. python scripts/verify_magento.py
```

## UK English (no Hindi / language question)

When `DATA_SOURCE=magento`, outbound confirmation **does not** ask “Which language are you comfortable with — English or Hindi?”. Aria uses **British English** only and goes straight to: intro → “do you have a minute to confirm before dispatch?”

Inbound support on the same deployment also avoids language-preference prompts.

## Inbound support (customer calls you)

On the Magento deployment, inbound uses the **same** telephony routes as Shopify:

- Exotel Voicebot → `https://YOUR-HOST/voice/inbound/exotel-answer` (or Twilio `/voice/incoming`)
- Browser test → `http://127.0.0.1:7860/browser`

With `DATA_SOURCE=magento`, Aria’s inbound tools read from **Magento REST** (and order desk when enabled):

- **Read:** `get_order_status`, `track_order`, `refund_status`
- **Write:** `cancel_order`, `update_delivery_address`, `update_item_quantity` (needs `Aria_VoiceOrder` module)

Set `STORE_BRAND_NAME` if the spoken brand is not badlavo.

## Order edits (address & quantity) — no order desk required

Magento core REST **cannot** update shipping address or line qty on placed orders. Install the bundled module:

1. Copy `magento_module/Aria/VoiceOrder` → `app/code/Aria/VoiceOrder` on your Magento server.
2. `bin/magento module:enable Aria_VoiceOrder && bin/magento setup:upgrade && bin/magento cache:flush`
3. Grant your integration token **Aria voice order edits** (under Sales).

With `ORDER_DESK_ENABLED=false`, outbound confirmation updates go **directly to Magento** via:

- `PUT /rest/{store_view}/V1/aria-voice/orders/{entity_id}/shipping-address`
- `PUT /rest/{store_view}/V1/aria-voice/orders/items/{item_id}/quantity`

See `magento_module/README.md`.

## Order desk (optional)

Only needed if you want a PHP CSV UI backup or Magento omits phone on some orders. When enabled, reads/writes prefer `ORDER_DESK_JSON_PATH` over the API.

## What stays Shopify-only

These routes and sync features are **not** used on `DATA_SOURCE=magento`:

- `/api/shopify/webhooks/*`
- `/api/order-desk/refresh-from-shopify`
- Shopify tag GraphQL / variant swap APIs

Magento cancel, lookup, address, and quantity use Magento REST (+ `Aria_VoiceOrder` for edits).
