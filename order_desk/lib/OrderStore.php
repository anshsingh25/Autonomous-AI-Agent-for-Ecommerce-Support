<?php

declare(strict_types=1);

namespace OrderDesk;

final class OrderStore
{
    private string $path;

    public function __construct(?string $path = null)
    {
        $this->path = $path ?? dirname(__DIR__, 2) . '/data/order_desk.json';
    }

    public function path(): string
    {
        return $this->path;
    }

    /** @return array{orders: array<string, array<string, mixed>>, meta: array<string, mixed>} */
    public function load(): array
    {
        if (!is_file($this->path)) {
            return ['orders' => [], 'meta' => $this->defaultMeta()];
        }
        $raw = file_get_contents($this->path);
        if ($raw === false || trim($raw) === '') {
            return ['orders' => [], 'meta' => $this->defaultMeta()];
        }
        $data = json_decode($raw, true);
        if (!is_array($data)) {
            return ['orders' => [], 'meta' => $this->defaultMeta()];
        }
        $orders = $data['orders'] ?? [];
        if (!is_array($orders)) {
            $orders = [];
        }
        $meta = $data['meta'] ?? $this->defaultMeta();
        if (!is_array($meta)) {
            $meta = $this->defaultMeta();
        }
        return ['orders' => $orders, 'meta' => $meta];
    }

    /** @param list<array<string, mixed>> $imported */
    public function mergeImport(array $imported, string $sourceFilename): int
    {
        $data = $this->load();
        $now = gmdate('c');
        $added = 0;
        $updated = 0;
        foreach ($imported as $order) {
            $id = (string) ($order['order_id'] ?? '');
            if ($id === '') {
                continue;
            }
            $order['updated_at'] = $now;
            $order['synced_at'] = $now;
            if (!isset($data['orders'][$id])) {
                $order['imported_at'] = $now;
                $data['orders'][$id] = $order;
                $added++;
            } else {
                $data['orders'][$id] = $this->mergeOrder($data['orders'][$id], $order);
                $updated++;
            }
        }
        $data['meta']['last_import_at'] = $now;
        $data['meta']['last_import_file'] = $sourceFilename;
        $data['meta']['order_count'] = count($data['orders']);
        $this->save($data);
        return $added + $updated;
    }

    /** @return array<string, mixed>|null */
    public function get(string $orderId): ?array
    {
        $id = ltrim(trim($orderId), '#');
        $data = $this->load();
        return $data['orders'][$id] ?? null;
    }

    /**
     * Save user-edited fields for one order.
     *
     * @param array<string, mixed> $fields
     * @return array<string, mixed>
     */
    public function update(string $orderId, array $fields): array
    {
        $id = ltrim(trim($orderId), '#');
        if ($id === '') {
            throw new \InvalidArgumentException('Order id is required.');
        }

        $data = $this->load();
        if (!isset($data['orders'][$id])) {
            throw new \InvalidArgumentException('Order #' . $id . ' not found.');
        }

        $order = $data['orders'][$id];
        $newId = ltrim(trim((string) ($fields['order_id'] ?? $id)), '#');
        if ($newId === '') {
            throw new \InvalidArgumentException('Order id cannot be empty.');
        }

        $shipping = is_array($order['shipping'] ?? null) ? $order['shipping'] : [];
        $shipping = [
            'name' => trim((string) ($fields['shipping_name'] ?? $shipping['name'] ?? '')),
            'phone' => trim((string) ($fields['shipping_phone'] ?? $shipping['phone'] ?? '')),
            'address1' => trim((string) ($fields['address1'] ?? $shipping['address1'] ?? '')),
            'address2' => trim((string) ($fields['address2'] ?? $shipping['address2'] ?? '')),
            'city' => trim((string) ($fields['city'] ?? $shipping['city'] ?? '')),
            'province' => trim((string) ($fields['province'] ?? $shipping['province'] ?? '')),
            'zip_code' => trim((string) ($fields['zip_code'] ?? $shipping['zip_code'] ?? '')),
            'country' => trim((string) ($fields['country'] ?? $shipping['country'] ?? '')),
        ];

        $delivery = trim((string) ($fields['delivery_address'] ?? ''));
        if ($delivery === '') {
            $delivery = ShopifyExportParser::formatAddress($shipping);
        }

        $phone = trim((string) ($fields['phone'] ?? ''));
        if ($phone === '' && $shipping['phone'] !== '') {
            $phone = $shipping['phone'];
        }

        $itemsRaw = trim((string) ($fields['items'] ?? ''));
        $items = $itemsRaw !== ''
            ? array_values(array_filter(array_map('trim', preg_split('/[;\n]+/', $itemsRaw) ?: [])))
            : ($order['items'] ?? []);

        $tagsRaw = trim((string) ($fields['tags'] ?? ''));
        $tags = $tagsRaw !== ''
            ? array_values(array_filter(array_map('trim', explode(',', $tagsRaw))))
            : ($order['tags'] ?? []);

        $totalRaw = (string) ($fields['total_inr'] ?? $order['total_inr'] ?? 0);
        $total = (float) preg_replace('/[^\d.\-]/', '', $totalRaw);

        $order['order_id'] = $newId;
        $order['customer_name'] = trim((string) ($fields['customer_name'] ?? $order['customer_name'] ?? ''));
        $order['phone'] = $phone;
        $order['email'] = trim((string) ($fields['email'] ?? $order['email'] ?? ''));
        $order['shipping'] = $shipping;
        $order['delivery_address'] = $delivery;
        $order['items'] = $items;
        $order['tags'] = $tags;
        $order['total_inr'] = $total;
        $order['financial_status'] = trim((string) ($fields['financial_status'] ?? $order['financial_status'] ?? ''));
        $order['fulfillment_status'] = trim((string) ($fields['fulfillment_status'] ?? $order['fulfillment_status'] ?? ''));
        $order['placed_at'] = trim((string) ($fields['placed_at'] ?? $order['placed_at'] ?? ''));
        $order['call_status'] = trim((string) ($fields['call_status'] ?? $order['call_status'] ?? 'pending'));
        $order['notes'] = trim((string) ($fields['notes'] ?? $order['notes'] ?? ''));
        $order['pii_limited'] = $phone === '' && $shipping['address1'] === '';
        $order['dirty'] = true;
        $order['updated_at'] = gmdate('c');

        unset($data['orders'][$id]);
        $data['orders'][$newId] = $order;
        $data['meta']['order_count'] = count($data['orders']);
        $this->save($data);

        $this->notifyPythonPushSync($newId);

        return $order;
    }

    /** @return list<array<string, mixed>> */
    public function list(?string $query = null, int $limit = 500): array
    {
        $data = $this->load();
        $orders = array_values($data['orders']);
        usort($orders, static function ($a, $b) {
            $oa = (int) ($a['order_id'] ?? 0);
            $ob = (int) ($b['order_id'] ?? 0);
            return $ob <=> $oa;
        });
        if ($query !== null && trim($query) !== '') {
            $q = strtolower(trim($query));
            $orders = array_values(array_filter($orders, static function ($o) use ($q) {
                $hay = strtolower(implode(' ', [
                    $o['order_id'] ?? '',
                    $o['customer_name'] ?? '',
                    $o['phone'] ?? '',
                    $o['email'] ?? '',
                    $o['delivery_address'] ?? '',
                    implode(' ', $o['tags'] ?? []),
                    implode(' ', $o['items'] ?? []),
                ]));
                return str_contains($hay, $q);
            }));
        }
        return array_slice($orders, 0, $limit);
    }

    private function mergeOrder(array $existing, array $incoming): array
    {
        foreach ($incoming as $k => $v) {
            if ($k === 'line_items' || $k === 'items') {
                continue;
            }
            if ($v === '' || $v === null || $v === []) {
                continue;
            }
            if ($k === 'shipping' && is_array($v)) {
                $existing['shipping'] = $this->mergeShipping(
                    is_array($existing['shipping'] ?? null) ? $existing['shipping'] : [],
                    $v
                );
                continue;
            }
            $existing[$k] = $v;
        }
        if (!empty($incoming['items'])) {
            $existing['items'] = array_values(array_unique(array_merge(
                $existing['items'] ?? [],
                $incoming['items']
            )));
        }
        if (!empty($incoming['line_items'])) {
            $existing['line_items'] = $incoming['line_items'];
        }
        if (!empty($existing['shipping']) && is_array($existing['shipping'])) {
            $existing['delivery_address'] = ShopifyExportParser::formatAddress($existing['shipping']);
        }
        $existing['pii_limited'] = ($existing['phone'] ?? '') === ''
            && (($existing['shipping']['address1'] ?? '') === '');
        return $existing;
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

    /** @param array{orders: array, meta: array} $data */
    private function save(array $data): void
    {
        $dir = dirname($this->path);
        if (!is_dir($dir)) {
            mkdir($dir, 0755, true);
        }
        $json = json_encode($data, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE);
        if ($json === false) {
            throw new \RuntimeException('Failed to encode order desk JSON.');
        }
        if (file_put_contents($this->path, $json . "\n") === false) {
            throw new \RuntimeException('Could not write ' . $this->path);
        }
    }

    /**
     * Optional: after local save, POST to FastAPI to push shipping → Shopify and merge totals.
     *
     * Env:
     * - ORDER_DESK_PUSH_SYNC_URL — full URL e.g. http://127.0.0.1:7860/api/order-desk/push-save-to-shopify
     * - ORDER_DESK_SYNC_SECRET — must match Python ORDER_DESK_SYNC_SECRET
     */
    private function notifyPythonPushSync(string $orderId): void
    {
        $url = getenv('ORDER_DESK_PUSH_SYNC_URL');
        $secret = getenv('ORDER_DESK_SYNC_SECRET');
        if (!is_string($url) || trim($url) === '') {
            return;
        }
        if (!is_string($secret) || trim($secret) === '') {
            return;
        }
        $payload = json_encode(['order_id' => $orderId], JSON_UNESCAPED_UNICODE);
        if ($payload === false) {
            return;
        }
        $ch = curl_init(trim($url));
        if ($ch === false) {
            return;
        }
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $payload,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'X-Order-Desk-Sync-Secret: ' . trim($secret),
            ],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 15,
        ]);
        curl_exec($ch);
        curl_close($ch);
    }

    private function defaultMeta(): array
    {
        return [
            'description' => 'Shopify order export import — customer/shipping PII for voice agents',
            'last_import_at' => null,
            'last_import_file' => null,
            'order_count' => 0,
        ];
    }
}
