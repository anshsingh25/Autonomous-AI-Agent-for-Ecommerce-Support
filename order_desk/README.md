# Order desk (PHP)

Import **Shopify Admin → Orders → Export** (CSV or Excel) and browse customer phone, email, and shipping address in a simple web UI. Saves to `data/order_desk.json` so your voice agents can use PII that the Shopify Admin API often omits on Basic plans.

## Setup

```bash
cd order_desk
# Optional — only needed for .xlsx Excel files (CSV works without this)
composer install
php -S 127.0.0.1:8090 -t public
```

Open **http://127.0.0.1:8090/**

## Workflow

1. In Shopify Admin: **Orders** → **Export** → save CSV to **Downloads** (or copy the download link).
2. On the order desk page:
   - Set **Local file path** (e.g. `~/Downloads/orders_export.csv`) or **Export file URL**, then click **Download & import**,  
   - **or** check **Use newest export in Downloads**,  
   - **or** upload the file manually.
3. Enter an **Order number** (e.g. `1600`) and click **Show fields** — all customer/shipping values appear in the form fields.
4. Click a table row to load the same fields.
5. Agents or scripts can call the JSON API:

```bash
# Single order (for outbound call prep)
curl "http://127.0.0.1:8090/api.php?order_id=1600"

# Search
curl "http://127.0.0.1:8090/api.php?q=Ansh&limit=20"
```

## Data file

Imports merge into:

`../data/order_desk.json`

Re-importing the same order updates empty fields with newer export values (phone, address, etc.).

## Troubleshooting — new order (e.g. #1601) not in the desk

Imports are **only from the CSV/Excel file** — there is **no live Shopify API** in PHP.

1. **Export view / filters** — Shopify only exports orders in the **current Orders view** (e.g. date range or “Unfulfilled”). After checkout, open **Orders** with a view that includes the new order (or **All time**), then **Export** again.
2. **“Newest in Downloads”** — We prefer `orders_export*.csv`. Other `.csv` files with a newer **file date** can win; remove stale exports or set an explicit **Local file path** to the file you just downloaded.
3. **BOM on first column** — Some CSVs hide the **Name** header; the parser strips UTF-8 BOM. Pull the latest code and re-import.

After import, check the green message: **it lists order numbers found in that file**. If #1601 is not listed, it is not in the export.

## Requirements

- PHP 8.1+
- [Composer](https://getcomposer.org/) for Excel (`.xlsx`) support
- CSV works without extra configuration once Composer autoload is installed

## Voice agent integration

When `ORDER_DESK_ENABLED=true` (default), the Python agent:

- Loads **phone, name, address, items, total** from `data/order_desk.json` for outbound calls and `get_order_status`
- Saves **address / quantity / cancel** edits back to the same JSON file (visible in this UI after refresh)
- Uses Shopify API only for tags/dial when `DATA_SOURCE=shopify` — not for protected customer PII

### Shopify two-way sync (Python + `DATA_SOURCE=shopify`)

When the FastAPI app has credentials and `ORDER_DESK_SYNC_SECRET` set:

| HTTP (Python) | Purpose |
|---------------|---------|
| `POST /api/order-desk/refresh-from-shopify` | Pull latest line items + `total_inr` from Shopify into `order_desk.json` |
| `POST /api/order-desk/sync-quantity` | Set line qty in Shopify, then merge desk from Shopify (correct pricing) |
| `POST /api/order-desk/push-save-to-shopify` | After PHP saves a row: push shipping to Shopify, append order **note**, merge totals/lines |

Local PHP hook (optional): set environment variables before `php -S`:

```bash
export ORDER_DESK_PUSH_SYNC_URL=http://127.0.0.1:7860/api/order-desk/push-save-to-shopify
export ORDER_DESK_SYNC_SECRET=your-secret   # same as ORDER_DESK_SYNC_SECRET in Python .env
```

Outbound **voice** quantity and address tools already try Shopify first when desk is primary, then refresh desk from Shopify.

Import or edit orders in this UI, then run confirmation calls — the agent uses desk data only.

## Security

This tool is intended for **local / trusted network** use. Do not expose it on the public internet without authentication.
