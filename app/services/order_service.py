from datetime import datetime, timezone
from app import get_db
from app.services.inventory_service import adjust_ready_stock_qty
from google.cloud.firestore_v1 import FieldFilter


PLATFORMS = ['Amazon', 'Flipkart', 'Meesho', 'Instagram', 'Personal Reference', 'Website']
REVIEWS = ['Done', 'Pending', 'Not Responding']
STATUSES = ['Pending', 'Shipped', 'Delivered', 'RTO', 'Returned', 'Cancelled', 'Settled']
DISPATCHED_STATUSES = ['Shipped', 'Delivered', 'Settled']
RETURNED_STATUSES = ['RTO', 'Returned', 'Customer Return']
CANCELLED_STATUSES = ['Cancelled']
TERMINAL_STATUSES = ['Cancelled', 'Settled', 'Returned', 'RTO']

def get_stock_deltas(status):
    """Returns (qty_delta, reserved_delta) based on order status."""
    if status in DISPATCHED_STATUSES:
        return (-1, 0) # physical stock leaves
    if status in RETURNED_STATUSES:
        return (-1, 0) # physical stock stays deducted (due to potential damages)
    if status in CANCELLED_STATUSES:
        return (0, 0) # neither physical nor reserved
    return (0, 1) # pending, reserved


def get_all_orders(date_from=None, date_to=None, platform=None, status=None, review_status=None, cursor_id=None, direction='next', limit=20):
    db = get_db()
    query = db.collection('orders')
    
    if platform:
        query = query.where(filter=FieldFilter('platform', '==', platform))
    if status:
        query = query.where(filter=FieldFilter('status', '==', status))
        
    if review_status:
        if review_status == 'Done':
            query = query.where(filter=FieldFilter('reviews', '==', 'Done'))
        elif review_status == 'Pending':
            query = query.where(filter=FieldFilter('reviews', 'in', ['Pending', 'Not Responding', '']))

    if date_from:
        df = datetime.combine(date_from, datetime.min.time()).replace(tzinfo=timezone.utc)
        query = query.where(filter=FieldFilter('date', '>=', df))
    if date_to:
        dt = datetime.combine(date_to, datetime.max.time()).replace(tzinfo=timezone.utc)
        query = query.where(filter=FieldFilter('date', '<=', dt))

    cursor_doc = None
    if cursor_id:
        doc_ref = db.collection('orders').document(cursor_id).get()
        if doc_ref.exists:
            cursor_doc = doc_ref

    is_prev = (direction == 'prev' and cursor_doc)
    sort_dir = 'ASCENDING' if is_prev else 'DESCENDING'

    query = query.order_by('date', direction=sort_dir)

    if is_prev:
        query = query.start_after(cursor_doc).limit(limit + 1)
    else:
        if cursor_doc:
            query = query.start_after(cursor_doc)
        query = query.limit(limit + 1)
        
    docs = list(query.stream())
    
    has_prev = False
    has_next = False

    if is_prev:
        docs.reverse()
        if len(docs) > limit:
            has_prev = True
            docs.pop(0)
        has_next = True
    else:
        if len(docs) > limit:
            has_next = True
            docs.pop()
        if cursor_doc:
            has_prev = True

    results = []
    for d in docs:
        entry = {'id': d.id, **d.to_dict()}
        results.append(entry)

    # Secondary sort in memory for the current page to preserve chronological order for same-date orders
    results.sort(key=lambda x: x.get('created_at').isoformat() if hasattr(x.get('created_at'), 'isoformat') else str(x.get('created_at', '')), reverse=True)
    # We must ensure the primary sort is still respected after the stable sort
    results.sort(key=lambda x: x.get('date').isoformat() if hasattr(x.get('date'), 'isoformat') else str(x.get('date', '')), reverse=True)

    return results, has_prev, has_next


def add_order(data):
    db = get_db()
    now = datetime.now(timezone.utc)

    order_items = data.get('order_items', [])
    selling_price = sum(float(item.get('price', 0)) * float(item.get('quantity', 1)) for item in order_items)
    
    shipping = max(0, float(data.get('shipping', 0)))
    refund = max(0, float(data.get('refund', 0)))
    tax = max(0, float(data.get('tax', 0)))
    marketplace_fee = max(0, float(data.get('marketplace_fee', 0)))
    other_charges = max(0, float(data.get('other_charges', 0)))
    
    status = data.get('status', 'Pending')
    if status in CANCELLED_STATUSES:
        bank_settlement = 0.0
    else:
        bank_settlement = selling_price - shipping - refund - tax - marketplace_fee - other_charges

    order_date = data.get('date')
    if order_date:
        try:
            order_date = datetime.strptime(order_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            order_date = now
    else:
        order_date = now

    order = {
        'date': order_date,
        'order_id': data.get('order_id', ''),
        'customer': data.get('customer', ''),
        'customer_id': data.get('customer_id', ''),
        'number': data.get('number', ''),
        'order_items': order_items,
        'platform': data.get('platform', ''),
        'selling_price': selling_price,
        'shipping': shipping,
        'refund': refund,
        'tax': tax,
        'marketplace_fee': marketplace_fee,
        'other_charges': other_charges,
        'bank_settlement': bank_settlement,
        'status': status,
        'reviews': data.get('reviews', ''),
        'created_at': now,
        'status_history': [{'status': status, 'timestamp': now.isoformat()}],
    }

    _, doc_ref = db.collection('orders').add(order)

    # Adjust stock based on status for each item
    for item in order_items:
        product = item.get('product', '')
        color = item.get('color', '')
        qty = float(item.get('quantity', 1))
        
        if product:
            qty_delta, res_delta = get_stock_deltas(status)
            if qty_delta != 0 or res_delta != 0:
                o_id = data.get('order_id')
                label = f"Order {o_id} " if o_id else "Order "
                reason = f"{label}logged ({status})"
                adjust_ready_stock_qty(product, color, qty_delta * qty, res_delta * qty, reason=reason, ref_id=doc_ref.id)

    # Cashbook entry is now exclusively handled by the Payment Settlement process

    return doc_ref.id


def update_order(doc_id, data):
    db = get_db()
    
    doc = db.collection('orders').document(doc_id).get()
    if not doc.exists:
        return
    old_data = doc.to_dict()
    
    update_data = {}

    # ── Status State Machine: enforce forward-only transitions ──────────────────
    ALLOWED_TRANSITIONS = {
        'Pending':   ['Pending', 'Shipped', 'Cancelled'],
        'Shipped':   ['Shipped', 'Delivered', 'Returned', 'RTO', 'Cancelled'],
        'Delivered': ['Delivered', 'Settled', 'Returned'],
        # Terminal states — locked forever
        'Settled':   ['Settled'],
        'Cancelled': ['Cancelled'],
        'RTO':       ['RTO'],
        'Returned':  ['Returned'],
        'Customer Return': ['Customer Return'],
    }

    incoming_status = data.get('status')
    if incoming_status:
        current_status = old_data.get('status', 'Pending')
        allowed = ALLOWED_TRANSITIONS.get(current_status, [current_status])
        if incoming_status not in allowed:
            # Silently ignore the illegal transition — just strip the status field
            # so the rest of the edit (financials, customer info) still saves.
            data = {k: v for k, v in data.items() if k != 'status'}

    for field in ['order_id', 'customer', 'number', 'platform', 'status', 'reviews']:
        if field in data:
            update_data[field] = data[field]

    # Save shipping_id only when it is explicitly provided (Shipped transitions)
    if 'shipping_id' in data:
        update_data['shipping_id'] = data.get('shipping_id') or ''

    if 'order_items' in data:
        update_data['order_items'] = data['order_items']
        update_data['selling_price'] = sum(float(item.get('price', 0)) * float(item.get('quantity', 1)) for item in data['order_items'])

    for field in ['shipping', 'refund', 'tax', 'marketplace_fee', 'other_charges']:
        if field in data:
            update_data[field] = max(0, float(data[field])) if data[field] else 0

    if any(f in data for f in ['order_items', 'shipping', 'refund', 'tax', 'marketplace_fee', 'other_charges', 'status']):
        new_status = update_data.get('status', old_data.get('status', 'Pending'))
        if new_status in CANCELLED_STATUSES:
            update_data['bank_settlement'] = 0.0
        else:
            sp = update_data.get('selling_price', old_data.get('selling_price', 0))
            sh = update_data.get('shipping', old_data.get('shipping', 0))
            rf = update_data.get('refund', old_data.get('refund', 0))
            tx = update_data.get('tax', old_data.get('tax', 0))
            mf = update_data.get('marketplace_fee', old_data.get('marketplace_fee', 0))
            oc = update_data.get('other_charges', old_data.get('other_charges', 0))
            update_data['bank_settlement'] = sp - sh - rf - tx - mf - oc

    if 'date' in data and data['date']:
        try:
            update_data['date'] = datetime.strptime(data['date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    update_data['updated_at'] = datetime.now(timezone.utc)

    # Append to status_history if status changed
    new_status_val = update_data.get('status')
    if new_status_val and new_status_val != old_data.get('status'):
        existing_history = old_data.get('status_history', [])
        existing_history.append({'status': new_status_val, 'timestamp': update_data['updated_at'].isoformat()})
        update_data['status_history'] = existing_history

    db.collection('orders').document(doc_id).update(update_data)


    old_status = old_data.get('status', 'Pending')
    old_items = old_data.get('order_items', [])
    new_status = update_data.get('status', old_status)
    new_items = update_data.get('order_items', old_items)

    def get_zone(status, data_dict):
        """
        Zone A (Reserved):   Pending
                             - Stock is on shelf, but reserved_qty is +1.
        Zone B (Dispatched): Shipped, Delivered, Settled, Returned (Damaged)
                             - Stock has physically left the warehouse.
        Zone C (Restocked):  Cancelled, RTO, Returned (Restock)
                             - Stock is back and fully available.
        """
        if status == 'Pending':
            return 'A'
        if status in ['Cancelled', 'RTO']:
            return 'C'
        if status in ['Returned', 'Customer Return']:
            condition = data_dict.get('item_condition', 'damaged')
            return 'C' if condition == 'restock' else 'B'
        # Shipped, Delivered, Settled → dispatched
        return 'B'

    old_zone = get_zone(old_status, old_data)
    new_zone = get_zone(new_status, {**old_data, **update_data})

    old_items_sig = [(i.get('product'), i.get('color'), float(i.get('quantity', 1))) for i in old_items]
    new_items_sig = [(i.get('product'), i.get('color'), float(i.get('quantity', 1))) for i in new_items]

    # CASE A: Items themselves changed → full reversal & re-apply (rare admin edit)
    if old_items_sig != new_items_sig:
        old_qty_d, old_res_d = get_stock_deltas(old_status)
        for item in old_items:
            prod = item.get('product')
            qty = float(item.get('quantity', 1))
            if prod and (old_qty_d != 0 or old_res_d != 0):
                adjust_ready_stock_qty(prod, item.get('color', ''), -old_qty_d * qty, -old_res_d * qty, reason="Order Edit Reversal", ref_id=doc_id)

        new_qty_d, new_res_d = get_stock_deltas(new_status)
        for item in new_items:
            prod = item.get('product')
            qty = float(item.get('quantity', 1))
            if prod and (new_qty_d != 0 or new_res_d != 0):
                adjust_ready_stock_qty(prod, item.get('color', ''), new_qty_d * qty, new_res_d * qty, reason="Order Edit Re-apply", ref_id=doc_id)

    # CASE B: Only status changed — apply precise zone-transition delta
    elif old_status != new_status:
        if old_zone == new_zone:
            # Same zone → no inventory change (e.g. Shipped → Delivered)
            pass

        elif old_zone == 'A' and new_zone == 'B':
            # Pending → Shipped/Delivered: release reservation AND deduct physical stock
            o_id = update_data.get('order_id', old_data.get('order_id'))
            prefix = f"Order {o_id} " if o_id else ""
            for item in new_items:
                prod = item.get('product')
                qty = float(item.get('quantity', 1))
                if prod:
                    adjust_ready_stock_qty(prod, item.get('color', ''), -qty, -qty,
                                          reason=f"{prefix}{new_status.upper()}", ref_id=doc_id)

        elif old_zone == 'A' and new_zone == 'C':
            # Pending → Cancelled: only release the reservation, stock stays on shelf
            o_id = update_data.get('order_id', old_data.get('order_id'))
            prefix = f"Order {o_id} " if o_id else ""
            for item in new_items:
                prod = item.get('product')
                qty = float(item.get('quantity', 1))
                if prod:
                    adjust_ready_stock_qty(prod, item.get('color', ''), 0, -qty,
                                          reason=f"{prefix}Reservation Released — {new_status}", ref_id=doc_id)

        elif old_zone == 'B' and new_zone == 'C':
            # Shipped → Cancelled/RTO/Returned: stock physically returns
            o_id = update_data.get('order_id', old_data.get('order_id'))
            prefix = f"Order {o_id} " if o_id else ""
            for item in new_items:
                prod = item.get('product')
                qty = float(item.get('quantity', 1))
                if prod:
                    adjust_ready_stock_qty(prod, item.get('color', ''), qty, 0,
                                          reason=f"{prefix}Restocked — {new_status}", ref_id=doc_id)

        elif old_zone == 'C' and new_zone == 'B':
            # Cancelled → re-shipped (defensive): deduct stock again
            o_id = update_data.get('order_id', old_data.get('order_id'))
            prefix = f"Order {o_id} " if o_id else ""
            for item in new_items:
                prod = item.get('product')
                qty = float(item.get('quantity', 1))
                if prod:
                    adjust_ready_stock_qty(prod, item.get('color', ''), -qty, 0,
                                          reason=f"{prefix}Order Reactivated — {new_status}", ref_id=doc_id)

        elif old_zone == 'C' and new_zone == 'A':
            # Cancelled → Pending (defensive): re-reserve the stock
            o_id = update_data.get('order_id', old_data.get('order_id'))
            prefix = f"Order {o_id} " if o_id else ""
            for item in new_items:
                prod = item.get('product')
                qty = float(item.get('quantity', 1))
                if prod:
                    adjust_ready_stock_qty(prod, item.get('color', ''), 0, qty,
                                          reason=f"{prefix}Re-Reserved — {new_status}", ref_id=doc_id)

        elif old_zone == 'B' and new_zone == 'A':
            # Shipped → Pending (defensive): undo dispatch, re-reserve
            o_id = update_data.get('order_id', old_data.get('order_id'))
            prefix = f"Order {o_id} " if o_id else ""
            for item in new_items:
                prod = item.get('product')
                qty = float(item.get('quantity', 1))
                if prod:
                    adjust_ready_stock_qty(prod, item.get('color', ''), qty, qty,
                                          reason=f"{prefix}Dispatch Reversed — {new_status}", ref_id=doc_id)


def delete_order(doc_id):
    db = get_db()
    doc = db.collection('orders').document(doc_id).get()
    if not doc.exists:
        return False
    data = doc.to_dict()
    
    if data.get('order_items'):
        old_qty_delta, old_res_delta = get_stock_deltas(data.get('status', 'Pending'))
        for item in data['order_items']:
            prod = item.get('product')
            qty = float(item.get('quantity', 1))
            if prod and (old_qty_delta != 0 or old_res_delta != 0):
                o_id = data.get('order_id')
                label = f"Order {o_id} " if o_id else "Order "
                reason = f"{label}deleted (reversed {data.get('status', 'Pending')})"
                adjust_ready_stock_qty(prod, item.get('color', ''), -old_qty_delta * qty, -old_res_delta * qty, reason=reason, ref_id=doc_id)
                
    db.collection('orders').document(doc_id).delete()
    
    # Fix 1: Delete orphaned cashbook entries if this order was logged in the legacy system
    from google.cloud.firestore_v1 import FieldFilter
    cashbook_docs = db.collection('cashbook').where(filter=FieldFilter('reference_id', '==', doc_id)).stream()
    for c_doc in cashbook_docs:
        db.collection('cashbook').document(c_doc.id).delete()

    return True
