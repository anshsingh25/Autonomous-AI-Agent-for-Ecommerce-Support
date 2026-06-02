<?php
/** @var array<string, mixed>|null $selected */
$ship = is_array($selected['shipping'] ?? null) ? $selected['shipping'] : [];
$phone = (string) ($selected['phone'] ?? $ship['phone'] ?? '');
$empty = $selected === null;
$oid = $empty ? '' : (string) $selected['order_id'];
$dirty = !$empty && !empty($selected['dirty']);
?>
<div id="fields_empty" class="fields-empty"<?= $empty ? '' : ' style="display:none"' ?>>
  <div class="fields-empty-icon">📋</div>
  <p>Select an order from the table or enter an order number above.</p>
</div>

<form id="order_fields" class="order-fields<?= $empty ? ' hidden' : '' ?>" method="post" action="">
  <input type="hidden" name="action" value="save_order" />
  <input type="hidden" name="original_order_id" id="f_original_order_id" value="<?= htmlspecialchars($oid, ENT_QUOTES) ?>" />

  <div class="fields-toolbar">
    <div>
      <h3 id="editor_title"><?= $empty ? 'Order editor' : 'Order #' . htmlspecialchars($oid, ENT_QUOTES) ?></h3>
      <?php if ($dirty): ?><span class="pill dirty">edited</span><?php endif; ?>
    </div>
    <div>
      <span id="save_status"></span>
      <button type="submit" class="btn btn-primary" id="btn_save">Save changes</button>
    </div>
  </div>

  <div class="field-section">
    <p class="field-section-title">Contact</p>
    <div class="field-grid">
      <label class="field"><span>Order #</span><input type="text" name="order_id" id="f_order_id" value="<?= $empty ? '' : htmlspecialchars($oid, ENT_QUOTES) ?>" /></label>
      <label class="field"><span>Customer name</span><input type="text" name="customer_name" id="f_customer_name" value="<?= $empty ? '' : htmlspecialchars((string) ($selected['customer_name'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field"><span>Phone</span><input type="text" name="phone" id="f_phone" class="mono" value="<?= $empty ? '' : htmlspecialchars($phone, ENT_QUOTES) ?>" /></label>
      <label class="field"><span>Email</span><input type="email" name="email" id="f_email" value="<?= $empty ? '' : htmlspecialchars((string) ($selected['email'] ?? ''), ENT_QUOTES) ?>" /></label>
    </div>
  </div>

  <div class="field-section">
    <p class="field-section-title">Shipping address</p>
    <div class="field-grid">
      <label class="field"><span>Recipient name</span><input type="text" name="shipping_name" id="f_shipping_name" value="<?= $empty ? '' : htmlspecialchars((string) ($ship['name'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field"><span>Shipping phone</span><input type="text" name="shipping_phone" id="f_shipping_phone" class="mono" value="<?= $empty ? '' : htmlspecialchars((string) ($ship['phone'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field full"><span>Address line 1</span><input type="text" name="address1" id="f_address1" value="<?= $empty ? '' : htmlspecialchars((string) ($ship['address1'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field full"><span>Address line 2</span><input type="text" name="address2" id="f_address2" value="<?= $empty ? '' : htmlspecialchars((string) ($ship['address2'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field"><span>City</span><input type="text" name="city" id="f_city" value="<?= $empty ? '' : htmlspecialchars((string) ($ship['city'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field"><span>State / Province</span><input type="text" name="province" id="f_province" value="<?= $empty ? '' : htmlspecialchars((string) ($ship['province'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field"><span>ZIP / Postal</span><input type="text" name="zip_code" id="f_zip" value="<?= $empty ? '' : htmlspecialchars((string) ($ship['zip_code'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field"><span>Country</span><input type="text" name="country" id="f_country" value="<?= $empty ? '' : htmlspecialchars((string) ($ship['country'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field full"><span>Full delivery address</span><textarea name="delivery_address" id="f_delivery_address" rows="2"><?= $empty ? '' : htmlspecialchars((string) ($selected['delivery_address'] ?? ''), ENT_QUOTES) ?></textarea></label>
    </div>
  </div>

  <div class="field-section">
    <p class="field-section-title">Order &amp; call</p>
    <div class="field-grid">
      <label class="field full"><span>Line items</span><textarea name="items" id="f_items" rows="2" placeholder="Item one x1; Item two x2"><?= $empty ? '' : htmlspecialchars(implode('; ', $selected['items'] ?? []), ENT_QUOTES) ?></textarea></label>
      <label class="field"><span>Total (INR)</span><input type="number" step="0.01" name="total_inr" id="f_total_inr" value="<?= $empty ? '' : htmlspecialchars((string) ($selected['total_inr'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field"><span>Payment status</span><input type="text" name="financial_status" id="f_financial" value="<?= $empty ? '' : htmlspecialchars((string) ($selected['financial_status'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field"><span>Fulfillment</span><input type="text" name="fulfillment_status" id="f_fulfillment" value="<?= $empty ? '' : htmlspecialchars((string) ($selected['fulfillment_status'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field"><span>Call status</span><input type="text" name="call_status" id="f_call_status" value="<?= $empty ? '' : htmlspecialchars((string) ($selected['call_status'] ?? 'pending'), ENT_QUOTES) ?>" /></label>
      <label class="field"><span>Placed at</span><input type="text" name="placed_at" id="f_placed_at" value="<?= $empty ? '' : htmlspecialchars((string) ($selected['placed_at'] ?? ''), ENT_QUOTES) ?>" /></label>
      <label class="field full"><span>Tags</span><input type="text" name="tags" id="f_tags" placeholder="Retry Call, AI Confirmed" value="<?= $empty ? '' : htmlspecialchars(implode(', ', $selected['tags'] ?? []), ENT_QUOTES) ?>" /></label>
      <label class="field full"><span>Notes</span><textarea name="notes" id="f_notes" rows="2"><?= $empty ? '' : htmlspecialchars((string) ($selected['notes'] ?? ''), ENT_QUOTES) ?></textarea></label>
    </div>
  </div>
</form>
