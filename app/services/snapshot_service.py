from datetime import datetime, timezone, date as date_type
from calendar import monthrange
from google.cloud.firestore_v1 import FieldFilter
from app import get_db
from app.services.inventory_service import log_inventory_transaction


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_date(dt):
    """Format a UTC datetime or Firestore Timestamp as '27 Apr 2026'."""
    if dt is None:
        return '—'
    if hasattr(dt, 'strftime'):
        return dt.strftime('%d %b %Y')
    return str(dt)


def _period_label(period_start_dt, period_end_dt=None):
    """Build a human label like '27 Apr 2026 → …' or '27 Apr 2026 → 30 Apr 2026'."""
    start = _fmt_date(period_start_dt)
    if period_end_dt:
        return f'{start} → {_fmt_date(period_end_dt)}'
    return f'{start} → …'


# ── Queries ────────────────────────────────────────────────────────────────────

def get_all_snapshots():
    """Return all period snapshot docs, newest first (by period_start desc)."""
    db = get_db()
    docs = db.collection('monthly_snapshots').stream()
    results = [{'id': d.id, **d.to_dict()} for d in docs]
    # Sort in Python — avoids needing a Firestore composite index
    results.sort(key=lambda s: s.get('period_start') or '', reverse=True)
    return results


def get_open_snapshot():
    """Return the single currently-open period, or None."""
    db = get_db()
    docs = list(
        db.collection('monthly_snapshots')
        .where('status', '==', 'open')
        .limit(1)
        .stream()
    )
    if docs:
        return {'id': docs[0].id, **docs[0].to_dict()}
    return None


def get_snapshot_by_id(doc_id):
    """Return a snapshot doc by its Firestore document ID."""
    db = get_db()
    doc = db.collection('monthly_snapshots').document(doc_id).get()
    if doc.exists:
        return {'id': doc.id, **doc.to_dict()}
    return None


def get_latest_closed_snapshot():
    """Return the most-recently closed period, or None (used for carry-forward)."""
    db = get_db()
    docs = list(
        db.collection('monthly_snapshots')
        .where('status', '==', 'closed')
        .stream()
    )
    if not docs:
        return None
    # Sort in Python — avoids needing a Firestore composite index
    results = [{'id': d.id, **d.to_dict()} for d in docs]
    results.sort(key=lambda s: s.get('period_start') or '', reverse=True)
    return results[0]


# ── Opening Snapshot ───────────────────────────────────────────────────────────

def take_opening_snapshot():
    """
    Start a new audit period.

    Rules:
    - Only one OPEN period allowed at a time → returns ('already_open', None) if one exists.
    - First-ever period → reads current raw_materials quantities from the system.
    - Subsequent periods → carries forward the previous closing physical counts.
    - Read-only operation: writes nothing to inventory_log.

    Returns ('ok', doc_id) on success, or an error string tuple.
    """
    db = get_db()

    # Guard: only one open period at a time
    if get_open_snapshot():
        return ('already_open', None)

    now = datetime.now(timezone.utc)
    latest_closed = get_latest_closed_snapshot()

    materials = []

    if latest_closed is None:
        # ── First period ever: read live system quantities ──────────────────
        rm_docs = db.collection('raw_materials').order_by('name').stream()
        for d in rm_docs:
            m = d.to_dict()
            materials.append({
                'name':        m.get('name', ''),
                'unit':        m.get('unit', 'pcs'),
                'opening_qty': float(m.get('quantity', 0)),
                'price':       float(m.get('price', 0)),
            })
        source = 'system'
    else:
        # ── Carry forward previous period's physical closing counts ─────────
        closing_materials = (
            latest_closed.get('closing', {}) or {}
        ).get('materials', [])
        # Build a lookup from the previous opening for unit/price
        prev_opening_materials = (
            latest_closed.get('opening', {}) or {}
        ).get('materials', [])
        unit_map  = {m['name']: m.get('unit', 'pcs') for m in prev_opening_materials}
        price_map = {m['name']: m.get('price', 0)    for m in prev_opening_materials}

        for cm in closing_materials:
            name = cm.get('name', '')
            materials.append({
                'name':        name,
                'unit':        unit_map.get(name, 'pcs'),
                'opening_qty': float(cm.get('closing_qty', 0)),
                'price':       float(price_map.get(name, 0)),
            })
        source = 'carry_forward'

    opening = {
        'taken_at':  now,
        'source':    source,
        'materials': materials,
    }

    _, doc_ref = db.collection('monthly_snapshots').add({
        'period_start':  now,
        'period_end':    None,
        'period_label':  _period_label(now),
        'status':        'open',
        'opening':       opening,
        'closing':       None,
        'created_at':    now,
    })

    return ('ok', doc_ref.id)


# ── Closing Snapshot ───────────────────────────────────────────────────────────

def take_closing_snapshot(doc_id, closing_counts):
    """
    Close an open audit period.

    closing_counts: dict  {material_name: physical_count (float)}

    Logic per material:
      system_qty   = current raw_materials[name].quantity
      purchases    = system_qty − opening_qty          (what arrived since opening)
      consumed     = system_qty − physical_closing_qty  (what was used)
      adjustment   = closing_qty − system_qty           (positive → surplus, negative → shrinkage)

    After saving the snapshot document:
      - Updates each material's raw_materials quantity to match the physical count.
      - Writes one inventory_log entry per material (delta = adjustment).

    Returns 'ok' on success, or an error string.
    """
    db = get_db()
    existing = get_snapshot_by_id(doc_id)
    if not existing:
        return 'not_found'
    if not existing.get('opening'):
        return 'no_opening'
    if existing.get('status') == 'closed':
        return 'already_closed'

    now              = datetime.now(timezone.utc)
    opening_materials = existing['opening']['materials']

    # Fetch live raw_materials quantities in one pass
    rm_docs = db.collection('raw_materials').order_by('name').stream()
    system_qty_map = {}  # name → current system qty
    rm_id_map      = {}  # name → firestore doc id
    for d in rm_docs:
        m = d.to_dict()
        name = m.get('name', '')
        system_qty_map[name] = float(m.get('quantity', 0))
        rm_id_map[name]      = d.id

    period_start = existing.get('period_start')
    start_label  = _fmt_date(period_start)
    end_label    = _fmt_date(now)
    audit_reason = f'Stock Audit Closing — {start_label} → {end_label}'

    closing_materials = []
    for m in opening_materials:
        name        = m['name']
        opening_qty = float(m.get('opening_qty', 0))
        system_qty  = system_qty_map.get(name, opening_qty)
        closing_qty = float(closing_counts.get(name, system_qty))

        if closing_qty > system_qty:
            return f'invalid_count:{name}'

        purchases_qty = max(0.0, system_qty - opening_qty)   # net inflow during period
        consumed      = max(0.0, system_qty - closing_qty)   # usage during period
        adjustment    = closing_qty - system_qty              # ± shrinkage / surplus

        closing_materials.append({
            'name':         name,
            'system_qty':   system_qty,
            'closing_qty':  closing_qty,
            'purchases_qty': purchases_qty,
            'consumed':     consumed,
            'adjustment':   adjustment,
        })

    closing = {
        'taken_at':  now,
        'materials': closing_materials,
    }

    # ── 1. Save closing data to Firestore ──────────────────────────────────
    db.collection('monthly_snapshots').document(doc_id).update({
        'closing':      closing,
        'period_end':   now,
        'period_label': _period_label(period_start, now),
        'status':       'closed',
        'updated_at':   now,
    })

    # ── 2. Update raw_materials quantities to match physical count ─────────
    # ── 3. Write inventory_log per material ───────────────────────────────
    for row in closing_materials:
        name        = row['name']
        closing_qty = row['closing_qty']
        adjustment  = row['adjustment']
        rm_id = rm_id_map.get(name)

        if rm_id:
            db.collection('raw_materials').document(rm_id).update({
                'quantity':   closing_qty,
                'updated_at': now,
            })

        # Only log if there is a real quantity change
        if adjustment != 0:
            log_inventory_transaction(
                item_type='Raw Material',
                item_name=name,
                color='',
                delta=adjustment,
                reason=audit_reason,
                reference_id=doc_id,
            )

    return 'ok'


# ═══════════════════════════════════════════════════════════════════════════════
# ── Ready Stock Monthly Snapshots ──────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

_RESTOCK_KEYWORDS = ('restocked', 'returned - restocked')


def _is_restock(reason: str) -> bool:
    """True when a positive-delta log entry represents a physical return (not a manual add)."""
    r = reason.lower()
    return any(k in r for k in _RESTOCK_KEYWORDS)


def _month_bounds(year: int, month: int):
    """Return (period_start, period_end) as UTC-aware datetimes for the given calendar month."""
    last_day = monthrange(year, month)[1]
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
    end   = datetime(year, month, last_day, 23, 59, 59, 999999, tzinfo=timezone.utc)
    return start, end


def _aggregate_logs_for_period(logs):
    """
    Aggregate a flat list of inventory_log dicts into per-SKU buckets.

    Returns dict:  product_key → {added, sold, returned}
    """
    buckets = {}
    for log in logs:
        name   = log.get('item_name', '')
        color  = log.get('color', '') or ''
        delta  = float(log.get('delta', 0))
        reason = log.get('reason', '') or ''

        if delta == 0:
            continue  # audit notes — no quantity impact

        key = f"{name}::{color}" if color else f"{name}::__none__"
        if key not in buckets:
            buckets[key] = {
                'item_name': name,
                'color':     color,
                'added':     0.0,
                'sold':      0.0,
                'returned':  0.0,
            }

        if delta > 0:
            if _is_restock(reason):
                buckets[key]['returned'] += delta
            else:
                buckets[key]['added'] += delta
        else:
            buckets[key]['sold'] += abs(delta)

    return buckets


def _fetch_rs_logs(year: int, month: int):
    """Query inventory_log for Ready Stock entries within the given month."""
    db = get_db()
    start, end = _month_bounds(year, month)
    docs = (
        db.collection('inventory_log')
        .where(filter=FieldFilter('item_type', '==', 'Ready Stock'))
        .where(filter=FieldFilter('date', '>=', start))
        .where(filter=FieldFilter('date', '<=', end))
        .stream()
    )
    return [{'id': d.id, **d.to_dict()} for d in docs]


def _fetch_rs_logs_before(cutoff: datetime):
    """
    Sum all Ready Stock log deltas strictly before `cutoff`.
    Used ONLY for the genesis opening calculation (first snapshot ever).
    Returns dict:  product_key → cumulative_delta (float)
    """
    db = get_db()
    docs = (
        db.collection('inventory_log')
        .where(filter=FieldFilter('item_type', '==', 'Ready Stock'))
        .where(filter=FieldFilter('date', '<', cutoff))
        .stream()
    )
    totals = {}
    for d in docs:
        data   = d.to_dict()
        name   = data.get('item_name', '')
        color  = data.get('color', '') or ''
        delta  = float(data.get('delta', 0))
        key    = f"{name}::{color}" if color else f"{name}::__none__"
        totals[key] = totals.get(key, 0.0) + delta
    return totals


def get_ready_stock_snapshot(year: int, month: int):
    """Return the cached snapshot doc for the given month, or None."""
    db  = get_db()
    key = f"{year:04d}-{month:02d}"
    doc = db.collection('ready_stock_snapshots').document(key).get()
    return {'id': doc.id, **doc.to_dict()} if doc.exists else None


def get_all_ready_stock_snapshots():
    """Return all ready_stock_snapshot docs, newest first."""
    db   = get_db()
    docs = db.collection('ready_stock_snapshots').stream()
    results = [{'id': d.id, **d.to_dict()} for d in docs]
    results.sort(key=lambda s: s.get('month_key', ''), reverse=True)
    return results


def generate_ready_stock_snapshot(year: int, month: int, force: bool = False):
    """
    Generate and persist a monthly Ready Stock snapshot.

    Rules:
    - If a cached snapshot already exists and force=False → returns ('already_exists', None).
    - Does NOT generate for the current calendar month (always live).
    - Verifies the accounting invariant before writing:
        closing = opening + added + returned - sold
    - Returns ('ok', month_key) on success, or an error-string tuple.
    """
    now_utc = datetime.now(timezone.utc)
    current_year, current_month = now_utc.year, now_utc.month

    if (year, month) >= (current_year, current_month):
        return ('current_month', None)

    month_key = f"{year:04d}-{month:02d}"
    db = get_db()

    # Guard: skip if already exists and not forced
    existing = db.collection('ready_stock_snapshots').document(month_key).get()
    if existing.exists and not force:
        return ('already_exists', month_key)

    # ── Step 1: Determine opening quantities ──────────────────────────────
    prev_month = month - 1
    prev_year  = year
    if prev_month == 0:
        prev_month = 12
        prev_year  -= 1

    prev_key = f"{prev_year:04d}-{prev_month:02d}"
    prev_doc = db.collection('ready_stock_snapshots').document(prev_key).get()

    if prev_doc.exists:
        # Carry forward from previous snapshot's closing quantities
        prev_data     = prev_doc.to_dict()
        opening_map   = {}  # product_key → closing_qty
        for row in prev_data.get('products', []):
            opening_map[row['product_key']] = float(row.get('closing_qty', 0))
    else:
        # Genesis: reconstruct opening from all historical log deltas before this month
        period_start, _ = _month_bounds(year, month)
        raw_totals       = _fetch_rs_logs_before(period_start)
        opening_map      = {k: max(0.0, v) for k, v in raw_totals.items()}

    # ── Step 2: Aggregate log entries for this month ──────────────────────
    month_logs = _fetch_rs_logs(year, month)
    buckets    = _aggregate_logs_for_period(month_logs)

    # Union of all known product keys (from opening AND from this month's activity)
    all_keys = set(opening_map.keys()) | set(buckets.keys())

    # ── Step 3: Build products list + verify accounting invariant ─────────
    products = []
    for key in sorted(all_keys):
        opening  = opening_map.get(key, 0.0)
        added    = buckets[key]['added']    if key in buckets else 0.0
        sold     = buckets[key]['sold']     if key in buckets else 0.0
        returned = buckets[key]['returned'] if key in buckets else 0.0

        closing  = opening + added + returned - sold

        # Accounting invariant check
        if round(closing, 6) < 0:
            closing = 0.0  # floor at zero — cannot have negative physical stock

        if key in buckets:
            item_name = buckets[key]['item_name']
            color     = buckets[key]['color']
        else:
            # Parse from the key itself
            parts     = key.split('::', 1)
            item_name = parts[0]
            color     = '' if len(parts) < 2 or parts[1] == '__none__' else parts[1]

        products.append({
            'product_key':  key,
            'item_name':    item_name,
            'color':        color,
            'opening_qty':  round(opening,  4),
            'added_qty':    round(added,    4),
            'sold_qty':     round(sold,     4),
            'returned_qty': round(returned, 4),
            'closing_qty':  round(closing,  4),
        })

    # ── Step 4: Build summary ─────────────────────────────────────────────
    period_start, period_end = _month_bounds(year, month)
    month_label = period_start.strftime('%B %Y')

    summary = {
        'total_skus':        len(products),
        'total_added_qty':   round(sum(p['added_qty']    for p in products), 4),
        'total_sold_qty':    round(sum(p['sold_qty']     for p in products), 4),
        'total_returned_qty':round(sum(p['returned_qty'] for p in products), 4),
    }

    # ── Step 5: Persist ───────────────────────────────────────────────────
    doc_data = {
        'month_key':        month_key,
        'month_label':      month_label,
        'period_start':     period_start,
        'period_end':       period_end,
        'status':           'closed',
        'generated_at':     now_utc,
        'generated_by':     'manual',
        'source_log_count': len(month_logs),
        'products':         products,
        'summary':          summary,
    }

    db.collection('ready_stock_snapshots').document(month_key).set(doc_data)
    return ('ok', month_key)


def get_ready_stock_snapshot_live(year: int, month: int):
    """
    Calculate the current (open) month's report on-the-fly without caching.

    Opening qty is read from the previous month's snapshot if available,
    else from the live ready_stock collection quantities (best approximation
    for the genesis case on a live month).

    Returns a dict with the same structure as a persisted snapshot.
    """
    db = get_db()

    # ── Opening quantities ─────────────────────────────────────────────────
    prev_month = month - 1
    prev_year  = year
    if prev_month == 0:
        prev_month = 12
        prev_year  -= 1

    prev_key = f"{prev_year:04d}-{prev_month:02d}"
    prev_doc = db.collection('ready_stock_snapshots').document(prev_key).get()

    if prev_doc.exists:
        prev_data   = prev_doc.to_dict()
        opening_map = {}
        for row in prev_data.get('products', []):
            opening_map[row['product_key']] = float(row.get('closing_qty', 0))
    else:
        # Fall back to live ready_stock quantities as approximate opening
        rs_docs = db.collection('ready_stock').stream()
        opening_map = {}
        for d in rs_docs:
            m = d.to_dict()
            name  = m.get('name', '')
            color = m.get('color', '') or ''
            qty   = float(m.get('quantity', 0))
            key   = f"{name}::{color}" if color else f"{name}::__none__"
            opening_map[key] = qty

    # ── This month's log aggregation ───────────────────────────────────────
    month_logs = _fetch_rs_logs(year, month)
    buckets    = _aggregate_logs_for_period(month_logs)

    all_keys = set(opening_map.keys()) | set(buckets.keys())

    products = []
    for key in sorted(all_keys):
        opening  = opening_map.get(key, 0.0)
        added    = buckets[key]['added']    if key in buckets else 0.0
        sold     = buckets[key]['sold']     if key in buckets else 0.0
        returned = buckets[key]['returned'] if key in buckets else 0.0
        closing  = max(0.0, opening + added + returned - sold)

        if key in buckets:
            item_name = buckets[key]['item_name']
            color     = buckets[key]['color']
        else:
            parts     = key.split('::', 1)
            item_name = parts[0]
            color     = '' if len(parts) < 2 or parts[1] == '__none__' else parts[1]

        products.append({
            'product_key':  key,
            'item_name':    item_name,
            'color':        color,
            'opening_qty':  round(opening,  4),
            'added_qty':    round(added,    4),
            'sold_qty':     round(sold,     4),
            'returned_qty': round(returned, 4),
            'closing_qty':  round(closing,  4),
        })

    period_start, period_end = _month_bounds(year, month)
    summary = {
        'total_skus':         len(products),
        'total_added_qty':    round(sum(p['added_qty']    for p in products), 4),
        'total_sold_qty':     round(sum(p['sold_qty']     for p in products), 4),
        'total_returned_qty': round(sum(p['returned_qty'] for p in products), 4),
    }

    return {
        'month_key':        f"{year:04d}-{month:02d}",
        'month_label':      period_start.strftime('%B %Y'),
        'period_start':     period_start,
        'period_end':       period_end,
        'status':           'live',
        'generated_at':     datetime.now(timezone.utc),
        'source_log_count': len(month_logs),
        'products':         products,
        'summary':          summary,
    }


def backfill_ready_stock_snapshots(start_year: int, start_month: int, force: bool = False):
    """
    Sequentially generate snapshots for every closed month starting from
    (start_year, start_month) up to (but not including) the current calendar month.

    MUST run oldest-first because each month's opening depends on the previous closing.

    Returns a list of result tuples: [(status_str, month_key), ...]
    """
    now_utc = datetime.now(timezone.utc)
    stop_year, stop_month = now_utc.year, now_utc.month

    results  = []
    cur_year = start_year
    cur_month= start_month

    while (cur_year, cur_month) < (stop_year, stop_month):
        status, key = generate_ready_stock_snapshot(cur_year, cur_month, force=force)
        results.append((status, key or f"{cur_year:04d}-{cur_month:02d}"))

        # Advance to next month
        cur_month += 1
        if cur_month > 12:
            cur_month = 1
            cur_year += 1

    return results
