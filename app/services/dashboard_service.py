from datetime import datetime, timezone
from calendar import monthrange
from google.cloud.firestore_v1 import FieldFilter
from app import get_db
from app.services.snapshot_service import (
    _month_bounds,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _prev_month(year: int, month: int):
    """Return (year, month) for the calendar month before the given one."""
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _iter_last_n_months(year: int, month: int, n: int):
    """
    Return a list of (year, month) tuples for the last `n` months ending at
    (year, month), inclusive, oldest first.
    """
    months = []
    y, m = year, month
    for _ in range(n):
        months.append((y, m))
        y, m = _prev_month(y, m)
    months.reverse()
    return months


def _fetch_rs_logs_bounded(start: datetime, end: datetime) -> list:
    """Fetch Ready Stock inventory_log entries between start and end datetimes."""
    db = get_db()
    docs = (
        db.collection('inventory_log')
        .where(filter=FieldFilter('item_type', '==', 'Ready Stock'))
        .where(filter=FieldFilter('date', '>=', start))
        .where(filter=FieldFilter('date', '<=', end))
        .stream()
    )
    return [{'id': d.id, **d.to_dict()} for d in docs]


# ── KPI Cards ─────────────────────────────────────────────────────────────────

def get_order_kpis(start: datetime, end: datetime) -> dict:
    """
    Query `orders` collection for the given date range and compute:
      - total_orders:         count of all orders
      - expected_revenue:     sum of bank_settlement for non-cancelled/non-RTO orders
      - status_counts:        {status: count} for every status present
      - return_rate:          (Returned + RTO) / dispatched_orders * 100
      - rto_rate:             RTO / dispatched_orders * 100
    """
    db = get_db()
    docs = (
        db.collection('orders')
        .where(filter=FieldFilter('date', '>=', start))
        .where(filter=FieldFilter('date', '<=', end))
        .stream()
    )

    total_orders = 0
    expected_revenue = 0.0
    status_counts = {}
    EXCLUDED_FROM_REVENUE = {'Cancelled', 'RTO'}

    for d in docs:
        data = d.to_dict()
        total_orders += 1
        status = data.get('status', 'Unknown')
        status_counts[status] = status_counts.get(status, 0) + 1
        if status not in EXCLUDED_FROM_REVENUE:
            expected_revenue += float(data.get('bank_settlement', 0) or 0)

    returned = status_counts.get('Returned', 0)
    rto = status_counts.get('RTO', 0)
    cancelled = status_counts.get('Cancelled', 0)
    dispatched_orders = total_orders - cancelled
    return_rate = round((returned + rto) / dispatched_orders * 100, 1) if dispatched_orders else 0.0
    rto_rate    = round(rto / dispatched_orders * 100, 1) if dispatched_orders else 0.0

    return {
        'total_orders':     total_orders,
        'expected_revenue': round(expected_revenue, 2),
        'status_counts':    status_counts,
        'return_rate':      return_rate,
        'rto_rate':         rto_rate,
    }


def get_cashbook_kpis(start: datetime, end: datetime) -> dict:
    """
    Query `cashbook` collection for the given date range and compute:
      - cash_received:       sum of inflow amounts
      - total_outflow:       sum of outflow amounts
      - net_cash_flow:       cash_received - total_outflow
      - outflow_chart_data:  list of {category, amount, color}, sorted by amount desc
    """
    OUTFLOW_PALETTE = [
        '#6366f1', '#10b981', '#f59e0b', '#ef4444',
        '#3b82f6', '#8b5cf6', '#06b6d4', '#f97316',
        '#ec4899', '#84cc16',
    ]

    db = get_db()
    docs = (
        db.collection('cashbook')
        .where(filter=FieldFilter('date', '>=', start))
        .where(filter=FieldFilter('date', '<=', end))
        .stream()
    )

    cash_received = 0.0
    total_outflow = 0.0
    raw_by_category = {}

    for d in docs:
        data = d.to_dict()
        amount = float(data.get('amount', 0) or 0)
        entry_type = data.get('type', '')
        if entry_type == 'inflow':
            cash_received += amount
        elif entry_type == 'outflow':
            total_outflow += amount
            cat = data.get('category', 'Uncategorised') or 'Uncategorised'
            raw_by_category[cat] = raw_by_category.get(cat, 0.0) + amount

    # Sort by amount descending — largest slice always gets first palette colour.
    sorted_cats = sorted(raw_by_category.items(), key=lambda x: x[1], reverse=True)
    outflow_chart_data = [
        {
            'category': cat,
            'amount':   round(amt, 2),
            'color':    OUTFLOW_PALETTE[i % len(OUTFLOW_PALETTE)],
        }
        for i, (cat, amt) in enumerate(sorted_cats)
    ]

    return {
        'cash_received':      round(cash_received, 2),
        'total_outflow':      round(total_outflow, 2),
        'net_cash_flow':      round(cash_received - total_outflow, 2),
        'outflow_chart_data': outflow_chart_data,
    }


# ── Revenue Trend (last 6 months — always month-anchored) ─────────────────────

def get_revenue_trend(year: int, month: int, n_months: int = 6) -> list:
    """
    Return a list of dicts for the last `n_months` ending at (year, month), inclusive.
    Each dict: {month_label, month_key, expected_revenue}

    Always anchored to the selected/current calendar month regardless of any
    custom date range — the custom range only affects bar highlighting in the
    template, not the data fetched here.
    """
    db = get_db()
    month_list = _iter_last_n_months(year, month, n_months)

    oldest_year, oldest_month = month_list[0]
    window_start, _ = _month_bounds(oldest_year, oldest_month)
    _, window_end   = _month_bounds(year, month)

    docs = (
        db.collection('orders')
        .where(filter=FieldFilter('date', '>=', window_start))
        .where(filter=FieldFilter('date', '<=', window_end))
        .stream()
    )

    EXCLUDED_FROM_REVENUE = {'Cancelled', 'RTO'}

    revenue_map = {}   # 'YYYY-MM' → float
    for d in docs:
        data = d.to_dict()
        if data.get('status', '') in EXCLUDED_FROM_REVENUE:
            continue
        order_date = data.get('date')
        if order_date is None:
            continue
        try:
            mk = order_date.strftime('%Y-%m')
        except AttributeError:
            continue
        revenue_map[mk] = revenue_map.get(mk, 0.0) + float(data.get('bank_settlement', 0) or 0)

    trend = []
    for y, m in month_list:
        mk = f'{y:04d}-{m:02d}'
        dt = datetime(y, m, 1)
        trend.append({
            'month_key':        mk,
            'month_label':      dt.strftime('%b %Y'),
            'expected_revenue': round(revenue_map.get(mk, 0.0), 2),
        })

    return trend


# ── Inventory Panel ───────────────────────────────────────────────────────────

def get_inventory_snapshot(start: datetime, end: datetime) -> dict:
    """
    Returns:
      - low_stock_items:    list of ready_stock items where qty <= min_stock (always live)
      - stock_valuation:    {ready_stock_value} (always live)
      - top_sold_products:  top 5 SKUs by sold_qty from inventory_log for exact date range
    """
    db = get_db()

    # ── 1. Low stock alerts + valuation (always live — not date-filtered) ──
    rs_docs = list(db.collection('ready_stock').stream())
    rs_items = [{'id': d.id, **d.to_dict()} for d in rs_docs]

    ready_stock_value = 0.0
    low_stock_items = []

    parents = {}
    variants_by_parent = {}
    for item in rs_items:
        if item.get('parent_id'):
            pid = item['parent_id']
            variants_by_parent.setdefault(pid, []).append(item)
        else:
            parents[item['id']] = item

    for item_id, item in parents.items():
        # Use the authoritative flag; fall back to checking children for
        # documents created before the has_variants migration.
        is_group = item.get('has_variants', False) or bool(variants_by_parent.get(item_id))
        if is_group:
            children = variants_by_parent.get(item_id, [])
            for child in children:
                qty  = float(child.get('quantity', 0))
                cost = float(child.get('cost_price', 0) or item.get('cost_price', 0))
                ready_stock_value += qty * cost
            for child in children:
                min_s = int(child.get('min_stock', 0) or 0)
                qty   = float(child.get('quantity', 0))
                if min_s > 0 and qty <= min_s:
                    low_stock_items.append({
                        'name':      item.get('name', ''),
                        'color':     child.get('color', ''),
                        'quantity':  qty,
                        'min_stock': min_s,
                    })
        else:
            qty  = float(item.get('quantity', 0))
            cost = float(item.get('cost_price', 0))
            ready_stock_value += qty * cost
            min_s = int(item.get('min_stock', 0) or 0)
            if min_s > 0 and qty <= min_s:
                low_stock_items.append({
                    'name':      item.get('name', ''),
                    'color':     item.get('color', ''),
                    'quantity':  qty,
                    'min_stock': min_s,
                })

    # ── 2. Top 5 sold products for the exact date range ───────────────────
    logs = _fetch_rs_logs_bounded(start, end)

    # Manually aggregate so we can normalise keys to lowercase and only
    # count OUT movements (negative delta). _aggregate_logs_for_period is
    # shared with snapshots and must not be changed, so we roll our own here.
    sold_buckets = {}   # lowercase_key → {display_label, sold_qty}
    for log in logs:
        delta = float(log.get('delta', 0))
        if delta >= 0:
            continue   # skip IN / restock entries entirely

        name  = log.get('item_name', '') or ''
        color = (log.get('color', '') or '').strip()

        # Normalise to lowercase for deduplication
        norm_key = f"{name.lower()}::{color.lower()}"

        if norm_key not in sold_buckets:
            # Store the original-casing display label on first encounter
            label = f"{name} ({color})" if color else name
            sold_buckets[norm_key] = {'label': label, 'sold_qty': 0.0}

        sold_buckets[norm_key]['sold_qty'] += abs(delta)

    # Sort by sold_qty descending; exclude any bucket that ended up at 0
    top_sold = sorted(
        [b for b in sold_buckets.values() if b['sold_qty'] > 0],
        key=lambda b: b['sold_qty'],
        reverse=True,
    )[:5]

    # Round final quantities
    for b in top_sold:
        b['sold_qty'] = round(b['sold_qty'], 2)

    return {
        'low_stock_items': low_stock_items,
        'stock_valuation': {
            'ready_stock_value': round(ready_stock_value, 2),
        },
        'top_sold_products': top_sold,
    }


# ── Open Purchase Orders ───────────────────────────────────────────────────────

def get_open_purchase_orders() -> list:
    """
    Return all purchase_orders where payment_status != 'paid' and
    status not in terminal states (Cancelled, Returned).
    """
    db = get_db()
    TERMINAL_PO = {'Cancelled', 'Returned'}

    docs = db.collection('purchase_orders').stream()
    open_pos = []
    for d in docs:
        data = d.to_dict()
        if data.get('status', '') in TERMINAL_PO:
            continue
        if data.get('payment_status', '') == 'paid':
            continue
        open_pos.append({
            'id':             d.id,
            'po_number':      data.get('po_number', '—'),
            'vendor_name':    data.get('vendor_name', '—'),
            'total_cost':     round(float(data.get('total_cost', 0) or 0), 2),
            'amount_paid':    round(float(data.get('amount_paid', 0) or 0), 2),
            'balance_due':    round(float(data.get('balance_due', 0) or 0), 2),
            'payment_status': data.get('payment_status', 'unpaid'),
            'status':         data.get('status', '—'),
        })

    open_pos.sort(key=lambda p: p['balance_due'], reverse=True)
    return open_pos


# ── Master aggregator ──────────────────────────────────────────────────────────

def get_dashboard_data(year: int, month: int,
                       custom_start: datetime = None,
                       custom_end: datetime = None) -> dict:
    """
    Single entry point for the dashboard route.

    If custom_start/custom_end are provided, all date-filtered sections
    (KPIs, cashbook, top sold) use the exact custom date range.
    The Revenue Trend chart always shows the last 6 months anchored to
    (year, month) — bars within the custom range are highlighted via
    'highlighted_months' passed to the template.
    """
    if custom_start and custom_end:
        start     = custom_start
        end       = custom_end
        is_custom = True
    else:
        start, end = _month_bounds(year, month)
        is_custom  = False

    order_kpis    = get_order_kpis(start, end)
    cashbook_kpis = get_cashbook_kpis(start, end)
    revenue_trend = get_revenue_trend(year, month, n_months=6)
    inventory     = get_inventory_snapshot(start, end)
    open_pos      = get_open_purchase_orders()

    # Build highlighted_months: all 'YYYY-MM' keys touched by [start, end]
    highlighted_months = set()
    if is_custom:
        cur = datetime(start.year, start.month, 1, tzinfo=timezone.utc)
        while cur <= end:
            highlighted_months.add(cur.strftime('%Y-%m'))
            if cur.month == 12:
                cur = datetime(cur.year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                cur = datetime(cur.year, cur.month + 1, 1, tzinfo=timezone.utc)
    else:
        highlighted_months.add(f'{year:04d}-{month:02d}')

    # Human-readable label for the page header
    if is_custom:
        display_label = f"{start.strftime('%d %b %Y')} → {end.strftime('%d %b %Y')}"
    else:
        display_label = datetime(year, month, 1).strftime('%B %Y')

    month_key = f'{year:04d}-{month:02d}'

    return {
        # Period context
        'month_label':          display_label,
        'month_key':            month_key,
        'selected_year':        year,
        'selected_month':       month,
        'is_custom_range':      is_custom,
        'highlighted_months':   list(highlighted_months),

        # KPI Cards
        'total_orders':         order_kpis['total_orders'],
        'expected_revenue':     order_kpis['expected_revenue'],
        'cash_received':        cashbook_kpis['cash_received'],
        'net_cash_flow':        cashbook_kpis['net_cash_flow'],

        # Order status breakdown
        'status_counts':        order_kpis['status_counts'],
        'return_rate':          order_kpis['return_rate'],
        'rto_rate':             order_kpis['rto_rate'],

        # Revenue trend
        'revenue_trend':        revenue_trend,

        # Inventory panel
        'low_stock_items':      inventory['low_stock_items'],
        'stock_valuation':      inventory['stock_valuation'],
        'top_sold_products':    inventory['top_sold_products'],

        # Money detail
        'outflow_chart_data':   cashbook_kpis['outflow_chart_data'],
        'total_outflow':        cashbook_kpis['total_outflow'],
        'open_purchase_orders': open_pos,
    }
