# Aria VoiceOrder — Magento module

Magento **does not** expose REST APIs to change a placed order’s shipping address or line quantities. Core `PUT /V1/orders/{id}` rejects those fields.

This module adds two endpoints for the Python voice agent:

| Method | Path | Purpose |
|--------|------|---------|
| `PUT` | `/rest/{store_view}/V1/aria-voice/orders/{entity_id}/shipping-address` | Update shipping address |
| `PUT` | `/rest/{store_view}/V1/aria-voice/orders/items/{item_id}/quantity` | Change `qty_ordered` |

Responses are a JSON **string** body with `success`, `message`, and optional `delivery_address` / `total`.

## Install

1. Copy `Aria/VoiceOrder` into your Magento root:

   ```text
   app/code/Aria/VoiceOrder/
   ```

2. Enable and compile:

   ```bash
   bin/magento module:enable Aria_VoiceOrder
   bin/magento setup:upgrade
   bin/magento cache:flush
   ```

3. **System → Integrations** — edit the integration used by `MAGENTO_ACCESS_TOKEN` and allow resource **Aria voice order edits** (under Sales).

4. Re-save / re-activate the integration if the token was created before the module was installed.

## Python app

On the Magento deployment set:

```bash
DATA_SOURCE=magento
ORDER_DESK_ENABLED=false
```

Address and quantity changes during outbound confirmation then go **straight to Magento** (no `order_desk.json`).

## Verify

```bash
curl -X PUT "https://YOUR-STORE/rest/thamesmead/V1/aria-voice/orders/58087/shipping-address" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"address1":"99 Test St","city":"London","postcode":"SW1A 1AA","countryId":"GB"}'
```

If you get `404 Request does not match any route`, the module is not installed or cache was not flushed.
