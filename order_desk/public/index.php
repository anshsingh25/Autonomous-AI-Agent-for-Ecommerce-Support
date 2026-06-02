<?php

declare(strict_types=1);

require dirname(__DIR__) . '/bootstrap.php';

use OrderDesk\DeskConfig;
use OrderDesk\ImportService;
use OrderDesk\OrderStore;

$store = new OrderStore();
$config = new DeskConfig();
$importer = new ImportService();
$cfg = $config->load();

$message = null;
$messageType = 'info';
$postSelectedId = null;

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $action = (string) ($_POST['action'] ?? '');
    try {
        if ($action === 'save_config') {
            $config->save([
                'export_url' => (string) ($_POST['export_url'] ?? ''),
                'local_path' => (string) ($_POST['local_path'] ?? ''),
                'downloads_folder' => (string) ($_POST['downloads_folder'] ?? ''),
            ]);
            $cfg = $config->load();
            $message = 'Settings saved.';
            $messageType = 'ok';
        } elseif ($action === 'download_import') {
            $source = trim((string) ($_POST['download_source'] ?? ''));
            $useLatest = isset($_POST['use_latest_downloads']);
            if ($useLatest) {
                $result = $importer->downloadAndImport('__latest_downloads__', $cfg['downloads_folder']);
            } else {
                if ($source === '') {
                    $source = $cfg['export_url'] !== '' ? $cfg['export_url'] : $cfg['local_path'];
                }
                $result = $importer->downloadAndImport($source, $cfg['downloads_folder'] ?: null);
            }
            $message = sprintf(
                'Imported %d order merge(s) from %s.',
                $result['count'],
                htmlspecialchars($result['filename'], ENT_QUOTES)
            );
            if (!empty($result['incoming_order_ids'])) {
                $preview = array_slice($result['incoming_order_ids'], 0, 35);
                $message .= ' Numbers in this export: #' . htmlspecialchars(
                    implode(', #', $preview),
                    ENT_QUOTES,
                    'UTF-8'
                );
                $left = count($result['incoming_order_ids']) - count($preview);
                if ($left > 0) {
                    $message .= " (+{$left} more)";
                }
                $message .= '. If a new order (e.g. #1601) is missing, Shopify’s CSV only lists orders in the view you exported — export again with the right date range or filters.';
            }
            $messageType = 'ok';
        } elseif ($action === 'import') {
            if (!isset($_FILES['export']) || $_FILES['export']['error'] !== UPLOAD_ERR_OK) {
                throw new InvalidArgumentException('Choose a file to upload.');
            }
            $tmp = $_FILES['export']['tmp_name'];
            $name = (string) ($_FILES['export']['name'] ?? 'export.csv');
            $result = $importer->importFromPath($tmp, $name);
            $message = sprintf('Imported %d order merge(s).', $result['count']);
            if (!empty($result['incoming_order_ids'])) {
                $preview = array_slice($result['incoming_order_ids'], 0, 35);
                $message .= ' In file: #' . htmlspecialchars(
                    implode(', #', $preview),
                    ENT_QUOTES,
                    'UTF-8'
                );
                if (count($result['incoming_order_ids']) > count($preview)) {
                    $more = count($result['incoming_order_ids']) - count($preview);
                    $message .= " (+{$more} more)";
                }
                $message .= '.';
            }
            $messageType = 'ok';
        } elseif ($action === 'save_order') {
            $originalId = (string) ($_POST['original_order_id'] ?? $_POST['order_id'] ?? '');
            $order = $store->update($originalId, $_POST);
            $postSelectedId = (string) $order['order_id'];
            $message = 'Order #' . htmlspecialchars($postSelectedId, ENT_QUOTES) . ' saved successfully.';
            $messageType = 'ok';
        }
    } catch (Throwable $e) {
        $message = $e->getMessage();
        $messageType = 'err';
    }
}

$q = isset($_GET['q']) ? trim((string) $_GET['q']) : '';
$selectedId = $postSelectedId ?? (isset($_GET['order']) ? ltrim(trim((string) $_GET['order']), '#') : '');
if ($selectedId === '' && isset($_GET['order_lookup'])) {
    $selectedId = ltrim(trim((string) $_GET['order_lookup']), '#');
}
$orders = $store->list($q !== '' ? $q : null);
$selected = $selectedId !== '' ? $store->get($selectedId) : null;
$data = $store->load();
$meta = $data['meta'];
$cfg = $config->load();
$orderCount = (int) ($meta['order_count'] ?? count($orders));
$withPhone = 0;
foreach ($orders as $o) {
    $p = (string) ($o['phone'] ?? '');
    if ($p === '' && !empty($o['shipping']['phone'])) {
        $p = (string) $o['shipping']['phone'];
    }
    if ($p !== '') {
        $withPhone++;
    }
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Order desk — badlavo</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <link rel="stylesheet" href="styles.css" />
</head>
<body>
  <header class="site-header">
    <div class="header-inner">
      <div class="brand">
        <div class="brand-icon">B</div>
        <div>
          <h1>Order desk</h1>
          <p class="sub">Shopify export · customer &amp; shipping PII for agents</p>
        </div>
      </div>
      <div class="stat-pills">
        <span class="stat-pill"><strong><?= $orderCount ?></strong> orders</span>
        <span class="stat-pill"><strong><?= $withPhone ?></strong> with phone</span>
        <?php if (!empty($meta['last_import_file'])): ?>
          <span class="stat-pill" title="<?= htmlspecialchars((string) ($meta['last_import_at'] ?? ''), ENT_QUOTES) ?>">
            <?= htmlspecialchars((string) $meta['last_import_file'], ENT_QUOTES) ?>
          </span>
        <?php endif; ?>
      </div>
    </div>
  </header>

  <main class="wrap">
    <?php if ($message): ?>
      <div class="toast <?= htmlspecialchars($messageType, ENT_QUOTES) ?>" role="status">
        <?= $messageType === 'ok' ? '✓ ' . $message : '✕ ' . htmlspecialchars($message, ENT_QUOTES) ?>
      </div>
    <?php endif; ?>

    <details class="card import-panel" open>
      <summary class="card-header">
        <h2><span class="step">1</span> Import from Shopify export <span class="chevron">▶</span></h2>
      </summary>
      <div class="card-body">
        <p class="hint">
          In Shopify Admin: <strong>Orders → Export</strong>. Then download automatically from URL/path,
          pick the newest file in Downloads, or upload manually.
        </p>
        <div class="import-blocks">
          <div class="import-block">
            <p class="import-block-title">Saved paths</p>
            <form method="post">
              <input type="hidden" name="action" value="save_config" />
              <div class="row">
                <label class="field grow2"><span>Export URL</span>
                  <input type="url" name="export_url" placeholder="https://…/orders_export.csv" value="<?= htmlspecialchars($cfg['export_url'], ENT_QUOTES) ?>" />
                </label>
                <label class="field grow2"><span>Local file path</span>
                  <input type="text" name="local_path" placeholder="~/Downloads/orders_export.csv" value="<?= htmlspecialchars($cfg['local_path'], ENT_QUOTES) ?>" />
                </label>
                <label class="field"><span>Downloads folder</span>
                  <input type="text" name="downloads_folder" value="<?= htmlspecialchars($cfg['downloads_folder'], ENT_QUOTES) ?>" />
                </label>
              </div>
              <button type="submit" class="btn btn-secondary">Save paths</button>
            </form>
          </div>
          <div class="import-block">
            <p class="import-block-title">Download &amp; import</p>
            <form method="post">
              <input type="hidden" name="action" value="download_import" />
              <div class="row">
                <label class="field grow2"><span>Source (optional override)</span>
                  <input type="text" name="download_source" placeholder="URL or path — empty uses saved values above" />
                </label>
                <label class="field chk">
                  <input type="checkbox" name="use_latest_downloads" value="1" />
                  <span>Newest file in Downloads</span>
                </label>
                <button type="submit" class="btn btn-primary">Download &amp; import</button>
              </div>
            </form>
          </div>
          <div class="import-block">
            <p class="import-block-title">Manual upload</p>
            <form method="post" enctype="multipart/form-data">
              <input type="hidden" name="action" value="import" />
              <div class="row">
                <label class="field grow2"><span>CSV or Excel file</span>
                  <input type="file" name="export" accept=".csv,.xlsx,.xls" />
                </label>
                <button type="submit" class="btn btn-secondary">Upload &amp; import</button>
              </div>
            </form>
          </div>
        </div>
      </div>
    </details>

    <section class="card">
      <div class="card-header">
        <h2><span class="step">2</span> Find order</h2>
      </div>
      <div class="card-body lookup-bar">
        <label class="field order-input"><span>Order #</span>
          <input type="text" id="order_lookup" placeholder="1600" value="<?= htmlspecialchars($selectedId, ENT_QUOTES) ?>" />
        </label>
        <button type="button" class="btn btn-purple" id="btn_lookup">Open</button>
        <form method="get" class="search-form" style="display:contents">
          <label class="field" style="min-width:0"><span>Search all orders</span>
            <input type="search" name="q" placeholder="Name, phone, address, tags…" value="<?= htmlspecialchars($q, ENT_QUOTES) ?>" />
          </label>
          <button type="submit" class="btn btn-secondary">Search</button>
          <?php if ($q !== ''): ?>
            <a class="btn btn-secondary" href="index.php">Clear</a>
          <?php endif; ?>
        </form>
      </div>
    </section>

    <div class="panels">
      <section class="card">
        <div class="card-header">
          <h2><span class="step">3</span> Orders <?= $q !== '' ? '<span style="color:var(--muted);font-weight:400">(' . count($orders) . ' results)</span>' : '' ?></h2>
        </div>
        <?php if ($orders === []): ?>
          <div class="empty-table">
            <div class="fields-empty-icon">📦</div>
            <strong>No orders yet</strong>
            <p>Import a Shopify export using step 1 above.</p>
          </div>
        <?php else: ?>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Order</th>
                  <th>Customer</th>
                  <th>Total</th>
                  <th>Phone</th>
                  <th>Payment</th>
                  <th>Fulfillment</th>
                </tr>
              </thead>
              <tbody>
                <?php foreach ($orders as $o):
                  $oid = (string) ($o['order_id'] ?? '');
                  $phone = (string) ($o['phone'] ?? '');
                  if ($phone === '' && !empty($o['shipping']['phone'])) {
                      $phone = (string) $o['shipping']['phone'];
                  }
                  $ful = strtolower((string) ($o['fulfillment_status'] ?? ''));
                  $fulClass = str_contains($ful, 'fulfill') && !str_contains($ful, 'un') ? 'fulfilled' : 'unfulfilled';
                ?>
                <tr class="<?= $selectedId === $oid ? 'selected' : '' ?>"
                    onclick="location.href='?order=<?= urlencode($oid) ?><?= $q !== '' ? '&q=' . urlencode($q) : '' ?>#order_fields_panel'">
                  <td>
                    <span class="order-num">#<?= htmlspecialchars($oid, ENT_QUOTES) ?></span>
                    <?php if (!empty($o['dirty'])): ?><span class="pill dirty">edited</span><?php endif; ?>
                  </td>
                  <td class="customer-cell"><?= htmlspecialchars((string) ($o['customer_name'] ?? ''), ENT_QUOTES) ?></td>
                  <td class="total-cell">₹<?= number_format((float) ($o['total_inr'] ?? 0), 2) ?></td>
                  <td class="mono"><?= $phone !== '' ? htmlspecialchars($phone, ENT_QUOTES) : '<span class="pill warn">missing</span>' ?></td>
                  <td><span class="pill paid"><?= htmlspecialchars((string) ($o['financial_status'] ?? '—'), ENT_QUOTES) ?></span></td>
                  <td><span class="pill <?= $fulClass ?>"><?= htmlspecialchars((string) ($o['fulfillment_status'] ?? '—'), ENT_QUOTES) ?></span></td>
                </tr>
                <?php endforeach; ?>
              </tbody>
            </table>
          </div>
        <?php endif; ?>
      </section>

      <section class="card editor-panel" id="order_fields_panel">
        <div class="card-header">
          <h2><span class="step">4</span> Edit order</h2>
        </div>
        <?php require __DIR__ . '/_order_fields.php'; ?>
      </section>
    </div>
  </main>
  <script src="app.js"></script>
</body>
</html>
