<?php

declare(strict_types=1);

namespace OrderDesk;

final class ExportFetcher
{
    private string $importsDir;

    public function __construct(?string $importsDir = null)
    {
        $this->importsDir = $importsDir ?? dirname(__DIR__, 2) . '/data/imports';
        if (!is_dir($this->importsDir)) {
            mkdir($this->importsDir, 0755, true);
        }
    }

    /**
     * Download or resolve a Shopify export file.
     *
     * @return array{path: string, filename: string, source: string}
     */
    public function fetch(string $source, ?string $downloadsFolder = null): array
    {
        $source = trim($source);
        if ($source === '') {
            throw new \InvalidArgumentException('Enter an export URL or file path.');
        }

        if ($this->isUrl($source)) {
            return $this->downloadUrl($source);
        }

        $resolved = $this->resolveLocalPath($source, $downloadsFolder);
        if (!is_file($resolved)) {
            throw new \InvalidArgumentException('File not found: ' . $resolved);
        }
        $ext = strtolower(pathinfo($resolved, PATHINFO_EXTENSION));
        if (!in_array($ext, ['csv', 'xlsx', 'xls'], true)) {
            throw new \InvalidArgumentException('File must be .csv, .xlsx, or .xls');
        }

        $filename = basename($resolved);
        $dest = $this->importsDir . '/latest_' . $filename;
        if (!copy($resolved, $dest)) {
            $dest = $resolved;
        }

        return [
            'path' => $dest,
            'filename' => $filename,
            'source' => 'local:' . $resolved,
        ];
    }

    /** Pick newest Shopify-like export in Downloads (prefers ``orders_export_*`` filenames). */
    public function fetchLatestFromDownloads(string $downloadsFolder): array
    {
        $dir = $this->expandPath($downloadsFolder);
        if (!is_dir($dir)) {
            throw new \InvalidArgumentException('Downloads folder not found: ' . $dir);
        }
        $shopifyExports = [];
        foreach (['orders_export*.csv', 'orders_export*.xlsx', 'orders_export*.xls'] as $pattern) {
            foreach (glob($dir . '/' . $pattern) ?: [] as $file) {
                if (is_file($file)) {
                    $shopifyExports[$file] = filemtime($file) ?: 0;
                }
            }
        }

        $candidates = [];
        if ($shopifyExports !== []) {
            $candidates = $shopifyExports;
        } else {
            foreach (['orders_export*.csv', 'orders_export*.xlsx', 'order*.csv', '*.csv'] as $pattern) {
                foreach (glob($dir . '/' . $pattern) ?: [] as $file) {
                    if (is_file($file)) {
                        $candidates[$file] = filemtime($file) ?: 0;
                    }
                }
            }
        }
        if ($candidates === []) {
            throw new \InvalidArgumentException(
                'No export file in Downloads. Export from Shopify (Orders → Export) first.'
            );
        }
        arsort($candidates);
        $latest = (string) array_key_first($candidates);
        return $this->fetch($latest, $downloadsFolder);
    }

    private function isUrl(string $source): bool
    {
        return (bool) preg_match('#^https?://#i', $source);
    }

    /** @return array{path: string, filename: string, source: string} */
    private function downloadUrl(string $url): array
    {
        $parsed = parse_url($url);
        if (!is_array($parsed) || !in_array(strtolower((string) ($parsed['scheme'] ?? '')), ['http', 'https'], true)) {
            throw new \InvalidArgumentException('Only http:// or https:// URLs are allowed.');
        }

        $pathPart = (string) ($parsed['path'] ?? '');
        $filename = basename($pathPart) !== '' ? basename($pathPart) : 'shopify_export.csv';
        if (!preg_match('/\.(csv|xlsx|xls)$/i', $filename)) {
            $filename = 'shopify_export.csv';
        }
        $dest = $this->importsDir . '/downloaded_' . date('Y-m-d_His') . '_' . $filename;

        $ctx = stream_context_create([
            'http' => [
                'timeout' => 60,
                'follow_location' => 1,
                'user_agent' => 'BadlavoOrderDesk/1.0',
            ],
            'ssl' => [
                'verify_peer' => true,
                'verify_peer_name' => true,
            ],
        ]);
        $body = @file_get_contents($url, false, $ctx);
        if ($body === false || $body === '') {
            throw new \RuntimeException(
                'Could not download file from URL. Use a direct link to .csv/.xlsx or save the file locally and use path instead.'
            );
        }
        if (file_put_contents($dest, $body) === false) {
            throw new \RuntimeException('Could not save downloaded file.');
        }

        return [
            'path' => $dest,
            'filename' => $filename,
            'source' => 'url:' . $url,
        ];
    }

    private function resolveLocalPath(string $source, ?string $downloadsFolder): string
    {
        $source = $this->expandPath($source);
        if ($source !== '' && is_file($source)) {
            $this->assertAllowedPath($source);
            return $source;
        }

        if ($downloadsFolder !== null && $downloadsFolder !== '') {
            $try = $this->expandPath(rtrim($downloadsFolder, '/') . '/' . ltrim($source, '/'));
            if (is_file($try)) {
                $this->assertAllowedPath($try);
                return $try;
            }
        }

        $projectImports = dirname(__DIR__, 2) . '/data/imports/' . ltrim($source, '/');
        if (is_file($projectImports)) {
            return $projectImports;
        }

        return $source;
    }

    private function expandPath(string $path): string
    {
        $path = trim($path);
        if ($path === '') {
            return '';
        }
        if (str_starts_with($path, '~/')) {
            $home = (string) (getenv('HOME') ?: getenv('USERPROFILE') ?: '');
            if ($home !== '') {
                return $home . substr($path, 1);
            }
        }
        return $path;
    }

    private function assertAllowedPath(string $path): void
    {
        $real = realpath($path);
        if ($real === false) {
            return;
        }
        $allowedRoots = array_filter([
            realpath(dirname(__DIR__, 2) . '/data'),
            realpath(dirname(__DIR__, 2) . '/order_desk'),
            $this->expandPath('~/Downloads') !== '' ? realpath($this->expandPath('~/Downloads')) : false,
            getenv('HOME') ? realpath((string) getenv('HOME') . '/Downloads') : false,
        ]);
        foreach ($allowedRoots as $root) {
            if ($root !== false && str_starts_with($real, $root)) {
                return;
            }
        }
        throw new \InvalidArgumentException(
            'For security, local files must be under Downloads, data/imports, or this project.'
        );
    }
}
