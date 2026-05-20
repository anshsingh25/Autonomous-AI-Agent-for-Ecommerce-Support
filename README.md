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
| `TELEPHONY_PROVIDER` | `mock`, `twilio`, or `exotel` |
| `PUBLIC_WS_URL` | Public `wss://` base URL (e.g. ngrok) |
| `PUBLIC_HTTP_URL` | HTTPS webhooks (optional; inferred from `PUBLIC_WS_URL`) |
| `EXOTEL_*` | Exotel credentials for India outbound/inbound |

See `.env.example` for all options.

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
