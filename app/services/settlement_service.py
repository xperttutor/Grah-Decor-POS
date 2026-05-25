import calendar
from datetime import datetime, timezone
from google.cloud.firestore_v1 import FieldFilter
from app import get_db
from app.services.cashbook_service import add_cashbook_entry
from app.services.inventory_service import adjust_ready_stock_qty, log_inventory_note


def get_unsettled_orders(platform=None):
    db = get_db()
    query = db.collection('orders').where(filter=FieldFilter("status", "in", ["Delivered", "RTO", "Returned"]))
    if platform:
        query = query.where(filter=FieldFilter("platform", "==", platform))
        
    docs = list(query.stream())
    results = []
    for d in docs:
        entry = {'id': d.id, **d.to_dict()}
        if not entry.get('payment_settled'):
            results.append(entry)
            
    # Sort locally by date desc
    results.sort(key=lambda x: x.get('date') or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return results


def create_payment_settlement(platform, utr_number, amount_received, order_ids, settlement_date, notes, platform_deductions=0):
    """Batch-settle selected orders: save snapshot to settlement_batches, mark orders as Settled, log cashbook inflow."""
    if not order_ids:
        return None
        
    db = get_db()
    now = datetime.now(timezone.utc)
    
    if settlement_date:
        try:
            settlement_dt = datetime.strptime(settlement_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            settlement_dt = now
    else:
        settlement_dt = now

    # Build order snapshots for the batch log
    orders_snapshot = []
    for o_id in order_ids:
        doc = db.collection('orders').document(o_id).get()
        if doc.exists:
            data = doc.to_dict()
            orders_snapshot.append({
                'doc_id':          o_id,
                'order_id':        data.get('order_id', ''),
                'customer':        data.get('customer', ''),
                'platform':        data.get('platform', ''),
                'selling_price':   data.get('selling_price', 0),
                'bank_settlement': data.get('bank_settlement', 0),
                'return_type':     data.get('return_type', ''),
            })

    received_amount = float(amount_received)
    deductions   = float(platform_deductions) if platform_deductions else 0.0

    batch_doc = {
        'platform':            platform,
        'utr_number':          utr_number,
        'amount_received':     received_amount,
        'platform_deductions': deductions,
        'order_count':         len(order_ids),
        'order_ids':           order_ids,
        'orders_snapshot':     orders_snapshot,
        'settlement_date':     settlement_dt,
        'notes':               notes,
        'created_at':          now,
    }
    
    _, doc_ref = db.collection('settlement_batches').add(batch_doc)
    
    # Update orders: Delivered → Settled; RTO/Returned → preserve status, mark payment_settled
    batch = db.batch()
    for o_id in order_ids:
        order_ref = db.collection('orders').document(o_id)
        snap = order_ref.get()
        if not snap.exists:
            continue
        current_status = snap.to_dict().get('status', 'Delivered')
        history = snap.to_dict().get('status_history', [])
        if current_status in ('RTO', 'Returned'):
            # Return status is preserved; only mark payment as settled
            history.append({'status': 'Payment Settled', 'timestamp': now.isoformat()})
            batch.update(order_ref, {
                'payment_settled': True,
                'settlement_batch_id': utr_number,
                'status_history': history,
                'updated_at': now,
            })
        else:
            # Delivered → Settled (terminal)
            history.append({'status': 'Settled', 'timestamp': now.isoformat()})
            batch.update(order_ref, {
                'payment_settled': True,
                'settlement_batch_id': utr_number,
                'status': 'Settled',
                'status_history': history,
                'updated_at': now,
            })
    batch.commit()
    
    # Cashbook entry — log the actual received amount as-is (source of truth)
    add_cashbook_entry(
        entry_type='inflow',
        category='Settlement',
        description=f"Platform Payout ({platform}) - UTR: {utr_number}" + (f" | Penalty tracked: ₹{deductions:.0f}" if deductions else ""),
        amount=received_amount,
        reference_id=doc_ref.id
    )
    
    return doc_ref.id


def get_settlement_batches(cursor_id=None, direction='next', limit=20):
    """Fetch settlement batch logs, newest first, paginated."""
    db = get_db()
    query = db.collection('settlement_batches')
    
    cursor_doc = None
    if cursor_id:
        doc_ref = query.document(cursor_id).get()
        if doc_ref.exists:
            cursor_doc = doc_ref

    is_prev = (direction == 'prev' and cursor_doc)
    sort_dir = 'ASCENDING' if is_prev else 'DESCENDING'

    query = query.order_by('created_at', direction=sort_dir)

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
        # Safely expose orders_snapshot avoiding dict.items() collision
        entry['order_lines'] = entry.pop('orders_snapshot', [])
        results.append(entry)
        
    results.sort(key=lambda x: x.get('created_at').isoformat() if hasattr(x.get('created_at'), 'isoformat') else str(x.get('created_at', '')), reverse=True)

    return results, has_prev, has_next


def process_order_return(order_id, return_type, penalty_amount, item_condition):
    """
    Process a return/RTO for an order.
    - return_type: 'rto' or 'customer_return'
    - penalty_amount: float (only for customer_return)
    - item_condition: 'restock' or 'damaged'
    Sets order to terminal status (RTO or Returned).
    If restock, adds items back to ready_stock.
    """
    db = get_db()
    now = datetime.now(timezone.utc)
    
    doc = db.collection('orders').document(order_id).get()
    if not doc.exists:
        return False
    
    order_data = doc.to_dict()

    # Guard: prevent double-processing an already-terminal return
    if order_data.get('status') in ('RTO', 'Returned'):
        return False

    # Determine new status
    new_status = 'RTO' if return_type == 'rto' else 'Returned'
    
    o_id = order_data.get('order_id', '')
    order_label = f"Order {o_id}" if o_id else "Order"

    # Determine bank_settlement: RTO → 0, Customer Return → negative penalty amount
    p_amt = 0.0
    if return_type == 'customer_return' and penalty_amount:
        p_amt = float(penalty_amount)
    bank_settlement_val = -p_amt if p_amt > 0 else 0.0

    update = {
        'status':          new_status,
        'return_type':     return_type,
        'item_condition':  item_condition,
        'bank_settlement': bank_settlement_val,
        'updated_at':      now,
    }

    if p_amt > 0:
        update['penalty_amount'] = p_amt

    # Append to status_history
    existing_history = order_data.get('status_history', [])
    existing_history.append({'status': new_status, 'timestamp': now.isoformat()})
    update['status_history'] = existing_history

    db.collection('orders').document(order_id).update(update)
    
    # Restock items if good condition; log damaged items for audit trail
    order_items = order_data.get('order_items', [])

    if item_condition == 'restock':
        for item in order_items:
            product = item.get('product', '')
            color = item.get('color', '')
            qty = float(item.get('quantity', 1))
            if product:
                reason = f"{order_label} returned - restocked ({new_status})"
                adjust_ready_stock_qty(product, color, qty, 0, reason=reason, ref_id=order_id)
    else:
        # Damaged — stock was already deducted at dispatch; write an audit note per item
        for item in order_items:
            product = item.get('product', '')
            color = item.get('color', '')
            if product:
                reason = f"{order_label} {new_status} — Damaged (not restocked)"
                log_inventory_note('Ready Stock', product, color, reason, reference_id=order_id)
    
    return True


def get_returned_orders():
    """Fetch all orders with status RTO or Returned, newest first."""
    db = get_db()
    docs = db.collection('orders').where(
        filter=FieldFilter("status", "in", ["RTO", "Returned"])
    ).stream()
    results = []
    for d in docs:
        entry = {'id': d.id, **d.to_dict()}
        results.append(entry)
    results.sort(
        key=lambda x: x.get('updated_at') or x.get('created_at') or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True
    )
    return results


def delete_settlement_batch(batch_id):
    """
    Reverse a settlement batch:
    1. Revert all orders to 'Delivered' status
    2. Delete the linked cashbook entry
    3. Delete the batch document
    """
    db = get_db()
    batch_ref = db.collection('settlement_batches').document(batch_id)
    doc = batch_ref.get()
    
    if not doc.exists:
        return False
        
    data = doc.to_dict()
    order_ids = data.get('order_ids', [])
    
    # 1. Revert orders: Settled → Delivered; RTO/Returned → just clear payment_settled
    batch = db.batch()
    now = datetime.now(timezone.utc)
    for o_id in order_ids:
        order_ref = db.collection('orders').document(o_id)
        snap = order_ref.get()
        if snap.exists:
            current_status = snap.to_dict().get('status', 'Settled')
            history = snap.to_dict().get('status_history', [])
            if current_status in ('RTO', 'Returned'):
                # Status was preserved during settlement; just clear the payment flag
                history.append({'status': 'Settlement Reversed', 'timestamp': now.isoformat()})
                batch.update(order_ref, {
                    'payment_settled': False,
                    'settlement_batch_id': '',
                    'status_history': history,
                    'updated_at': now
                })
            else:
                # Was Settled (came from Delivered) → revert to Delivered
                history.append({'status': 'Delivered', 'timestamp': now.isoformat()})
                batch.update(order_ref, {
                    'payment_settled': False,
                    'settlement_batch_id': '',
                    'status': 'Delivered',
                    'status_history': history,
                    'updated_at': now
                })
    batch.commit()
    
    # 2. Delete linked cashbook entry
    cashbook_docs = db.collection('cashbook').where(filter=FieldFilter('reference_id', '==', batch_id)).stream()
    for c_doc in cashbook_docs:
        db.collection('cashbook').document(c_doc.id).delete()
        
    # 3. Delete the batch document
    batch_ref.delete()

def get_settled_orders_charges(year, month, platform=None):
    db = get_db()
    
    _, last_day = calendar.monthrange(year, month)
    start_date = datetime(year, month, 1, tzinfo=timezone.utc)
    end_date = datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=timezone.utc)
    
    query = db.collection('orders').where(filter=FieldFilter('payment_settled', '==', True))
    query = query.where(filter=FieldFilter('date', '>=', start_date))
    query = query.where(filter=FieldFilter('date', '<=', end_date))
    
    if platform:
        query = query.where(filter=FieldFilter('platform', '==', platform))
        
    query = query.order_by('date', direction='DESCENDING')
    
    docs = query.stream()
    
    orders = []
    summary = {
        'selling_price': 0.0,
        'shipping': 0.0,
        'tax': 0.0,
        'marketplace_fee': 0.0,
        'other_charges': 0.0,
        'total_deductions': 0.0
    }
    
    for d in docs:
        data = d.to_dict()
        sp = float(data.get('selling_price', 0))
        sh = float(data.get('shipping', 0))
        tx = float(data.get('tax', 0))
        mf = float(data.get('marketplace_fee', 0))
        oc = float(data.get('other_charges', 0))
        deductions = sh + tx + mf + oc
        
        orders.append({
            'id': d.id,
            'date': data.get('date'),
            'order_id': data.get('order_id', ''),
            'platform': data.get('platform', ''),
            'selling_price': sp,
            'shipping': sh,
            'tax': tx,
            'marketplace_fee': mf,
            'other_charges': oc,
            'total_deductions': deductions
        })
        
        summary['selling_price'] += sp
        summary['shipping'] += sh
        summary['tax'] += tx
        summary['marketplace_fee'] += mf
        summary['other_charges'] += oc
        summary['total_deductions'] += deductions
        
    return orders, summary
