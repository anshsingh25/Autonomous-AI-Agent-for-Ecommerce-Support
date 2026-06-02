<?php
declare(strict_types=1);

namespace Aria\VoiceOrder\Api;

/**
 * REST endpoints used by the Aria voice agent (Magento has no core order-edit API).
 */
interface OrderEditManagementInterface
{
    /**
     * Update shipping address on an open order (entity_id).
     *
     * @param int $orderId Order entity_id
     * @param string $address1 Street line 1
     * @param string $city
     * @param string $postcode
     * @param string $address2 Street line 2
     * @param string $province Region name
     * @param string $name Full customer name (optional)
     * @param string $phone
     * @param string $countryId ISO country, e.g. GB
     * @return string JSON: success, message, delivery_address, order_id
     */
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
    ): string;

    /**
     * Change qty_ordered on a sales order item.
     *
     * @param int $itemId sales_order_item.item_id
     * @param float $qty New quantity (>= 1)
     * @return string JSON: success, message, total, order_id
     */
    public function updateItemQuantity(int $itemId, float $qty): string;
}
