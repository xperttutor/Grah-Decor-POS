from datetime import datetime, timezone
from calendar import monthrange
from google.cloud.firestore_v1 import FieldFilter
from app import get_db
from app.services.snapshot_service import (
    _month_bounds,
    _fetch_rs_logs,
    _aggregate_logs_for_period,
    get_ready_stock_snapshot,
    get_ready_stock_snapshot_live,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _prev_month(year: int, month: int):
    """Return (year, month) for the calendar month before the given one."""
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _iter_last_n_months(year: int, month: int, n: int):
    """
    Yield (year, month) tuples for the last `n` months ending at (year, month),
    inclusive, oldest first.
    """
    months = []
    y, m = year, month
    for _ in range(n):
        months.append((y, m))
        y, m = _prev_month(y, m)
    months.reverse()
    return months


# ── KPI Cards ─────────────────────────────────────────────────────────────────

def get_order_kpis(year: int, month: int) -> dict:
    """
    Query `orders` collection for the given month and compute:
      - total_orders:         count of all orders
      - expected_revenue:     sum of bank_settlement for non-cancelled/non-RTO orders
      - status_counts:        {status: count} for every status present
      - return_rate:          (Returned + RTO) / total_orders * 100
      - rto_rate:             RTO / total_orders * 100
    """
    db = get_db()
    # orders.date is stored as a Firestore Timestamp (UTC datetime), not a string.
    # Use datetime objects for both bounds — identical pattern to cashbook queries.
    start, end = _month_bounds(year, month)

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
    return_rate = round((returned + rto) / total_orders * 100, 1) if total_orders else 0.0
    rto_rate = round(rto / total_orders * 100, 1) if total_orders else 0.0

    return {
        'total_orders': total_orders,
        'expected_revenue': round(expected_revenue, 2),
        'status_counts': status_counts,
        'return_rate': return_rate,
        'rto_rate': rto_rate,
    }


def get_cashbook_kpis(year: int, month: int) -> dict:
    """
    Query `cashbook` collection for the given month and compute:
      - cash_received:        sum of inflow amounts
      - total_outflow:        sum of outflow amounts
      - net_cash_flow:        cash_received - total_outflow
      - outflow_by_category:  {category: amount} for all outflows
    """
    db = get_db()
    start, end = _month_bounds(year, month)

    docs = (
        db.collection('cashbook')
        .where(filter=FieldFilter('date', '>=', start))
        .where(filter=FieldFilter('date', '<=', end))
        .stream()
    )

    cash_received = 0.0
    total_outflow = 0.0
    outflow_by_category = {}

    for d in docs:
        data = d.to_dict()
        amount = float(data.get('amount', 0) or 0)
        entry_type = data.get('type', '')
        if entry_type == 'inflow':
            cash_received += amount
        elif entry_type == 'outflow':
            total_outflow += amount
            cat = data.get('category', 'Uncategorised') or 'Uncategorised'
            outflow_by_category[cat] = outflow_by_category.get(cat, 0.0) + amount

    # Round category values
    outflow_by_category = {k: round(v, 2) for k, v in outflow_by_category.items()}

    return {
        'cash_received': round(cash_received, 2),
        'total_outflow': round(total_outflow, 2),
        'net_cash_flow': round(cash_received - total_outflow, 2),
        'outflow_by_category': outflow_by_category,
    }


# ── Revenue Trend (last 6 months) ─────────────────────────────────────────────

def get_revenue_trend(year: int, month: int, n_months: int = 6) -> list:
    """
    Return a list of dicts for the last `n_months` ending at (year, month), inclusive.
    Each dict:  {month_label, month_key, expected_revenue}

    Strategy: fetch ALL orders across the full 6-month window in a single Firestore
    query (bounded by start of oldest month → end of current month), then group
    by order date in Python. This avoids 6 separate round-trips to Firestore.
    """
    db = get_db()
    month_list = _iter_last_n_months(year, month, n_months)  # [(y,m), ...]

    oldest_year, oldest_month = month_list[0]
    window_start, _ = _month_bounds(oldest_year, oldest_month)
    _, window_end = _month_bounds(year, month)

    docs = (
        db.collection('orders')
        .where(filter=FieldFilter('date', '>=', window_start))
        .where(filter=FieldFilter('date', '<=', window_end))
        .stream()
    )

    EXCLUDED_FROM_REVENUE = {'Cancelled', 'RTO'}

    # Accumulate revenue per month_key
    revenue_map = {}   # 'YYYY-MM' → float
    for d in docs:
        data = d.to_dict()
        status = data.get('status', '')
        if status in EXCLUDED_FROM_REVENUE:
            continue
        order_date = data.get('date')   # stored as Firestore Timestamp / datetime
        if order_date is None:
            continue
        # Firestore Timestamps have strftime via datetime interface
        try:
            mk = order_date.strftime('%Y-%m')
        except AttributeError:
            continue
        revenue_map[mk] = revenue_map.get(mk, 0.0) + float(data.get('bank_settlement', 0) or 0)

    # Build ordered result list matching month_list
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

def get_inventory_snapshot(year: int, month: int) -> dict:
    """
    Returns:
      - low_stock_items:    list of ready_stock docs where quantity <= min_stock
                            (only items with min_stock > 0)
      - stock_valuation:    {ready_stock_value, raw_material_value, total_value}
      - top_sold_products:  top 5 SKUs by sold_qty from inventory_log this month
    """
    db = get_db()

    # ── 1. Low stock alerts + valuation (ready stock) ─────────────────────
    rs_docs = list(db.collection('ready_stock').stream())
    rs_items = [{'id': d.id, **d.to_dict()} for d in rs_docs]

    ready_stock_value = 0.0
    low_stock_items = []

    # Build a parent→children map to avoid double-counting parent aggregates
    parents = {}
    variants_by_parent = {}
    for item in rs_items:
        if item.get('parent_id'):
            pid = item['parent_id']
            variants_by_parent.setdefault(pid, []).append(item)
        else:
            parents[item['id']] = item

    for item_id, item in parents.items():
        children = variants_by_parent.get(item_id, [])
        if children:
            # Parent with variants: value = sum of children (each child has its own cost_price or inherits)
            for child in children:
                qty = float(child.get('quantity', 0))
                cost = float(child.get('cost_price', 0) or item.get('cost_price', 0))
                ready_stock_value += qty * cost
            # Low stock: check each variant independently
            for child in children:
                min_s = int(child.get('min_stock', 0) or 0)
                qty = float(child.get('quantity', 0))
                if min_s > 0 and qty <= min_s:
                    low_stock_items.append({
                        'name':      item.get('name', ''),
                        'color':     child.get('color', ''),
                        'quantity':  qty,
                        'min_stock': min_s,
                    })
        else:
            # Simple item (no children)
            qty = float(item.get('quantity', 0))
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

    # ── 2. Raw material valuation ─────────────────────────────────────────
    rm_docs = db.collection('raw_materials').stream()
    raw_material_value = sum(
        float(d.to_dict().get('quantity', 0)) * float(d.to_dict().get('price', 0))
        for d in rm_docs
    )

    # ── 3. Top 5 sold products this month (via inventory_log) ─────────────
    month_logs = _fetch_rs_logs(year, month)
    buckets = _aggregate_logs_for_period(month_logs)

    # Sort by sold_qty descending; take top 5
    sold_sorted = sorted(buckets.values(), key=lambda b: b['sold'], reverse=True)
    top_sold = []
    for b in sold_sorted[:5]:
        label = b['item_name']
        if b.get('color'):
            label = f"{b['item_name']} ({b['color']})"
        top_sold.append({
            'label':    label,
            'sold_qty': round(b['sold'], 2),
        })

    return {
        'low_stock_items': low_stock_items,
        'stock_valuation': {
            'ready_stock_value':   round(ready_stock_value, 2),
            'raw_material_value':  round(raw_material_value, 2),
            'total_value':         round(ready_stock_value + raw_material_value, 2),
        },
        'top_sold_products': top_sold,
    }


# ── Open Purchase Orders ───────────────────────────────────────────────────────

def get_open_purchase_orders() -> list:
    """
    Return all purchase_orders where payment_status != 'paid' and
    status not in terminal states (Cancelled, Returned).
    Each dict:  {po_number, vendor_name, total_cost, amount_paid, balance_due, payment_status}
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

    # Sort by balance_due descending so largest debts are shown first
    open_pos.sort(key=lambda p: p['balance_due'], reverse=True)
    return open_pos


# ── Master aggregator ──────────────────────────────────────────────────────────

def get_dashboard_data(year: int, month: int) -> dict:
    """
    Single entry point for the dashboard route.
    Calls each sub-aggregator and merges results into one context dict.
    """
    order_kpis     = get_order_kpis(year, month)
    cashbook_kpis  = get_cashbook_kpis(year, month)
    revenue_trend  = get_revenue_trend(year, month, n_months=6)
    inventory      = get_inventory_snapshot(year, month)
    open_pos       = get_open_purchase_orders()

    # Build month metadata for the template
    month_dt    = datetime(year, month, 1)
    month_label = month_dt.strftime('%B %Y')
    month_key   = f'{year:04d}-{month:02d}'

    return {
        # Month context
        'month_label':          month_label,
        'month_key':            month_key,
        'selected_year':        year,
        'selected_month':       month,

        # KPI Cards
        'total_orders':         order_kpis['total_orders'],
        'expected_revenue':     order_kpis['expected_revenue'],
        'cash_received':        cashbook_kpis['cash_received'],
        'net_cash_flow':        cashbook_kpis['net_cash_flow'],

        # Order status breakdown (for chart + rates)
        'status_counts':        order_kpis['status_counts'],
        'return_rate':          order_kpis['return_rate'],
        'rto_rate':             order_kpis['rto_rate'],

        # Revenue trend (for bar chart)
        'revenue_trend':        revenue_trend,

        # Inventory panel
        'low_stock_items':      inventory['low_stock_items'],
        'stock_valuation':      inventory['stock_valuation'],
        'top_sold_products':    inventory['top_sold_products'],

        # Money detail
        'outflow_by_category':  cashbook_kpis['outflow_by_category'],
        'total_outflow':        cashbook_kpis['total_outflow'],
        'open_purchase_orders': open_pos,
    }
