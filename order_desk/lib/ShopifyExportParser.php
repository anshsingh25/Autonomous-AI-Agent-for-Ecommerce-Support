<?php

declare(strict_types=1);

namespace OrderDesk;

/**
 * Parses Shopify Admin "Export orders" CSV or Excel files.
 * One row per line item; orders are grouped by Name (#1600) or Id.
 */
final class ShopifyExportParser
{
    /** @var list<string> */
    private array $headerNorms = [];

    public function parseFile(string $path, string $originalName): array
    {
        $ext = strtolower(pathinfo($originalName, PATHINFO_EXTENSION));
        if ($ext === 'csv') {
            return $this->parseCsv($path);
        }
        if (in_array($ext, ['xlsx', 'xls', 'ods'], true)) {
            return $this->parseSpreadsheet($path);
        }
        throw new \InvalidArgumentException(
            'Unsupported file type. Export from Shopify as CSV or Excel (.xlsx).'
        );
    }

    public function parseCsv(string $path): array
    {
        $handle = fopen($path, 'r');
        if ($handle === false) {
            throw new \RuntimeException('Could not read uploaded file.');
        }
        $headers = fgetcsv($handle, 0, ',', '"', '\\');
        if ($headers === false || $headers === [null]) {
            fclose($handle);
            throw new \InvalidArgumentException('CSV file is empty or has no header row.');
        }
        $this->buildHeaderMap($headers);
        $orders = [];
        while (($row = fgetcsv($handle, 0, ',', '"', '\\')) !== false) {
            if ($this->rowIsEmpty($row)) {
                continue;
            }
            $this->mergeRow($orders, $this->assocRow($row));
        }
        fclose($handle);
        return array_values($orders);
    }

    public function parseSpreadsheet(string $path): array
    {
        if (!class_exists(\PhpOffice\PhpSpreadsheet\IOFactory::class)) {
            throw new \RuntimeException(
                'Excel support requires Composer. Run: cd order_desk && composer install — or export CSV from Shopify.'
            );
        }
        $spreadsheet = \PhpOffice\PhpSpreadsheet\IOFactory::load($path);
        $sheet = $spreadsheet->getActiveSheet();
        $rows = $sheet->toArray(null, true, true, false);
        if ($rows === []) {
            throw new \InvalidArgumentException('Spreadsheet is empty.');
        }
        $headersRow = array_shift($rows);
        $headers = array_map(static function ($v): string {
            $label = trim(is_scalar($v) ? (string) $v : '');
            return preg_replace('/^\xEF\xBB\xBF/', '', $label) ?? $label;
        }, $headersRow);
        $this->buildHeaderMap($headers);
        $orders = [];
        foreach ($rows as $row) {
            if (!is_array($row) || $this->rowIsEmpty($row)) {
                continue;
            }
            $this->mergeRow($orders, $this->assocRow($row));
        }
        return array_values($orders);
    }

    private function buildHeaderMap(array $headers): void
    {
        $this->headerNorms = [];
        foreach ($headers as $h) {
            $label = trim((string) $h);
            if ($label === '') {
                $this->headerNorms[] = '';
                continue;
            }
            // UTF-8 BOM on first Shopify column breaks “Name” → column not found.
            $label = preg_replace('/^\xEF\xBB\xBF/', '', $label) ?? $label;
            $this->headerNorms[] = $this->normalizeKey($label);
        }
    }

    private function normalizeKey(string $label): string
    {
        return strtolower(preg_replace('/[^a-z0-9]+/i', '_', trim($label)) ?? '');
    }

    private function assocRow(array $cells): array
    {
        $out = [];
        foreach ($this->headerNorms as $i => $norm) {
            if ($norm === '') {
                continue;
            }
            $cell = trim((string) ($cells[$i] ?? ''));
            if ($cell !== '') {
                $cell = preg_replace('/^\xEF\xBB\xBF/', '', $cell) ?? $cell;
            }
            $out[$norm] = $cell;
        }
        return $out;
    }

    /** @param array<string, string> $ship */
    public static function formatAddress(array $ship): string
    {
        $parts = array_filter([
            $ship['address1'] ?? '',
            $ship['address2'] ?? '',
            $ship['city'] ?? '',
            $ship['province'] ?? '',
            $ship['zip_code'] ?? '',
            $ship['country'] ?? '',
        ], static fn ($p) => trim((string) $p) !== '');
        return implode(', ', $parts);
    }

    private function rowIsEmpty(array $row): bool
    {
        foreach ($row as $cell) {
            if (trim((string) $cell) !== '') {
                return false;
            }
        }
        return true;
    }

    private function mergeRow(array &$orders, array $row): void
    {
        $key = $this->orderKey($row);
        if ($key === '') {
            return;
        }
        if (!isset($orders[$key])) {
            $orders[$key] = $this->baseOrder($row, $key);
        }
        $this->appendLineItem($orders[$key], $row);
        $this->fillMissingScalars($orders[$key], $row);
    }

    private function orderKey(array $row): string
    {
        // Shopify “All orders” CSV: usually “Name” (#1601). Some locales use Order number.
        $name = $this->val(
            $row,
            'name',
            'order',
            'order_name',
            'order_number',
            'order_no',
            'order__name',
            'shopify_order_name',
        );
        if ($name !== '') {
            return ltrim($name, '#');
        }
        $id = $this->val($row, 'id', 'order_id', 'shopify_order_id');
        return $id !== '' ? $id : '';
    }

    private function baseOrder(array $row, string $key): array
    {
        $phone = $this->bestPhone($row);
        $shipping = $this->shippingBlock($row);
        $customerName = $shipping['name'] !== ''
            ? $shipping['name']
            : $this->val($row, 'billing_name');

        $total = $this->parseMoney($this->val($row, 'total'));
        $placedAt = $this->val($row, 'created_at', 'paid_at');

        return [
            'order_id' => $key,
            'shopify_order_id' => $this->parseInt($this->val($row, 'id')),
            'customer_name' => $customerName !== '' ? $customerName : 'Customer (order #' . $key . ')',
            'phone' => $phone,
            'email' => $this->val($row, 'email', 'contact_email'),
            'status' => 'processing',
            'financial_status' => strtolower($this->val($row, 'financial_status', 'payment_status')),
            'fulfillment_status' => strtolower($this->val($row, 'fulfillment_status') ?: 'unfulfilled'),
            'total_inr' => $total,
            'currency' => $this->val($row, 'currency') ?: 'INR',
            'items' => [],
            'line_items' => [],
            'delivery_address' => self::formatAddress($shipping),
            'shipping' => $shipping,
            'tags' => $this->parseTags($this->val($row, 'tags')),
            'placed_at' => $placedAt,
            'call_status' => 'pending',
            'notes' => $this->val($row, 'notes', 'note'),
            'editable' => true,
            'pii_limited' => $phone === '' && $shipping['address1'] === '',
            'imported_at' => gmdate('c'),
            'updated_at' => gmdate('c'),
            'synced_at' => gmdate('c'),
            'dirty' => false,
        ];
    }

    private function appendLineItem(array &$order, array $row): void
    {
        $qty = (int) ($this->val($row, 'lineitem_quantity', 'line_item_quantity', 'quantity') ?: '1');
        $title = $this->val($row, 'lineitem_name', 'line_item_name', 'lineitem_title');
        if ($title === '') {
            return;
        }
        $spoken = $title . ($qty > 1 ? ' x' . $qty : ' x1');
        if (!in_array($spoken, $order['items'], true)) {
            $order['items'][] = $spoken;
        }
        $order['line_items'][] = [
            'line_item_id' => null,
            'title' => $title,
            'quantity' => max(1, $qty),
            'variant_title' => $this->val($row, 'lineitem_variant', 'variant') ?: null,
            'sku' => $this->val($row, 'lineitem_sku', 'sku') ?: null,
            'product_id' => null,
            'variant_id' => null,
            'price' => $this->parseMoney($this->val($row, 'lineitem_price', 'price')) ?: null,
        ];
    }

    private function fillMissingScalars(array &$order, array $row): void
    {
        if ($order['phone'] === '') {
            $order['phone'] = $this->bestPhone($row);
        }
        if ($order['email'] === '') {
            $order['email'] = $this->val($row, 'email');
        }
        $ship = $this->shippingBlock($row);
        if ($ship['address1'] !== '' || $ship['phone'] !== '') {
            $order['shipping'] = $this->mergeShipping($order['shipping'], $ship);
            $order['delivery_address'] = self::formatAddress($order['shipping']);
            $order['pii_limited'] = $order['phone'] === '' && $order['shipping']['address1'] === '';
        }
        if ($order['customer_name'] === '' || str_starts_with($order['customer_name'], 'Customer (order #')) {
            if ($ship['name'] !== '') {
                $order['customer_name'] = $ship['name'];
            }
        }
        $fs = strtolower($this->val($row, 'financial_status'));
        if ($fs !== '') {
            $order['financial_status'] = $fs;
        }
        $ff = strtolower($this->val($row, 'fulfillment_status'));
        if ($ff !== '') {
            $order['fulfillment_status'] = $ff;
        }
        $tags = $this->parseTags($this->val($row, 'tags'));
        if ($tags !== []) {
            $order['tags'] = array_values(array_unique(array_merge($order['tags'], $tags)));
        }
    }

    private function mergeShipping(array $a, array $b): array
    {
        foreach ($b as $k => $v) {
            if ($v !== '' && ($a[$k] ?? '') === '') {
                $a[$k] = $v;
            }
        }
        return $a;
    }

    private function shippingBlock(array $row): array
    {
        return [
            'name' => $this->val($row, 'shipping_name'),
            'phone' => $this->val($row, 'shipping_phone'),
            'address1' => $this->val($row, 'shipping_address1', 'shipping_street'),
            'address2' => $this->val($row, 'shipping_address2'),
            'city' => $this->val($row, 'shipping_city'),
            'province' => $this->val($row, 'shipping_province', 'shipping_province_name'),
            'zip_code' => $this->val($row, 'shipping_zip', 'shipping_postal_code'),
            'country' => $this->val($row, 'shipping_country'),
        ];
    }

    private function bestPhone(array $row): string
    {
        foreach (['shipping_phone', 'phone', 'billing_phone', 'contact_phone'] as $k) {
            $p = $this->val($row, $k);
            if ($p !== '') {
                return $p;
            }
        }
        return '';
    }

    private function val(array $row, string ...$keys): string
    {
        foreach ($keys as $key) {
            $norm = $this->normalizeKey($key);
            if (isset($row[$norm]) && trim((string) $row[$norm]) !== '') {
                return trim((string) $row[$norm]);
            }
        }
        return '';
    }

    private function parseMoney(string $raw): float
    {
        $clean = preg_replace('/[^\d.\-]/', '', $raw) ?? '0';
        return (float) ($clean !== '' ? $clean : '0');
    }

    private function parseInt(string $raw): ?int
    {
        $digits = preg_replace('/\D/', '', $raw) ?? '';
        if ($digits === '') {
            return null;
        }
        return (int) $digits;
    }

    /** @return list<string> */
    private function parseTags(string $raw): array
    {
        if ($raw === '') {
            return [];
        }
        $parts = preg_split('/\s*,\s*/', $raw) ?: [];
        return array_values(array_filter(array_map('trim', $parts)));
    }
}
