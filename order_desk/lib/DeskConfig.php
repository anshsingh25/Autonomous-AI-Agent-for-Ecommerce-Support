<?php

declare(strict_types=1);

namespace OrderDesk;

final class DeskConfig
{
    private string $path;

    public function __construct(?string $path = null)
    {
        $this->path = $path ?? dirname(__DIR__, 2) . '/data/order_desk_config.json';
    }

    /** @return array{export_url: string, local_path: string, downloads_folder: string} */
    public function load(): array
    {
        $defaults = $this->defaults();
        if (!is_file($this->path)) {
            return $defaults;
        }
        $raw = file_get_contents($this->path);
        if ($raw === false) {
            return $defaults;
        }
        $data = json_decode($raw, true);
        if (!is_array($data)) {
            return $defaults;
        }
        return [
            'export_url' => trim((string) ($data['export_url'] ?? $defaults['export_url'])),
            'local_path' => trim((string) ($data['local_path'] ?? $defaults['local_path'])),
            'downloads_folder' => trim((string) ($data['downloads_folder'] ?? $defaults['downloads_folder'])),
        ];
    }

    /** @param array{export_url?: string, local_path?: string, downloads_folder?: string} $patch */
    public function save(array $patch): void
    {
        $current = $this->load();
        foreach (['export_url', 'local_path', 'downloads_folder'] as $key) {
            if (array_key_exists($key, $patch)) {
                $current[$key] = trim((string) $patch[$key]);
            }
        }
        $dir = dirname($this->path);
        if (!is_dir($dir)) {
            mkdir($dir, 0755, true);
        }
        $json = json_encode($current, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE);
        if ($json === false || file_put_contents($this->path, $json . "\n") === false) {
            throw new \RuntimeException('Could not save order desk config.');
        }
    }

    /** @return array{export_url: string, local_path: string, downloads_folder: string} */
    private function defaults(): array
    {
        $home = (string) (getenv('HOME') ?: getenv('USERPROFILE') ?: '');
        $downloads = $home !== '' ? $home . '/Downloads' : '';
        return [
            'export_url' => '',
            'local_path' => '',
            'downloads_folder' => $downloads,
        ];
    }
}
