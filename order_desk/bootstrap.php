<?php

declare(strict_types=1);

/**
 * Load ORDER_DESK_* (and PORT) from repo root .env so `php -S` picks up sync settings
 * without manual export. ORDER_DESK_PUSH_SYNC_URL defaults to local FastAPI push endpoint.
 */
function order_desk_bootstrap_env(): void
{
    $envFile = dirname(__DIR__) . '/.env';
    if (!is_file($envFile)) {
        return;
    }
    $lines = file($envFile, FILE_IGNORE_NEW_LINES);
    if ($lines === false) {
        return;
    }
    foreach ($lines as $line) {
        $line = trim($line);
        if ($line === '' || str_starts_with($line, '#')) {
            continue;
        }
        if (!str_contains($line, '=')) {
            continue;
        }
        [$key, $val] = explode('=', $line, 2);
        $key = trim($key);
        $val = trim($val);
        if (preg_match('/\s+#/', $val)) {
            $val = trim(preg_replace('/\s+#.*$/', '', $val) ?? $val);
        }
        $val = trim($val, " \t\"'");
        if (!in_array($key, ['ORDER_DESK_SYNC_SECRET', 'PORT'], true)) {
            continue;
        }
        if (getenv($key) !== false) {
            continue;
        }
        putenv($key . '=' . $val);
        $_ENV[$key] = $val;
    }
    if (getenv('ORDER_DESK_PUSH_SYNC_URL') === false) {
        $port = getenv('PORT') ?: '7860';
        $url = 'http://127.0.0.1:' . $port . '/api/order-desk/push-save-to-shopify';
        putenv('ORDER_DESK_PUSH_SYNC_URL=' . $url);
        $_ENV['ORDER_DESK_PUSH_SYNC_URL'] = $url;
    }
}

order_desk_bootstrap_env();

$vendor = __DIR__ . '/vendor/autoload.php';
if (is_file($vendor)) {
    require $vendor;
} else {
    spl_autoload_register(static function (string $class): void {
        $prefix = 'OrderDesk\\';
        if (!str_starts_with($class, $prefix)) {
            return;
        }
        $rel = substr($class, strlen($prefix));
        $path = __DIR__ . '/lib/' . str_replace('\\', '/', $rel) . '.php';
        if (is_file($path)) {
            require $path;
        }
    });
}
