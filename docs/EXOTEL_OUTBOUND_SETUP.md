# Exotel outbound — fix “call drops when I answer”

If the phone rings but **disconnects right after you pick up**, Exotel is not connecting audio to your server. Follow this setup once.

## Why it disconnects

1. **Wrong `Url` on Calls/connect** — must be an **ExoML app** link, not your ngrok HTTPS URL.
2. **No Voicebot applet** in that app — audio needs a **Voicebot** (bidirectional stream).
3. **ngrok stopped** or wrong port — Exotel cannot open `wss://YOUR-NGROK/voice/ws`.
4. **Dynamic URL not set** on Voicebot — must point to `/voice/outbound/exotel-answer`.
5. **Wrong response shape** — HTTPS dynamic URL must return JSON `{"url":"wss://..."}`, not plain text.

## Step 1 — App Bazaar flow

1. Exotel Dashboard → **App Bazaar** → create app e.g. `order-confirmation`.
2. Add one applet: **Voicebot** (bidirectional).
3. **URL type:** Dynamic (HTTPS).
4. **URL:**

   ```
   https://YOUR-NGROK-ID.ngrok-free.app/voice/outbound/exotel-answer
   ```

   (Same host as `PUBLIC_HTTP_URL` in `.env`, no `session_id` in the dashboard — it comes from `CustomField`.)

5. **Sample rate:** 8000 Hz.
6. Save and copy the **App ID** from the flow / ExoML start link.

ExoML start URL looks like:

```
http://my.exotel.com/voiceagent5/exoml/start_voice/YOUR_APP_ID
```

## Step 2 — `.env`

```env
TELEPHONY_PROVIDER=exotel
EXOTEL_ACCOUNT_SID=voiceagent5
EXOTEL_API_KEY=...
EXOTEL_API_TOKEN=...
EXOTEL_CALLER_ID=09513886363
EXOTEL_API_HOST=https://api.exotel.com

PUBLIC_WS_URL=wss://YOUR-NGROK-ID.ngrok-free.app
PUBLIC_HTTP_URL=https://YOUR-NGROK-ID.ngrok-free.app

EXOTEL_OUTBOUND_APP_ID=YOUR_APP_ID
# or full URL:
# EXOTEL_OUTBOUND_FLOW_URL=http://my.exotel.com/voiceagent5/exoml/start_voice/YOUR_APP_ID
```

## Step 3 — Keep running

```bash
ngrok http 7860
PYTHONPATH=. uvicorn app.main:app --host 0.0.0.0 --port 7860
```

## Step 4 — Test

```bash
curl -X POST http://127.0.0.1:7860/api/outbound/confirm-order \
  -H "Content-Type: application/json" \
  -d '{"order_id": "1588", "phone": "6358762776"}'
```

When you answer, server logs should show:

```
Exotel voicebot URL fetch ... session_id=...
Exotel voicebot WSS for session_id=... → wss://...
WebSocket accepted (session_id=...)
```

Our `/voice/outbound/exotel-answer` returns `{"url":"wss://YOUR-NGROK/voice/ws?session_id=...&sample-rate=8000"}` as required by Exotel.

If you only see `Exotel outbound connect` and **no** `WebSocket accepted`, the Voicebot URL or App ID is still wrong.

## Agent voice cuts off mid-sentence

If Aria stops talking halfway through a sentence:

- Server ignores spurious **Gemini `interrupted`** signals on outbound calls.
- Exotel **`clear`** is not sent on interruption (that event flushes all queued audio).
- Customer mic stays muted until **`TTSStoppedFrame`** (not brief 0.35s gaps between audio chunks).

Restart uvicorn after pulling these changes. Some lag from ngrok + Gemini Live + 8 kHz PSTN is normal.

## Inbound vs outbound

| | Inbound | Outbound |
|---|---------|----------|
| Customer calls | 09513886363 | System calls customer |
| Voicebot WSS | `wss://HOST/voice/ws?sample-rate=8000` | `wss://HOST/voice/ws?session_id=...` |
| ExoML on API dial | `EXOTEL_INBOUND_APP_ID` or same as outbound | `EXOTEL_OUTBOUND_APP_ID` |
| Voicebot dynamic HTTPS | `/voice/inbound/exotel-answer` | `/voice/outbound/exotel-answer` |

**Inbound disconnect on answer:** same root cause — `POST /api/inbound/test-call` must use the **ExoML start URL**, not the ngrok HTTPS answer URL. The Voicebot applet in that flow must call your dynamic URL; it returns JSON `{"url":"wss://..."}`.

You can use one App Bazaar flow for both: point Voicebot to `/voice/outbound/exotel-answer` — without `CustomField` it returns the inbound WSS URL automatically.

Test inbound:

```bash
curl -X POST http://127.0.0.1:7860/api/inbound/test-call \
  -H "Content-Type: application/json" \
  -d '{"phone": "6358762776"}'
```

Logs when working: `Exotel voicebot (inbound — no session_id)` then `WebSocket accepted`.
