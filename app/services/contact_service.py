from datetime import datetime, timezone
from app import get_db
from google.cloud.firestore_v1 import ArrayUnion

def generate_vendor_id():
    db = get_db()
    docs = list(db.collection('vendors').order_by('created_at', direction='DESCENDING').limit(1).stream())
    if not docs:
        return "GDV-0001"
    
    last_doc = docs[0].to_dict()
    last_id = last_doc.get('vendor_id', '')
    if last_id.startswith('GDV-'):
        try:
            num = int(last_id.replace('GDV-', ''))
            return f"GDV-{num + 1:04d}"
        except:
            pass
    count = len(list(db.collection('vendors').stream()))
    return f"GDV-{count + 1:04d}"

def generate_customer_id():
    db = get_db()
    docs = list(db.collection('customers').order_by('created_at', direction='DESCENDING').limit(1).stream())
    if not docs:
        return "GDC-0001"
    
    last_doc = docs[0].to_dict()
    last_id = last_doc.get('customer_id', '')
    if last_id.startswith('GDC-'):
        try:
            num = int(last_id.replace('GDC-', ''))
            return f"GDC-{num + 1:04d}"
        except:
            pass
    count = len(list(db.collection('customers').stream()))
    return f"GDC-{count + 1:04d}"

def get_all_vendors():
    db = get_db()
    docs = db.collection('vendors').order_by('created_at', direction='DESCENDING').stream()
    return [{'id': d.id, **d.to_dict()} for d in docs]

def get_all_customers(cursor_id=None, direction='next', limit=20):
    db = get_db()
    query = db.collection('customers')
    
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
        row = {'id': d.id, **d.to_dict()}
        # Normalize platform_used: legacy single-string → list
        pu = row.get('platform_used')
        if isinstance(pu, str):
            row['platform_used'] = [pu] if pu else []
        elif pu is None:
            row['platform_used'] = []
        # Ensure order_ids is always a list
        if not isinstance(row.get('order_ids'), list):
            row['order_ids'] = []
        # Computed backward-compat property
        row['recent_order_id'] = row['order_ids'][-1] if row['order_ids'] else row.get('recent_order_id', '')
        results.append(row)
        
    results.sort(key=lambda x: x.get('created_at').isoformat() if hasattr(x.get('created_at'), 'isoformat') else str(x.get('created_at', '')), reverse=True)
    return results, has_prev, has_next

def add_vendor(name, phone_numbers):
    db = get_db()
    vendor_id = generate_vendor_id()
    
    if not phone_numbers:
        phone_numbers = ["Not available"]
        
    doc_ref = db.collection('vendors').document()
    doc_ref.set({
        'vendor_id': vendor_id,
        'name': name,
        'phone_numbers': phone_numbers,
        'created_at': datetime.now(timezone.utc)
    })
    return vendor_id

def add_customer(name, phone_numbers, platform_used=None, recent_order_id=None):
    """
    Add or merge a customer record.

    Rules (in order):
    1. If a non-empty phone number is provided, look for any existing
       customer that shares that number; if found, merge and return its ID.
       NOTE: Unknown/Walk-in customers always create a new record regardless
       of any existing Unknown entries — each one is a distinct person.
    2. Otherwise create a brand-new customer document.
    """
    db = get_db()
    new_order_ids = [recent_order_id] if recent_order_id else []
    new_platforms = [platform_used]   if platform_used   else []

    # ── Rule 1: Phone-based deduplication (skip for Unknown customers) ───────
    clean_phones = [p for p in (phone_numbers or []) if p and p != 'Not available']
    if clean_phones and name != 'Unknown':
        for phone in clean_phones:
            matches = list(
                db.collection('customers')
                  .where('phone_numbers', 'array_contains', phone)
                  .limit(1)
                  .stream()
            )
            if matches:
                doc = matches[0]
                updates = {'updated_at': datetime.now(timezone.utc)}
                if new_order_ids:
                    updates['order_ids'] = ArrayUnion(new_order_ids)
                if new_platforms:
                    updates['platform_used'] = ArrayUnion(new_platforms)
                doc.reference.update(updates)
                return doc.to_dict().get('customer_id', doc.id)

    # ── Rule 2: Create new customer record ───────────────────────────────────
    if not phone_numbers:
        phone_numbers = ["Not available"]

    customer_id = generate_customer_id()
    doc_ref = db.collection('customers').document()
    doc_ref.set({
        'customer_id': customer_id,
        'name': name,
        'phone_numbers': phone_numbers,
        'platform_used': new_platforms,
        'order_ids': new_order_ids,
        'created_at': datetime.now(timezone.utc)
    })
    return customer_id

def update_customer_metadata(customer_doc_id, platform_used=None, recent_order_id=None):
    """
    Append a new order ID and/or platform to the customer document.
    Uses ArrayUnion so duplicates are never introduced.
    """
    db = get_db()
    doc_ref = db.collection('customers').document(customer_doc_id)
    updates = {}
    if platform_used:
        updates['platform_used'] = ArrayUnion([platform_used])
    if recent_order_id:
        updates['order_ids'] = ArrayUnion([recent_order_id])

    if updates:
        updates['updated_at'] = datetime.now(timezone.utc)
        doc_ref.update(updates)

def update_vendor(vendor_doc_id, name, phone_numbers):
    db = get_db()
    doc_ref = db.collection('vendors').document(vendor_doc_id)
    
    if not phone_numbers:
        phone_numbers = ["Not available"]
        
    doc_ref.update({
        'name': name,
        'phone_numbers': phone_numbers,
        'updated_at': datetime.now(timezone.utc)
    })

def update_customer(customer_doc_id, name, phone_numbers):
    db = get_db()
    doc_ref = db.collection('customers').document(customer_doc_id)

    if not phone_numbers:
        phone_numbers = ["Not available"]

    doc_ref.update({
        'name': name,
        'phone_numbers': phone_numbers,
        'updated_at': datetime.now(timezone.utc)
    })


def get_customer_lifetime_value(customer_id):
    """
    Sum bank_settlement for all orders whose 'customer_id' field equals
    the given GDC-XXXX ID and whose status is Delivered or Settled.
    Querying by customer_id (not name) keeps Unknown records independent.
    Returns the total as a float.
    """
    db = get_db()
    from google.cloud.firestore_v1 import FieldFilter
    docs = (
        db.collection('orders')
          .where(filter=FieldFilter('customer_id', '==', customer_id))
          .stream()
    )
    total = 0.0
    settled_statuses = {'Settled'}
    for d in docs:
        data = d.to_dict()
        if data.get('status') in settled_statuses:
            total += float(data.get('bank_settlement', 0) or 0)
    return total
