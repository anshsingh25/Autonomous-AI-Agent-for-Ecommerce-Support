# Autonomous AI Agent for Ecommerce Support

Voice support agent (**Aria**) for **badlavo** — realtime phone and browser conversations powered by **Gemini Live**, **Pipecat**, and **FastAPI**, with **Shopify** order lookup.

## Features

- **Inbound support** — customer calls you; Aria helps with orders (existing `/voice/incoming` flow)
- **Outbound order confirmation** — Aria calls the customer to confirm order details (`POST /api/outbound/confirm-order`)
- Browser voice test at `/browser` (mic → Gemini → speaker)
- Telephony via **Twilio** or **Exotel** (Exotel recommended for Indian numbers)
- Shopify order status, tracking, refunds, cancellation
- Delivery address read from Shopify shipping address
- Senior call protocols (32 scenarios) in `app/ai/system_prompt_badlavo.py`

## Quick start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set GEMINI_API_KEY and DATA_SOURCE (mock | shopify)

export NLTK_DATA="$(pwd)/nltk_data"
bash scripts/setup_nltk.sh   # first time only

PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 7860
```

Open **http://127.0.0.1:7860/browser** and click **Start conversation**.

## Shopify auto confirmation (outbound)

When `DATA_SOURCE=shopify` and `AUTO_CONFIRMATION_ENABLED=true`:

1. Shopify sends `orders/paid` or `orders/create` to your server.
2. If the order is **unfulfilled** and has a phone number, Aria places an outbound confirmation call.
3. After the call, Shopify tags are updated (existing tags are **never removed**, only appended):

| Customer response | Shopify action |
|-------------------|----------------|
| yes, confirm, okay, proceed, haan, correct | Tags: `Proceed`, `AI Confirmed` |
| cancel, no, reject | Cancel order + tag `Cancelled` |
| no answer / busy | Tag `Retry Call` |

**Webhook setup** (Shopify Admin → Settings → Notifications → Webhooks):

- Event: `Order payment` → URL: `https://YOUR-HOST/api/shopify/webhooks/orders-paid`
- Optional: `Order creation` → `https://YOUR-HOST/api/shopify/webhooks/orders-create`
- Set `SHOPIFY_WEBHOOK_SECRET` in `.env` to the secret Shopify shows.

**Manual scan** (cron): `POST /api/shopify/confirmation/scan-unfulfilled`

Outbound only dials orders that are **unfulfilled** and **not** tagged `AI Confirmed` / `confirmed` / `Proceed` (with confirm) / `Cancelled` / `AI Call In Progress`.

- Optional **Shopify webhook filter:** set `CONFIRMATION_WEBHOOK_REQUIRE_TAG` (e.g. `Call AI Confirm`) — `orders/create` and `orders/paid` only auto-dial when that tag appears on Shopify **or** on the row in order desk (`data/order_desk.json`). Useful with Shopify Flow: add tag → webhook fires → dial only tagged rows.
- **Tag batch dial:** `GET /api/shopify/confirmation/tagged-pending?tag=Your+Tag` — list phones + orders; `POST /api/shopify/confirmation/dial-tagged` with JSON `{"tag": "Your Tag", "limit": 20, "dry_run": false}` — call every matching row that has a number (desk or Shopify).

- **Web desk:** [http://127.0.0.1:7860/browser/confirmation-desk](http://127.0.0.1:7860/browser/confirmation-desk) — refresh unconfirmed orders, **Call customer** per row, or **Call all ready**
- `GET /api/shopify/confirmation/pending` — list orders that still need a call
- `POST /api/shopify/confirmation/scan-unfulfilled?dry_run=true` — preview who would be dialed
- `POST /api/outbound/confirm-order` — dial one order (checks tags first; pass `"phone"` if missing on order)

**Phone on Basic Shopify:** Admin may show the customer phone, but the Admin API often omits it (protected customer data / incomplete address). Pass `"phone": "98XXXXXXXX"` in the JSON body until [protected customer data access](https://shopify.dev/docs/apps/launch/protected-customer-data) is enabled for your app.

**Shopify app scopes**: `read_orders`, `write_orders` (tags + cancel).

## Outbound order confirmation call (manual API)

After a customer places an order, trigger a confirmation call:

```bash
curl -X POST http://127.0.0.1:7860/api/outbound/confirm-order \
  -H "Content-Type: application/json" \
  -d '{"order_id": "1569"}'
```

Optional: `"phone": "+919876543210"` if the order has no phone on file.

1. Set `TELEPHONY_PROVIDER=twilio` or `exotel` (use `mock` to test without dialing).
2. Set `PUBLIC_WS_URL` to your ngrok host (e.g. `wss://xxxx.ngrok-free.app`).
3. For **Twilio**: configure `TWILIO_*` — outbound uses `POST /voice/outbound/answer`.
4. For **Exotel** (India): configure `EXOTEL_*` + **`EXOTEL_OUTBOUND_APP_ID`** — see [docs/EXOTEL_OUTBOUND_SETUP.md](docs/EXOTEL_OUTBOUND_SETUP.md) if the call drops when you answer.

Aria uses a dedicated **outbound prompt** (not the inbound support script): confirms items, total, and delivery address.

## Configuration

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google AI / Gemini API key |
| `DATA_SOURCE` | `mock`, `shopify`, or `api` |
| `SHOPIFY_SHOP_DOMAIN` | `your-store.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | Admin API access token |
| `AUTO_CONFIRMATION_ENABLED` | Shopify `orders/*` webhooks enqueue confirmation dials when `true` |
| `CONFIRMATION_WEBHOOK_REQUIRE_TAG` | If set (e.g. `Call AI Confirm`), webhooks dial only orders with this tag |
| `TELEPHONY_PROVIDER` | `mock`, `twilio`, or `exotel` |
| `PUBLIC_WS_URL` | Public `wss://` base URL (e.g. ngrok) |
| `PUBLIC_HTTP_URL` | HTTPS webhooks (optional; inferred from `PUBLIC_WS_URL`) |
| `EXOTEL_*` | Exotel credentials for India outbound/inbound |

See `.env.example` for all options (including **`GEMINI_OUTBOUND_VAD_*`**, **`GEMINI_OUTBOUND_TRANSCRIPTION_*`**, and **`TOOL_HOLD_PAUSE_SEC`** — gap before order tools run after the hold line).

## Architecture (quick map)

| Area | Purpose |
|------|---------|
| `app/main.py` | FastAPI app, routers, `/health`, ngrok warning on startup |
| `app/routes/voice.py` | `/voice/incoming`, `/voice/ws`, Exotel **`/voice/outbound/exotel-answer`**, **`/voice/inbound/exotel-answer`** |
| `app/routes/outbound.py` | `POST /api/outbound/confirm-order` |
| `app/routes/inbound.py` | `POST /api/inbound/test-call` (Exotel inbound support dial) |
| `app/routes/shopify_webhooks.py` | Webhooks (HMAC optional), confirmation desk helpers |
| `app/routes/order_desk_sync.py` | Secured order desk ↔ Shopify sync (lines, totals, shipping, notes) |
| `app/services/order_confirmation_service.py` | Dial orchestration + Shopify tagging |
| `app/services/outbound_call_service.py` | Build `OutboundCallContext`, Twilio / Exotel `Calls/connect` |
| `app/services/call_context_store.py` | **In-memory** outbound sessions (**2 h TTL**); replace with Redis for multi-worker |
| `app/ai/pipeline.py` | Browser vs PSTN transports; **`CustomField`** / UUID session resolution |
| `app/ai/session.py` | Gemini Live pipeline + outbound VAD from settings |
| `order_desk/` + `ORDER_DESK_*` | PHP UI + JSON for PII/order lines when Shopify omits phone |

## Production / operations checklist

- **Never commit `.env`** — webhook secrets and telephony credentials are plaintext in config.
- **Shopify:** set **`SHOPIFY_WEBHOOK_SECRET`** — if unset, webhook HMAC verification is skipped (fine for dev only).
- **Public HTTP + WSS:** `PUBLIC_HTTP_URL` / `PUBLIC_WS_URL` must stay on the **same live tunnel/domain** (`GET /voice/exotel-setup`; startup warns on ngrok mismatch).
- **Outbound answer drops:** Voicebot applet → dynamic HTTPS **`/voice/outbound/exotel-answer`**, **`CustomField`** must reach callback (see `docs/EXOTEL_OUTBOUND_SETUP.md`).
- **Unauthenticated HTTP:** `POST /api/outbound/confirm-order`, `GET /api/orders/*`, `POST /api/shopify/confirmation/*`, **`POST …/finalize/{session_id}`** assume a **trusted network** or reverse-proxy auth — add your own gateway API key before exposing to the Internet.
- **Scale-out:** outbound context is **per-process RAM** — run one worker behind the load balancer, or port `call_context_store` to Redis.
- **`CORS`** is `*` with credentials — tighten `allow_origins` when you expose a branded web origin only.

## Project layout

```
app/
  main.py           # FastAPI entry
  ai/               # Gemini session, prompts, tools
  routes/           # Browser + Twilio WebSockets
  services/         # Order business logic
  database/         # Mock, Shopify, HTTP store adapters
static/browser/     # Voice test UI
scripts/            # NLTK setup, Shopify verify
```

## License

MIT (add a LICENSE file if you publish formally).
