<?php
declare(strict_types=1);

namespace Aria\VoiceOrder\Model;

use Aria\VoiceOrder\Api\OrderEditManagementInterface;
use Magento\Framework\Exception\LocalizedException;
use Magento\Sales\Api\Data\OrderInterface;
use Magento\Sales\Api\OrderAddressRepositoryInterface;
use Magento\Sales\Api\OrderItemRepositoryInterface;
use Magento\Sales\Api\OrderRepositoryInterface;
use Magento\Sales\Model\Order;

class OrderEditManagement implements OrderEditManagementInterface
{
    public function __construct(
        private readonly OrderRepositoryInterface $orderRepository,
        private readonly OrderAddressRepositoryInterface $orderAddressRepository,
        private readonly OrderItemRepositoryInterface $orderItemRepository
    ) {
    }

    public function updateShippingAddress(
        int $orderId,
        string $address1,
        string $city,
        string $postcode,
        string $address2 = '',
        string $province = '',
        string $name = '',
        string $phone = '',
        string $countryId = 'GB'
    ): string {
        try {
            $order = $this->orderRepository->get($orderId);
            $this->assertEditable($order);

            $address = $order->getShippingAddress();
            if ($address === null) {
                throw new LocalizedException(__('Order has no shipping address.'));
            }

            $street = array_values(array_filter([
                trim($address1),
                trim($address2),
            ], static fn (string $line): bool => $line !== ''));

            if ($street === []) {
                throw new LocalizedException(__('Street address is required.'));
            }

            $address->setStreet($street);
            $address->setCity(trim($city));
            $address->setPostcode(trim($postcode));
            $address->setCountryId($countryId ?: 'GB');

            if (trim($province) !== '') {
                $address->setRegion(trim($province));
            }
            if (trim($phone) !== '') {
                $address->setTelephone(trim($phone));
            }

            if (trim($name) !== '') {
                $parts = preg_split('/\s+/', trim($name), 2) ?: [];
                $address->setFirstname($parts[0] ?? $name);
                $address->setLastname($parts[1] ?? '');
            }

            $this->orderAddressRepository->save($address);

            $formatted = $this->formatAddress($address);

            return $this->encode([
                'success' => true,
                'order_id' => (string) $order->getIncrementId(),
                'delivery_address' => $formatted,
                'message' => 'Delivery address updated successfully in Magento.',
            ]);
        } catch (LocalizedException $e) {
            return $this->encode([
                'success' => false,
                'message' => $e->getMessage(),
            ]);
        } catch (\Throwable $e) {
            return $this->encode([
                'success' => false,
                'message' => 'Could not update shipping address: ' . $e->getMessage(),
            ]);
        }
    }

    public function updateItemQuantity(int $itemId, float $qty): string
    {
        try {
            if ($qty < 1) {
                throw new LocalizedException(__('Quantity must be at least 1.'));
            }

            $item = $this->orderItemRepository->get($itemId);
            $order = $this->orderRepository->get((int) $item->getOrderId());
            $this->assertEditable($order);

            $item->setQtyOrdered($qty);
            $this->orderItemRepository->save($item);

            // Reload for updated grand total when Magento recalculates on read.
            $order = $this->orderRepository->get((int) $order->getEntityId());

            return $this->encode([
                'success' => true,
                'order_id' => (string) $order->getIncrementId(),
                'total' => (float) $order->getGrandTotal(),
                'message' => 'Line item quantity updated in Magento.',
            ]);
        } catch (LocalizedException $e) {
            return $this->encode([
                'success' => false,
                'message' => $e->getMessage(),
            ]);
        } catch (\Throwable $e) {
            return $this->encode([
                'success' => false,
                'message' => 'Could not update quantity: ' . $e->getMessage(),
            ]);
        }
    }

    private function assertEditable(OrderInterface $order): void
    {
        $state = strtolower((string) $order->getState());
        $status = strtolower((string) $order->getStatus());

        if (in_array($state, ['canceled', 'closed', 'complete'], true)
            || in_array($status, ['canceled', 'cancelled', 'closed', 'complete'], true)
        ) {
            throw new LocalizedException(
                __('Order cannot be edited after it is complete, closed, or cancelled.')
            );
        }
    }

    private function formatAddress(\Magento\Sales\Api\Data\OrderAddressInterface $address): string
    {
        $street = $address->getStreet();
        if (!is_array($street)) {
            $street = [$street];
        }
        $lines = array_filter([
            trim(implode(' ', array_map('strval', $street))),
            trim((string) $address->getCity()),
            trim((string) $address->getRegion()),
            trim((string) $address->getPostcode()),
            trim((string) $address->getCountryId()),
        ], static fn (string $line): bool => $line !== '');

        return implode(', ', $lines);
    }

    /**
     * @param array<string, mixed> $payload
     */
    private function encode(array $payload): string
    {
        return (string) json_encode($payload, JSON_THROW_ON_ERROR);
    }
}
