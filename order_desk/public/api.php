<?php

declare(strict_types=1);

require dirname(__DIR__) . '/bootstrap.php';

use OrderDesk\OrderStore;

header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit;
}

$store = new OrderStore();

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $body = json_decode((string) file_get_contents('php://input'), true);
    if (!is_array($body)) {
        $body = $_POST;
    }
    $originalId = (string) ($body['original_order_id'] ?? $body['order_id'] ?? '');
    try {
        $order = $store->update($originalId, $body);
        echo json_encode(['ok' => true, 'order' => $order], JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
    } catch (Throwable $e) {
        http_response_code(400);
        echo json_encode(['ok' => false, 'message' => $e->getMessage()], JSON_UNESCAPED_UNICODE);
    }
    exit;
}

if (isset($_GET['order_id']) || isset($_GET['order'])) {
    $id = (string) ($_GET['order_id'] ?? $_GET['order'] ?? '');
    $order = $store->get($id);
    if ($order === null) {
        http_response_code(404);
        echo json_encode(['found' => false, 'message' => 'Order not found in order desk'], JSON_UNESCAPED_UNICODE);
        exit;
    }
    echo json_encode(['found' => true, 'order' => $order], JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
    exit;
}

$q = isset($_GET['q']) ? trim((string) $_GET['q']) : null;
$limit = min(500, max(1, (int) ($_GET['limit'] ?? 100)));
$orders = $store->list($q, $limit);
$data = $store->load();

echo json_encode([
    'orders' => $orders,
    'count' => count($orders),
    'meta' => $data['meta'],
], JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT);
