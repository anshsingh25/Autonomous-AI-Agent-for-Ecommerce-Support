(function () {
  const orderInput = document.getElementById('order_lookup');
  const fieldsForm = document.getElementById('order_fields');
  if (!orderInput || !fieldsForm) return;

  function setField(id, value) {
    const el = document.getElementById(id);
    if (el) el.value = value ?? '';
  }

  function fillForm(order) {
    if (!order) return;
    const ship = order.shipping || {};
    const phone = order.phone || ship.phone || '';
    setField('f_original_order_id', order.order_id);
    setField('f_order_id', order.order_id);
    setField('f_customer_name', order.customer_name);
    setField('f_phone', phone);
    setField('f_email', order.email);
    setField('f_shipping_name', ship.name);
    setField('f_shipping_phone', ship.phone);
    setField('f_address1', ship.address1);
    setField('f_address2', ship.address2);
    setField('f_city', ship.city);
    setField('f_province', ship.province);
    setField('f_zip', ship.zip_code);
    setField('f_country', ship.country);
    setField('f_delivery_address', order.delivery_address);
    setField('f_items', (order.items || []).join('; '));
    setField('f_total_inr', order.total_inr != null ? String(order.total_inr) : '');
    setField('f_financial', order.financial_status);
    setField('f_fulfillment', order.fulfillment_status);
    setField('f_call_status', order.call_status || 'pending');
    setField('f_tags', (order.tags || []).join(', '));
    setField('f_placed_at', order.placed_at);
    setField('f_notes', order.notes || '');
    fieldsForm.classList.remove('hidden');
    const empty = document.getElementById('fields_empty');
    if (empty) empty.style.display = 'none';
    const title = document.getElementById('editor_title');
    if (title) title.textContent = 'Order #' + order.order_id;
  }

  function formPayload() {
    const data = {};
    fieldsForm.querySelectorAll('input, textarea, select').forEach((el) => {
      if (!el.name || el.type === 'submit') return;
      data[el.name] = el.value;
    });
    return data;
  }

  function setSaveStatus(text, ok) {
    const el = document.getElementById('save_status');
    if (!el) return;
    el.textContent = text;
    el.className = ok ? 'ok' : 'err';
  }

  async function lookupOrder(id) {
    const key = (id || '').replace(/^#/, '').trim();
    if (!key) return;
    const res = await fetch('api.php?order_id=' + encodeURIComponent(key));
    const data = await res.json();
    if (data.found && data.order) {
      fillForm(data.order);
      const url = new URL(window.location.href);
      url.searchParams.set('order', key);
      window.history.replaceState({}, '', url);
    }
  }

  async function saveOrder(e) {
    if (e) e.preventDefault();
    const payload = formPayload();
    if (!payload.original_order_id && !payload.order_id) {
      setSaveStatus('Load an order first.', false);
      return;
    }
    setSaveStatus('Saving…', true);
    try {
      const res = await fetch('api.php', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.message || 'Save failed');
      }
      fillForm(data.order);
      orderInput.value = data.order.order_id;
      setSaveStatus('✓ Saved', true);
      const title = document.getElementById('editor_title');
      if (title) title.textContent = 'Order #' + data.order.order_id;
      const url = new URL(window.location.href);
      url.searchParams.set('order', data.order.order_id);
      window.history.replaceState({}, '', url);
    } catch (err) {
      setSaveStatus(err.message || 'Save failed', false);
    }
  }

  orderInput.addEventListener('change', () => lookupOrder(orderInput.value));
  orderInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      lookupOrder(orderInput.value);
    }
  });

  document.getElementById('btn_lookup')?.addEventListener('click', (e) => {
    e.preventDefault();
    lookupOrder(orderInput.value);
  });

  fieldsForm.addEventListener('submit', saveOrder);
})();
