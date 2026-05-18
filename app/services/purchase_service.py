from datetime import datetime, timezone
from app import get_db
from app.services.inventory_service import adjust_raw_material_qty, add_raw_material
from google.cloud.firestore_v1 import FieldFilter, ArrayUnion

# ---------------------------------------------------------------------------
# Phase 1 — Schema Hardening
# ---------------------------------------------------------------------------
# NEW FIELDS (per-item):
#   ordered_qty   – quantity on the original PO (frozen at creation)
#   received_qty  – quantity actually received so far   (default 0)
#   returned_qty  – quantity returned to vendor so far  (default 0)
#
# NEW FIELDS (PO document):
#   inventory_status  – 'pending' | 'partial' | 'received' | 'returned'
#   payment_status    – 'unpaid'  | 'partial'  | 'paid'
#   amount_paid       – running total of confirmed payments  (default 0.0)
#   balance_due       – total_cost - amount_paid             (default = total_cost)
#   extra_charges     – array of {label, amount, added_at} for freight etc.
#
# BACKWARD COMPATIBILITY:
#   _apply_po_shim() adds missing keys at READ-TIME so older documents
#   never crash the existing UI even before they are written back.
# ---------------------------------------------------------------------------


# ── helpers ─────────────────────────────────────────────────────────────────

def _enrich_items(items):
    """Ensure every item dict carries the Phase-1 qty-tracking keys."""
    enriched = []
    for it in items:
        enriched.append({
            **it,
            'ordered_qty':  float(it.get('ordered_qty',  it.get('quantity', 0))),
            'received_qty': float(it.get('received_qty', 0)),
            'returned_qty': float(it.get('returned_qty', 0)),
        })
    return enriched


def _apply_po_shim(entry: dict) -> dict:
    """
    Read-time shim: back-fills Phase-1 fields that are absent on legacy
    documents so that no template or API consumer receives a KeyError.

    This function is ADDITIVE-ONLY and never writes to Firestore.
    """
    total = float(entry.get('total_cost', 0))

    # -- per-item enrichment --------------------------------------------------
    if 'items' in entry:
        entry['items'] = _enrich_items(entry['items'])

    # -- PO-level financial fields -------------------------------------------
    entry.setdefault('inventory_status', _derive_inventory_status(entry))
    entry.setdefault('payment_status',   _derive_payment_status(entry))
    entry.setdefault('amount_paid',      total if entry.get('status') == 'Paid' else 0.0)
    entry.setdefault('balance_due',      total - entry.get('amount_paid', 0.0))
    entry.setdefault('extra_charges',    [])

    return entry


def _derive_inventory_status(data: dict) -> str:
    """Derive a sensible inventory_status from the legacy 'status' field."""
    status = data.get('status', '')
    mapping = {
        'Received': 'received',
        'Paid':     'received',
        'Returned': 'returned',
        'Cancelled':'returned',
    }
    return mapping.get(status, 'pending')


def _derive_payment_status(data: dict) -> str:
    """Derive a sensible payment_status from the legacy 'status' field."""
    status = data.get('status', '')
    if status == 'Paid':
        return 'paid'
    return 'unpaid'


def _new_po_phase1_fields(total_cost: float, items: list) -> dict:
    """Return the complete set of Phase-1 fields for a brand-new PO."""
    return {
        'inventory_status': 'pending',
        'payment_status':   'unpaid',
        'amount_paid':      0.0,
        'balance_due':      total_cost,
        'extra_charges':    [],
    }


# ── PO number generator ─────────────────────────────────────────────────────

def generate_po_number():
    db = get_db()
    docs = list(db.collection('purchase_orders').order_by('created_at', direction='DESCENDING').limit(1).stream())
    if not docs:
        return "PO-001"

    last_doc = docs[0].to_dict()
    last_id = last_doc.get('po_number', '')
    if last_id.startswith('PO-'):
        try:
            num = int(last_id.replace('PO-', ''))
            return f"PO-{num + 1:03d}"
        except Exception:
            pass
    count = len(list(db.collection('purchase_orders').stream()))
    return f"PO-{count + 1:03d}"


# ── read ─────────────────────────────────────────────────────────────────────

def get_all_purchase_orders(date_from=None, date_to=None, status=None, cursor_id=None, direction='next', limit=20):
    db = get_db()
    query = db.collection('purchase_orders')

    if status:
        query = query.where(filter=FieldFilter('status', '==', status))
        
    if date_from:
        df = datetime.combine(date_from, datetime.min.time()).replace(tzinfo=timezone.utc)
        query = query.where(filter=FieldFilter('created_at', '>=', df))
    if date_to:
        dt = datetime.combine(date_to, datetime.max.time()).replace(tzinfo=timezone.utc)
        query = query.where(filter=FieldFilter('created_at', '<=', dt))

    cursor_doc = None
    if cursor_id:
        doc_ref = db.collection('purchase_orders').document(cursor_id).get()
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
        results.append(_apply_po_shim(entry))

    results.sort(key=lambda x: x.get('created_at').isoformat() if hasattr(x.get('created_at'), 'isoformat') else str(x.get('created_at', '')), reverse=True)

    return results, has_prev, has_next


# ── create ───────────────────────────────────────────────────────────────────

def add_purchase_order(vendor_name, items):
    db = get_db()

    # Enrich items with Phase-1 qty tracking fields
    enriched_items = _enrich_items(items)
    total_cost = sum(float(it['quantity']) * float(it['unit_cost']) for it in enriched_items)
    now = datetime.now(timezone.utc)
    po_number = generate_po_number()

    _, doc_ref = db.collection('purchase_orders').add({
        'po_number':            po_number,
        'vendor_name':          vendor_name,
        'items':                enriched_items,
        'total_cost':           total_cost,
        'status':               'Draft',
        'vendor_invoice_number': '',
        'payment_id':           '',
        'created_at':           now,
        'updated_at':           now,
        'status_history':       [{'status': 'Draft', 'timestamp': now.isoformat()}],
        # ── Phase 1 fields ──────────────────────────────────────────────────
        **_new_po_phase1_fields(total_cost, enriched_items),
    })

    return doc_ref.id


# ── Mark Sent ────────────────────────────────────────────────────────────────

def mark_po_sent(po_id):
    db = get_db()
    now = datetime.now(timezone.utc)
    db.collection('purchase_orders').document(po_id).update({
        'status':         'Sent',
        'updated_at':     now,
        'status_history': ArrayUnion([{'status': 'Sent', 'timestamp': now.isoformat()}]),
    })


# ── Mark Received ────────────────────────────────────────────────────────────

def mark_po_received(po_id):
    db = get_db()

    doc = db.collection('purchase_orders').document(po_id).get()
    if not doc.exists:
        return False
    data = doc.to_dict()

    if data.get('status') in ['Received', 'Paid']:
        return False

    now = datetime.now(timezone.utc)

    # Build the updated items list with received_qty = ordered_qty (full receipt)
    raw_items = data.get('items', [])
    if not raw_items and data.get('item'):
        raw_items = [{'item': data.get('item'), 'quantity': data.get('quantity'), 'unit_cost': data.get('unit_cost', 0)}]

    updated_items = []
    for it in raw_items:
        ordered = float(it.get('ordered_qty', it.get('quantity', 0)))
        updated_items.append({
            **it,
            'ordered_qty':  ordered,
            'received_qty': ordered,   # full receipt
            'returned_qty': float(it.get('returned_qty', 0)),
        })

    db.collection('purchase_orders').document(po_id).update({
        'status':           'Received',
        'inventory_status': 'received',
        'items':            updated_items,
        'updated_at':       now,
        'status_history':   ArrayUnion([{'status': 'Received', 'timestamp': now.isoformat()}]),
    })

    # Increment inventory
    po_number = data.get('po_number', po_id)
    reason = f"PO {po_number} Received"

    for it in updated_items:
        item_name = it.get('item')
        qty        = float(it.get('received_qty', 0))
        unit_cost  = float(it.get('unit_cost', 0))
        if not adjust_raw_material_qty(item_name, qty, reason=reason, price=unit_cost):
            add_raw_material(item_name, qty, 'pcs', reason=reason, price=unit_cost)

    return True


# ── Mark Paid ────────────────────────────────────────────────────────────────

def mark_po_paid(po_id, payment_id):
    from app.services.cashbook_service import add_cashbook_entry
    db = get_db()

    doc = db.collection('purchase_orders').document(po_id).get()
    if not doc.exists:
        return False
    data = doc.to_dict()

    if data.get('status') == 'Paid' or data.get('payment_status') == 'paid':
        return False

    now = datetime.now(timezone.utc)
    total_cost = float(data.get('total_cost', 0))

    db.collection('purchase_orders').document(po_id).update({
        'status':         'Paid',
        'payment_id':     payment_id,
        # ── Phase 1 financial fields ─────────────────────────────────────
        'payment_status': 'paid',
        'amount_paid':    total_cost,
        'balance_due':    0.0,
        # ────────────────────────────────────────────────────────────────
        'updated_at':     now,
        'status_history': ArrayUnion([{'status': 'Paid', 'timestamp': now.isoformat()}]),
    })

    po_number = data.get('po_number', po_id)
    vendor    = data.get('vendor_name', 'Unknown')
    desc = f"{po_number} Paid to {vendor}"
    if payment_id:
        desc += f" - Txn: {payment_id}"

    add_cashbook_entry(
        entry_type='outflow',
        category='Purchase',
        description=desc,
        amount=total_cost,
        reference_id=po_id,
    )

    return True


# ── Cancel ───────────────────────────────────────────────────────────────────

def cancel_po(po_id):
    db = get_db()

    doc = db.collection('purchase_orders').document(po_id).get()
    if not doc.exists:
        return False
    data = doc.to_dict()

    old_status = data.get('status')
    if old_status == 'Cancelled':
        return True

    now = datetime.now(timezone.utc)

    # Preserve payment history if the PO was already paid; only zero out if it was never paid.
    already_paid = data.get('payment_status') == 'paid'
    cancel_update = {
        'status':           'Cancelled',
        'inventory_status': 'returned',
        'balance_due':      0.0,
        'updated_at':       now,
        'status_history':   ArrayUnion([{'status': 'Cancelled', 'timestamp': now.isoformat()}]),
    }
    if already_paid:
        cancel_update['cancellation_refunded'] = True   # refund logged below; payment_status kept
    else:
        cancel_update['payment_status'] = 'unpaid'

    db.collection('purchase_orders').document(po_id).update(cancel_update)

    # If it was received / paid → reverse inventory
    if old_status in ['Received', 'Paid']:
        po_number = data.get('po_number', po_id)
        reason    = f"PO {po_number} Cancelled (Reversal)"

        items = data.get('items', [])
        if not items and data.get('item'):
            items = [{'item': data.get('item'), 'quantity': data.get('quantity')}]

        # Reset per-item received_qty to 0 on the items array
        zeroed_items = []
        for it in items:
            item_name  = it.get('item')
            recv_qty   = float(it.get('received_qty', it.get('quantity', 0)))
            zeroed_items.append({**it, 'received_qty': 0.0})
            adjust_raw_material_qty(item_name, -recv_qty, reason=reason)

        db.collection('purchase_orders').document(po_id).update({'items': zeroed_items})

        # If explicitly paid → log a refund inflow
        if old_status == 'Paid':
            from app.services.cashbook_service import add_cashbook_entry
            vendor = data.get('vendor_name', 'Unknown')
            add_cashbook_entry(
                entry_type='inflow',
                category='Refund',
                description=f"Refund: {po_number} Cancelled (from {vendor})",
                amount=data.get('total_cost', 0),
                reference_id=po_id,
            )

    return True


# ── Return ───────────────────────────────────────────────────────────────────

def return_po(po_id, refund_amount=0):
    db = get_db()

    doc = db.collection('purchase_orders').document(po_id).get()
    if not doc.exists:
        return False
    data = doc.to_dict()

    status = data.get('status')
    if status not in ['Received', 'Paid']:
        return False

    now = datetime.now(timezone.utc)

    items = data.get('items', [])
    if not items and data.get('item'):
        items = [{'item': data.get('item'), 'quantity': data.get('quantity')}]

    # Mark every item as fully returned
    returned_items = []
    for it in items:
        ordered  = float(it.get('ordered_qty',  it.get('quantity', 0)))
        received = float(it.get('received_qty', ordered))
        returned_items.append({
            **it,
            'returned_qty': received,   # everything received is now returned
        })

    db.collection('purchase_orders').document(po_id).update({
        'status':           'Returned',
        # ── Phase 1 ──────────────────────────────────────────────────────
        'inventory_status': 'returned',
        'items':            returned_items,
        # ────────────────────────────────────────────────────────────────
        'updated_at':       now,
        'status_history':   ArrayUnion([{'status': 'Returned', 'timestamp': now.isoformat()}]),
    })

    # Reverse inventory — use received_qty (what we actually have in stock)
    po_number = data.get('po_number', po_id)
    reason    = f"PO {po_number} Returned"

    for it in returned_items:
        item_name = it.get('item')
        qty       = float(it.get('returned_qty', 0))
        adjust_raw_material_qty(item_name, -qty, reason=reason)

    if status == 'Paid':
        from app.services.cashbook_service import add_cashbook_entry
        vendor = data.get('vendor_name', 'Unknown')
        refund = float(refund_amount)
        if refund > 0:
            add_cashbook_entry(
                entry_type='inflow',
                category='Refund',
                description=f"Refund: {po_number} Returned (from {vendor})",
                amount=refund,
                reference_id=po_id,
            )

    return True


# =============================================================================
# Phase 2 — Partial Fulfillment Engine
# =============================================================================
# Three new functions that operate on INDIVIDUAL QUANTITIES rather than the
# whole PO.  The old mark_po_received / mark_po_paid / return_po functions
# remain untouched for backward compatibility.
#
# Allowed inventory_status transitions:
#   pending  → partially_received → received
#                                  → returned  (if all received qty is returned)
#
# Allowed payment_status transitions:
#   unpaid → partially_paid → paid
# =============================================================================


# ── Partial Receive ───────────────────────────────────────────────────────────

def partial_receive_po(po_id: str, received_quantities: dict) -> dict:
    """
    Record receipt of SOME (or all) items on a PO.

    Parameters
    ----------
    po_id : str
        Firestore document ID of the purchase order.
    received_quantities : dict
        Mapping of ``item_name → qty_received_this_shipment``.
        Only items present in this dict are updated.
        Example: ``{"Fabric A": 50, "Thread B": 200}``

    Returns
    -------
    dict  with keys:
        success      : bool
        inventory_status : new value written to Firestore
        message      : human-readable summary
        items_updated : list of item names that were updated
    """
    db = get_db()

    doc = db.collection('purchase_orders').document(po_id).get()
    if not doc.exists:
        return {'success': False, 'message': 'PO not found.'}
    data = doc.to_dict()

    # Guard: cannot receive against Cancelled / Returned POs
    if data.get('status') in ['Cancelled', 'Returned']:
        return {'success': False, 'message': f"Cannot receive items on a {data.get('status')} PO."}

    now = datetime.now(timezone.utc)
    po_number = data.get('po_number', po_id)
    reason    = f"PO {po_number} Partial Receipt"

    raw_items = data.get('items', [])
    if not raw_items and data.get('item'):
        raw_items = [{
            'item':       data.get('item'),
            'quantity':   data.get('quantity'),
            'unit_cost':  data.get('unit_cost', 0),
            'ordered_qty': data.get('quantity'),
        }]

    updated_items  = []
    items_updated  = []
    total_ordered  = 0.0
    total_received = 0.0

    for it in raw_items:
        item_name  = it.get('item', '')
        ordered    = float(it.get('ordered_qty',  it.get('quantity', 0)))
        prev_recv  = float(it.get('received_qty', 0))
        returned   = float(it.get('returned_qty', 0))
        unit_cost  = float(it.get('unit_cost', 0))

        # How many are we receiving right now?
        incoming = float(received_quantities.get(item_name, 0))

        # Clamp: cannot receive more than what is still outstanding
        outstanding = ordered - prev_recv
        if incoming < 0:
            incoming = 0.0
        if incoming > outstanding:
            incoming = outstanding

        new_recv = prev_recv + incoming

        updated_it = {
            **it,
            'ordered_qty':  ordered,
            'received_qty': new_recv,
            'returned_qty': returned,
        }
        updated_items.append(updated_it)
        total_ordered  += ordered
        total_received += new_recv

        # Update physical inventory for the delta only
        if incoming > 0:
            items_updated.append(item_name)
            if not adjust_raw_material_qty(item_name, incoming, reason=reason, price=unit_cost):
                add_raw_material(item_name, incoming, 'pcs', reason=reason, price=unit_cost)

    # Derive the new inventory_status
    if total_received <= 0:
        new_inv_status = 'pending'
    elif total_received < total_ordered:
        new_inv_status = 'partially_received'
    else:
        new_inv_status = 'received'

    # Derive the new high-level status (only promote, never demote)
    old_status = data.get('status', 'Draft')
    new_status = old_status
    if new_inv_status == 'received' and old_status not in ['Paid', 'Received']:
        new_status = 'Received'
    elif new_inv_status == 'partially_received' and old_status in ['Draft', 'Sent']:
        new_status = 'Partially Received'

    db.collection('purchase_orders').document(po_id).update({
        'items':            updated_items,
        'inventory_status': new_inv_status,
        'status':           new_status,
        'updated_at':       now,
        'status_history':   ArrayUnion([{
            'status':    f"Partial Receipt ({', '.join(items_updated) or 'none'})",
            'timestamp': now.isoformat(),
        }]),
    })

    return {
        'success':          True,
        'inventory_status': new_inv_status,
        'items_updated':    items_updated,
        'message': (
            f"Received {len(items_updated)} item(s). "
            f"Inventory status → {new_inv_status}."
        ),
    }


# ── Partial Payment ───────────────────────────────────────────────────────────

def partial_pay_po(
    po_id: str,
    payment_amount: float,
    payment_reference: str = '',
    extra_charges: list | None = None,
) -> dict:
    """
    Record a (partial or full) payment against a PO.

    Optionally accepts extra vendor charges (freight, handling, etc.) that are
    added to ``total_cost`` BEFORE the payment is applied.  Each charge is
    appended to the PO's ``extra_charges`` audit array.

    Parameters
    ----------
    po_id : str
        Firestore document ID.
    payment_amount : float
        Amount being paid right now (must be > 0).
    payment_reference : str
        UTR / cheque number / notes for this payment.
    extra_charges : list of dicts, optional
        Each dict must have ``label`` (str) and ``amount`` (float).
        Example: ``[{"label": "Freight", "amount": 350.0}]``

    Returns
    -------
    dict  with keys:
        success        : bool
        payment_status : new value written to Firestore
        amount_paid    : cumulative total after this payment
        balance_due    : remaining balance after this payment
        message        : human-readable summary
    """
    from app.services.cashbook_service import add_cashbook_entry

    if float(payment_amount) <= 0:
        return {'success': False, 'message': 'Payment amount must be greater than zero.'}

    db = get_db()
    doc = db.collection('purchase_orders').document(po_id).get()
    if not doc.exists:
        return {'success': False, 'message': 'PO not found.'}
    data = doc.to_dict()

    if data.get('payment_status') == 'paid':
        return {'success': False, 'message': 'PO is already fully paid.'}

    if data.get('status') in ['Cancelled', 'Returned']:
        return {'success': False, 'message': f"Cannot pay against a {data.get('status')} PO."}

    now        = datetime.now(timezone.utc)
    po_number  = data.get('po_number', po_id)
    vendor     = data.get('vendor_name', 'Unknown')
    payment_amount = float(payment_amount)

    # ── 1. Process extra charges first ───────────────────────────────────────
    extra_charges = extra_charges or []
    extra_total   = 0.0
    charges_audit = list(data.get('extra_charges', []))
    charges_desc_parts = []

    for charge in extra_charges:
        label  = str(charge.get('label', 'Extra Charge')).strip()
        amount = float(charge.get('amount', 0))
        if amount <= 0:
            continue
        extra_total += amount
        charges_audit.append({
            'label':    label,
            'amount':   amount,
            'added_at': now.isoformat(),
            'added_with_payment': payment_reference or '(no ref)',
        })
        charges_desc_parts.append(f"{label}: ₹{amount:,.2f}")

    # ── 2. Update total_cost if extra charges were added ─────────────────────
    old_total   = float(data.get('total_cost', 0))
    new_total   = old_total + extra_total
    prev_paid   = float(data.get('amount_paid', 0))

    # ── 3. Apply payment ─────────────────────────────────────────────────────
    # The total cash going out this transaction = payment_amount + extra_total.
    # Clamp the combined outflow so we never record more than what is owed.
    total_cash_out = payment_amount + extra_total
    max_payable    = new_total - prev_paid          # = old balance + new charges
    if total_cash_out > max_payable:
        # Proportionally reduce payment_amount; extra_total is already fixed
        payment_amount = max(0.0, max_payable - extra_total)
        total_cash_out = payment_amount + extra_total

    new_paid    = prev_paid + total_cash_out        # ← includes extra charges
    new_balance = new_total - new_paid

    # Round off tiny floating-point residuals
    if abs(new_balance) < 0.005:
        new_balance = 0.0

    new_pay_status = 'paid' if new_balance <= 0 else 'partially_paid'

    # Derive high-level status
    old_status = data.get('status', '')
    new_status = 'Paid' if new_pay_status == 'paid' else old_status

    # ── 4. Build cashbook description ────────────────────────────────────────
    desc_parts = [f"{po_number} Payment to {vendor}"]
    if charges_desc_parts:
        desc_parts.append("incl. " + ", ".join(charges_desc_parts))
    if payment_reference:
        desc_parts.append(f"Txn: {payment_reference}")
    cashbook_desc = " — ".join(desc_parts)

    # ── 5. Firestore update ──────────────────────────────────────────────────
    firestore_update = {
        'payment_status': new_pay_status,
        'amount_paid':    new_paid,
        'balance_due':    new_balance,
        'extra_charges':  charges_audit,
        'updated_at':     now,
        'status_history': ArrayUnion([{
            'status':    f"Payment ₹{total_cash_out:,.2f} ({new_pay_status})",
            'timestamp': now.isoformat(),
        }]),
    }
    if extra_total > 0:
        firestore_update['total_cost'] = new_total
    if new_status != old_status:
        firestore_update['status'] = new_status

    # ── 5. Cashbook entry FIRST — if this fails, the PO is not marked paid ──────
    add_cashbook_entry(
        entry_type='outflow',
        category='Purchase',
        description=cashbook_desc,
        amount=total_cash_out,   # one combined outflow
        reference_id=po_id,
    )

    # ── 6. Firestore update — only after cashbook is safely written ───────────
    db.collection('purchase_orders').document(po_id).update(firestore_update)

    return {
        'success':        True,
        'payment_status': new_pay_status,
        'amount_paid':    new_paid,
        'balance_due':    new_balance,
        'extra_total_added': extra_total,
        'message': (
            f"Payment of ₹{payment_amount:,.2f} recorded"
            + (f" (+ ₹{extra_total:,.2f} extra charges)" if extra_total else "")
            + f". Balance due: ₹{new_balance:,.2f}."
        ),
    }


# ── Partial Return ────────────────────────────────────────────────────────────

def partial_return_po(
    po_id: str,
    return_quantities: dict,
    refund_amount: float = 0,
    reason_note: str = '',
) -> dict:
    """
    Return SOME (or all) received items back to the vendor.

    Parameters
    ----------
    po_id : str
        Firestore document ID.
    return_quantities : dict
        Mapping of ``item_name → qty_being_returned``.
        Only items present in this dict are affected.
        Example: ``{"Fabric A": 10}``
    refund_amount : float, optional
        Cash refund received from vendor for these items.
        If > 0, logged as an inflow in the cashbook.
    reason_note : str, optional
        Short note recorded in status_history (e.g. "Defective batch").

    Returns
    -------
    dict  with keys:
        success          : bool
        inventory_status : new value written to Firestore
        items_returned   : list of item names that were updated
        message          : human-readable summary
    """
    db = get_db()

    doc = db.collection('purchase_orders').document(po_id).get()
    if not doc.exists:
        return {'success': False, 'message': 'PO not found.'}
    data = doc.to_dict()

    # Guard: can only return from a received (or paid) PO
    if data.get('inventory_status') not in ['received', 'partially_received']:
        return {
            'success': False,
            'message': (
                'Can only return items from a PO with inventory_status '
                '"received" or "partially_received". '
                f'Current: "{data.get("inventory_status")}".'
            ),
        }

    now       = datetime.now(timezone.utc)
    po_number = data.get('po_number', po_id)
    reason    = f"PO {po_number} Partial Return" + (f" — {reason_note}" if reason_note else "")

    raw_items          = data.get('items', [])
    updated_items      = []
    items_returned     = []
    total_ordered      = 0.0
    total_received     = 0.0
    total_returned     = 0.0
    total_return_value = 0.0   # ← financial deduction accumulator

    for it in raw_items:
        item_name = it.get('item', '')
        ordered   = float(it.get('ordered_qty',  it.get('quantity', 0)))
        received  = float(it.get('received_qty', 0))
        prev_ret  = float(it.get('returned_qty', 0))

        # How many are we returning right now?
        returning = float(return_quantities.get(item_name, 0))

        # Clamp: cannot return more than net-held (received - already returned)
        net_held = received - prev_ret
        if returning < 0:
            returning = 0.0
        if returning > net_held:
            returning = net_held

        new_ret = prev_ret + returning

        # ── Financial deduction for returned items ────────────────────────
        unit_cost = float(it.get('unit_cost', 0))
        return_value = returning * unit_cost          # value being deducted

        updated_it = {
            **it,
            'ordered_qty':  ordered,
            'received_qty': received,
            'returned_qty': new_ret,
        }
        updated_items.append(updated_it)
        total_ordered        += ordered
        total_received       += received
        total_returned       += new_ret
        total_return_value   += return_value

        # Reduce physical inventory for items being returned
        if returning > 0:
            items_returned.append(item_name)
            adjust_raw_material_qty(item_name, -returning, reason=reason)

    # Derive the new inventory_status
    net_in_stock = total_received - total_returned
    if net_in_stock <= 0 and total_returned > 0:
        new_inv_status = 'returned'
    elif total_returned > 0:
        new_inv_status = 'partially_received'   # some still held
    else:
        # Nothing actually changed
        new_inv_status = data.get('inventory_status', 'pending')

    history_note = f"Partial Return ({', '.join(items_returned) or 'none'})"
    if reason_note:
        history_note += f" — {reason_note}"

    # ── Recalculate financials after return ───────────────────────────────
    old_total    = float(data.get('total_cost', 0))
    amount_paid  = float(data.get('amount_paid', 0))
    new_total    = max(0.0, old_total - total_return_value)
    new_balance  = new_total - amount_paid
    # Round off float dust
    if abs(new_balance) < 0.005:
        new_balance = 0.0
    if new_balance <= 0 and new_balance > -0.005:
        new_balance = 0.0
    new_pay_status = (
        'paid'            if new_balance <= 0 and amount_paid >= new_total - 0.005
        else 'partially_paid' if amount_paid > 0.005
        else 'unpaid'
    )

    firestore_payload = {
        'items':            updated_items,
        'inventory_status': new_inv_status,
        'total_cost':       new_total,
        'balance_due':      new_balance,
        'payment_status':   new_pay_status,
        'updated_at':       now,
        'status_history':   ArrayUnion([{
            'status':    history_note,
            'timestamp': now.isoformat(),
        }]),
    }

    db.collection('purchase_orders').document(po_id).update(firestore_payload)

    # Log vendor refund if provided
    if float(refund_amount) > 0:
        from app.services.cashbook_service import add_cashbook_entry
        vendor = data.get('vendor_name', 'Unknown')
        desc   = f"Partial Refund: {po_number} Return from {vendor}"
        if reason_note:
            desc += f" ({reason_note})"
        add_cashbook_entry(
            entry_type='inflow',
            category='Refund',
            description=desc,
            amount=float(refund_amount),
            reference_id=po_id,
        )

    return {
        'success':          True,
        'inventory_status': new_inv_status,
        'items_returned':   items_returned,
        'message': (
            f"Returned {len(items_returned)} item type(s). "
            f"Inventory status → {new_inv_status}."
            + (f" Refund ₹{float(refund_amount):,.2f} logged." if float(refund_amount) > 0 else "")
        ),
    }


# ── Log Refund (Automatic Balance Adjustment) ─────────────────────────────────

def log_refund(po_id: str, refund_amount: float, reference: str = '') -> dict:
    """
    Record collection of a vendor refund against a PO whose balance_due is
    negative (i.e. the vendor owes us money after a partial return).

    This is the financial counterpart to partial_return_po and is completely
    decoupled from inventory — it only touches money.

    Parameters
    ----------
    po_id         : Firestore document ID.
    refund_amount : Cash received from vendor (must be > 0 and ≤ abs(balance_due)).
    reference     : UTR / cheque number / notes for this transaction.

    Returns
    -------
    dict  with keys: success, balance_due, payment_status, message
    """
    from app.services.cashbook_service import add_cashbook_entry

    refund_amount = float(refund_amount)
    if refund_amount <= 0:
        return {'success': False, 'message': 'Refund amount must be greater than zero.'}

    db = get_db()
    doc = db.collection('purchase_orders').document(po_id).get()
    if not doc.exists:
        return {'success': False, 'message': 'PO not found.'}
    data = doc.to_dict()

    current_balance = float(data.get('balance_due', 0))
    if current_balance >= 0:
        return {
            'success': False,
            'message': (
                f'No refund is due on this PO. '
                f'Current balance_due is ₹{current_balance:,.2f}.'
            ),
        }

    # Maximum refundable = abs(negative balance)
    max_refund = abs(current_balance)
    if refund_amount > max_refund:
        refund_amount = max_refund   # clamp silently

    now       = datetime.now(timezone.utc)
    po_number = data.get('po_number', po_id)
    vendor    = data.get('vendor_name', 'Unknown')

    # Reduce amount_paid (vendor gave cash back, so our net outflow decreases)
    prev_paid    = float(data.get('amount_paid', 0))
    new_paid     = prev_paid - refund_amount
    new_balance  = float(data.get('total_cost', 0)) - new_paid

    # Clamp float residuals
    if abs(new_balance) < 0.005:
        new_balance = 0.0

    # Derive payment_status
    if new_balance <= 0:
        new_pay_status = 'paid'
    elif new_paid <= 0:
        new_pay_status = 'unpaid'
    else:
        new_pay_status = 'partially_paid'

    # Build payment_history entry
    payment_history_entry = {
        'type':      'Refund',
        'amount':    refund_amount,
        'reference': reference,
        'timestamp': now.isoformat(),
    }

    db.collection('purchase_orders').document(po_id).update({
        'amount_paid':     new_paid,
        'balance_due':     new_balance,
        'payment_status':  new_pay_status,
        'updated_at':      now,
        'payment_history': ArrayUnion([payment_history_entry]),
        'status_history':  ArrayUnion([{
            'status':    f'Refund Collected ₹{refund_amount:,.2f}',
            'timestamp': now.isoformat(),
        }]),
    })

    # Log cashbook inflow
    desc = f"Refund for {po_number} from {vendor}"
    if reference:
        desc += f" — Ref: {reference}"
    add_cashbook_entry(
        entry_type='inflow',
        category='Refund',
        description=desc,
        amount=refund_amount,
        reference_id=po_id,
    )

    return {
        'success':        True,
        'balance_due':    new_balance,
        'payment_status': new_pay_status,
        'message': (
            f"Refund of ₹{refund_amount:,.2f} collected and logged. "
            f"New balance_due: ₹{new_balance:,.2f}."
        ),
    }
