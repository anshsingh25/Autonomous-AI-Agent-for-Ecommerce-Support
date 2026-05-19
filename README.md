# Autonomous AI Agent for Ecommerce Support

Voice support agent (**Aria**) for **badlavo** — realtime phone and browser conversations powered by **Gemini Live**, **Pipecat**, and **FastAPI**, with **Shopify** order lookup.

## Features

- Browser voice test at `/browser` (mic → Gemini → speaker)
- Twilio telephony integration (optional)
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

## Configuration

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Google AI / Gemini API key |
| `DATA_SOURCE` | `mock`, `shopify`, or `api` |
| `SHOPIFY_SHOP_DOMAIN` | `your-store.myshopify.com` |
| `SHOPIFY_ACCESS_TOKEN` | Admin API access token |
| `PUBLIC_WS_URL` | Public `wss://` URL for Twilio (e.g. ngrok) |

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
