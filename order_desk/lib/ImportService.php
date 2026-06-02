<?php

declare(strict_types=1);

namespace OrderDesk;

final class ImportService
{
    public function __construct(
        private readonly ShopifyExportParser $parser = new ShopifyExportParser(),
        private readonly OrderStore $store = new OrderStore(),
        private readonly ExportFetcher $fetcher = new ExportFetcher(),
    ) {
    }

    /** @return array{count: int, filename: string, source: string, incoming_order_ids: list<string>, distinct_orders_in_file: int} */
    public function importFromPath(string $path, string $filename): array
    {
        $orders = $this->parser->parseFile($path, $filename);
        if ($orders === []) {
            throw new \InvalidArgumentException('No orders found in export file.');
        }
        $count = $this->store->mergeImport($orders, $filename);
        $incomingIds = [];
        foreach ($orders as $o) {
            $id = (string) ($o['order_id'] ?? '');
            if ($id !== '') {
                $incomingIds[] = $id;
            }
        }
        $incomingIds = array_values(array_unique($incomingIds));
        rsort($incomingIds, SORT_NATURAL);

        return [
            'count' => $count,
            'filename' => $filename,
            'source' => 'file:' . $path,
            'incoming_order_ids' => $incomingIds,
            'distinct_orders_in_file' => count($incomingIds),
        ];
    }

    /** @return array{count: int, filename: string, source: string, incoming_order_ids: list<string>, distinct_orders_in_file: int} */
    public function downloadAndImport(string $source, ?string $downloadsFolder = null): array
    {
        $fetcher = $this->fetcher;
        if ($source === '__latest_downloads__' && $downloadsFolder !== null) {
            $file = $fetcher->fetchLatestFromDownloads($downloadsFolder);
        } else {
            $file = $fetcher->fetch($source, $downloadsFolder);
        }
        $result = $this->importFromPath($file['path'], $file['filename']);
        $result['source'] = $file['source'];
        return $result;
    }
}
