from datetime import datetime, timezone
from google.cloud.firestore_v1 import FieldFilter
from app import get_db


def get_today_transactions():
    """Get all cashbook entries for today."""
    db = get_db()
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    docs = (
        db.collection('cashbook')
        .order_by('date', direction='DESCENDING')
        .stream()
    )
    results = []
    for d in docs:
        entry = {'id': d.id, **d.to_dict()}
        results.append(entry)
    # Filter today's in Python for simplicity
    today_entries = []
    for e in results:
        dt = e.get('date')
        if dt and hasattr(dt, 'date') and dt.date() == now.date():
            today_entries.append(e)
    return today_entries


def get_all_transactions(date_from=None, date_to=None, cursor_id=None, direction='next', limit=20):
    """Get all cashbook entries with optional date filter."""
    db = get_db()
    query = db.collection('cashbook')

    if date_from:
        df = datetime.combine(date_from, datetime.min.time()).replace(tzinfo=timezone.utc)
        query = query.where(filter=FieldFilter('date', '>=', df))
    if date_to:
        dt = datetime.combine(date_to, datetime.max.time()).replace(tzinfo=timezone.utc)
        query = query.where(filter=FieldFilter('date', '<=', dt))

    cursor_doc = None
    if cursor_id:
        doc_ref = db.collection('cashbook').document(cursor_id).get()
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

    results.sort(key=lambda x: x.get('created_at').isoformat() if hasattr(x.get('created_at'), 'isoformat') else str(x.get('created_at', '')), reverse=True)
    results.sort(key=lambda x: x.get('date').isoformat() if hasattr(x.get('date'), 'isoformat') else str(x.get('date', '')), reverse=True)

    return results, has_prev, has_next


def get_running_balance():
    """Calculate total balance = sum(inflows) - sum(outflows)."""
    db = get_db()
    docs = db.collection('cashbook').stream()
    balance = 0.0
    for d in docs:
        data = d.to_dict()
        if data.get('type') == 'inflow':
            balance += data.get('amount', 0)
        else:
            balance -= data.get('amount', 0)
    return balance


def add_cashbook_entry(entry_type, category, description, amount, reference_id='', source='', entry_date=None, receipt_file=None):
    from app.services.storage_service import upload_receipt
    db = get_db()
    now = datetime.now(timezone.utc)
    
    if entry_date:
        if isinstance(entry_date, str):
            try:
                dt = datetime.strptime(entry_date, '%Y-%m-%d').replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                dt = now
        else:
            dt = entry_date
    else:
        dt = now

    receipt_url = upload_receipt(receipt_file) if receipt_file else None

    db.collection('cashbook').add({
        'date': dt,
        'type': entry_type,
        'category': category,
        'description': description,
        'amount': float(amount),
        'reference_id': reference_id,
        'source': source,
        'receipt_url': receipt_url,
        'created_at': now,
    })

def update_cashbook_entry_by_ref(ref_id, amount=None, description=None):
    """Update connected cashbook entries (amount or description) based on the origin reference_id."""
    if not ref_id:
        return
    db = get_db()
    docs = list(db.collection('cashbook').where(
        filter=FieldFilter('reference_id', '==', ref_id)
    ).stream())
    
    updates = {'updated_at': datetime.now(timezone.utc)}
    if amount is not None:
        updates['amount'] = float(amount)
    if description is not None:
        updates['description'] = description
        
    for d in docs:
        db.collection('cashbook').document(d.id).update(updates)
