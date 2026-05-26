from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from app import get_db
from app.services.inventory_service import (
    get_all_raw_materials, add_raw_material, update_raw_material, delete_raw_material,
    get_all_ready_stock, get_ready_stock_grouped, add_ready_stock, add_ready_stock_variant,
    update_ready_stock, delete_ready_stock, adjust_ready_stock_qty,
    get_inventory_logs, get_product_inventory_logs
)

inventory_bp = Blueprint('inventory', __name__, url_prefix='/inventory')


from datetime import datetime, timezone

@inventory_bp.route('/')
def inventory_list():
    raw = get_all_raw_materials()
    ready = get_all_ready_stock()
    item_name = request.args.get('item_name')
    color = request.args.get('color')
    cursor_id = request.args.get('cursor_id')
    direction = request.args.get('direction', 'next')
    
    logs, has_prev, has_next = get_inventory_logs(
        item_name=item_name or None,
        color=color or None,
        cursor_id=cursor_id,
        direction=direction,
        limit=20
    )
    
    # Calculate Quick Analytics
    today = datetime.now(timezone.utc).date()
    today_in = 0
    today_out = 0
    today_log_count = 0
    today_shipped = 0
    
    for log in logs:
        # Some datetime objects might have timezone attached, safe way:
        dt = log.get('date')
        if dt and hasattr(dt, 'date') and dt.date() == today:
            today_log_count += 1
            delta = log.get('delta', 0)
            reason = log.get('reason', '')
            
            if delta > 0:
                today_in += delta
            elif delta < 0:
                today_out += abs(delta)
                if 'Shipped' in reason or 'Delivered' in reason:
                    today_shipped += abs(delta)

    tab = request.args.get('tab', 'ready')
    return render_template('inventory.html', 
                           raw_materials=raw, ready_stock=get_ready_stock_grouped(), logs=logs, active_tab=tab,
                           today_log_count=today_log_count, today_shipped=today_shipped,
                           filter_item_name=item_name or '', filter_color=color or '',
                           has_prev_log=has_prev, has_next_log=has_next)


# ── Raw Materials ──────────────────────────────────────────────

@inventory_bp.route('/raw/add', methods=['POST'])
def raw_add():
    name = request.form.get('name', '').strip()
    quantity = request.form.get('quantity', 0)
    unit = request.form.get('unit', 'pcs').strip()
    if name:
        add_raw_material(name, quantity, unit)
        flash('Raw material added.', 'success')
    else:
        flash('Name is required.', 'error')
    return redirect(url_for('inventory.inventory_list', tab='raw'))


@inventory_bp.route('/raw/edit/<doc_id>', methods=['POST'])
def raw_edit(doc_id):
    data = {}
    if request.form.get('unit'):
        data['unit'] = request.form['unit'].strip()
    if request.form.get('name'):
        data['name'] = request.form['name'].strip()
    if data:
        update_raw_material(doc_id, data)
        flash('Raw material unit updated.', 'success')
    return redirect(url_for('inventory.inventory_list', tab='raw'))


@inventory_bp.route('/raw/delete/<doc_id>', methods=['POST'])
def raw_delete(doc_id):
    delete_raw_material(doc_id)
    flash('Raw material deleted.', 'success')
    return redirect(url_for('inventory.inventory_list', tab='raw'))


# ── Ready Stock ────────────────────────────────────────────────

@inventory_bp.route('/ready/add', methods=['POST'])
def ready_add():
    name        = request.form.get('name', '').strip()
    color       = request.form.get('color', '').strip()
    quantity    = request.form.get('quantity', 0)
    cost_price  = request.form.get('cost_price', 0)
    min_stock   = request.form.get('min_stock', 0)
    reason      = request.form.get('reason', 'Manual Add').strip() or 'Manual Add'
    has_variants = request.form.get('has_variants') == '1'
    if name:
        add_ready_stock(name, color, quantity, cost_price, reason=reason,
                        min_stock=min_stock, has_variants=has_variants)
        flash('Ready stock item added.', 'success')
    else:
        flash('Product name is required.', 'error')
    return redirect(url_for('inventory.inventory_list', tab='ready'))


@inventory_bp.route('/ready/edit/<doc_id>', methods=['POST'])
def ready_edit(doc_id):
    data = {}
    # Name and Quantity are LOCKED — use Adjust Stock to change quantity
    if request.form.get('cost_price') is not None:
        data['cost_price'] = float(request.form.get('cost_price') or 0)
    if request.form.get('min_stock') is not None:
        data['min_stock'] = int(float(request.form.get('min_stock') or 0))
    if data:
        try:
            update_ready_stock(doc_id, data)
            flash('Product updated.', 'success')
        except ValueError as e:
            flash(str(e), 'error')
    return redirect(url_for('inventory.inventory_list', tab='ready'))


@inventory_bp.route('/ready/adjust/<doc_id>', methods=['POST'])
def ready_adjust(doc_id):
    adjustment = request.form.get('adjustment', '').strip()
    reason = request.form.get('reason', '').strip()
    notes = request.form.get('notes', '').strip()

    if not adjustment or not reason:
        flash('Adjustment quantity and reason are required.', 'error')
        return redirect(url_for('inventory.inventory_list', tab='ready'))

    try:
        delta = int(float(adjustment))
    except ValueError:
        flash('Invalid adjustment quantity.', 'error')
        return redirect(url_for('inventory.inventory_list', tab='ready'))

    if delta < 1:
        flash('Only positive additions are allowed. Stock is reduced through orders.', 'error')
        return redirect(url_for('inventory.inventory_list', tab='ready'))

    from app.services.inventory_service import get_all_ready_stock
    all_docs = get_all_ready_stock()
    item = next((d for d in all_docs if d['id'] == doc_id), None)
    if not item:
        flash('Item not found.', 'error')
        return redirect(url_for('inventory.inventory_list', tab='ready'))

    full_reason = f"{reason}: {notes}" if notes else reason
    adjust_ready_stock_qty(
        item.get('name', ''),
        item.get('color', ''),
        delta,
        0,
        reason=full_reason,
        ref_id=doc_id
    )
    direction = f"+{delta}" if delta > 0 else str(delta)
    flash(f'Stock adjusted by {direction} for {item.get("name")}. Reason: {reason}.', 'success')
    return redirect(url_for('inventory.inventory_list', tab='ready'))


@inventory_bp.route('/ready/delete/<doc_id>', methods=['POST'])
def ready_delete(doc_id):
    # Deletions disabled — inventory records are permanent
    flash('Inventory records cannot be deleted. Set quantity to 0 to zero it out.', 'error')
    return redirect(url_for('inventory.inventory_list', tab='ready'))


@inventory_bp.route('/ready/add_variant/<parent_id>', methods=['POST'])
def ready_add_variant(parent_id):
    from app.services.inventory_service import get_all_ready_stock
    db_docs = get_all_ready_stock()
    parent = next((d for d in db_docs if d['id'] == parent_id), None)
    if not parent:
        flash('Parent product not found.', 'error')
        return redirect(url_for('inventory.inventory_list', tab='ready'))
    
    variant_name = request.form.get('variant_name', '').strip()
    quantity = request.form.get('quantity', 0)
    min_stock = request.form.get('min_stock', 0)

    if not variant_name:
        flash('Variant name is required.', 'error')
        return redirect(url_for('inventory.inventory_list', tab='ready'))

    add_ready_stock_variant(parent_id, parent['name'], variant_name, quantity, min_stock=min_stock)
    flash(f'Variant "{variant_name}" added to {parent["name"]}.', 'success')
    return redirect(url_for('inventory.inventory_list', tab='ready'))





# ── API endpoints for JS ───────────────────────────────────────

@inventory_bp.route('/api/raw', methods=['GET'])
def api_raw_list():
    return jsonify(get_all_raw_materials())


@inventory_bp.route('/api/ready', methods=['GET'])
def api_ready_list():
    return jsonify(get_all_ready_stock())


@inventory_bp.route('/api/variants', methods=['GET'])
def api_variants():
    """Return variants (children) for a given parent product name."""
    name = request.args.get('name', '')
    if not name:
        return jsonify([])
    all_docs = get_all_ready_stock()
    # Find parent doc
    parent = next((d for d in all_docs if d.get('name') == name and not d.get('parent_id')), None)
    if not parent:
        return jsonify([])
    # Find children
    children = [d for d in all_docs if d.get('parent_id') == parent['id']]
    return jsonify([{'id': c['id'], 'color': c.get('color', ''), 'quantity': c.get('quantity', 0)} for c in children])


@inventory_bp.route('/api/product-logs', methods=['GET'])
def api_product_logs():
    name = request.args.get('name')
    color = request.args.get('color')
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    
    logs = get_product_inventory_logs(name, color)
    # Serialize datetime for JSON
    for log in logs:
        if log.get('date'):
            log['date'] = log['date'].isoformat()
    return jsonify(logs)


@inventory_bp.route('/api/raw-stock-ledger', methods=['GET'])
def api_raw_stock_ledger():
    """
    Return a combined chronological stock ledger for a raw material.

    Inflows: purchase_orders where this material appears and status is Received or Paid.
    Outflows + Adjustments: inventory_log entries for item_name == name (any delta != 0).

    Response is sorted newest-first, with a running_balance column computed
    from oldest to newest so the client can display it in either order.

    Each entry shape:
      { iso_date, date_str, type, reference, delta, running_balance }
    """
    name = request.args.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    from app import get_db
    from app.services.inventory_service import get_product_inventory_logs

    events = []  # list of {iso_date, date_str, type, reference, delta}

    # ── 1. PO inflows (Received / Paid only) ──────────────────────────────
    db = get_db()
    all_pos_docs = db.collection('purchase_orders').stream()
    all_pos = [{'id': d.id, **d.to_dict()} for d in all_pos_docs]

    for po in all_pos:
        status = po.get('status', '')
        if status not in ('Received', 'Paid'):
            continue

        matched_qty = 0.0

        items = po.get('items', [])
        if items:
            for it in items:
                if (it.get('item') or '').strip().lower() == name.lower():
                    matched_qty += float(it.get('quantity', 0))
        else:
            if (po.get('item') or '').strip().lower() == name.lower():
                matched_qty = float(po.get('quantity', 0))

        if matched_qty == 0:
            continue

        # Use updated_at if available (when it was actually received), else created_at
        dt = po.get('updated_at') or po.get('created_at')
        iso = dt.isoformat() if dt and hasattr(dt, 'isoformat') else ''
        date_str = dt.strftime('%d/%m/%Y') if dt and hasattr(dt, 'strftime') else '-'
        vendor = po.get('vendor_name') or '-'
        po_num = po.get('po_number') or '-'

        events.append({
            'iso_date':  iso,
            'date_str':  date_str,
            'type':      'PO Received',
            'reference': f'{po_num} — {vendor}',
            'delta':     matched_qty,
        })

    # ── 2. inventory_log entries (all deltas != 0) ─────────────────────────
    logs = get_product_inventory_logs(name, color=None, limit=500)
    for log in logs:
        delta = float(log.get('delta', 0))
        if delta == 0:
            continue  # informational notes — skip from ledger

        dt = log.get('date')
        iso = dt.isoformat() if dt and hasattr(dt, 'isoformat') else ''
        date_str = dt.strftime('%d/%m/%Y') if dt and hasattr(dt, 'strftime') else '-'
        reason = log.get('reason') or '-'
        ref_id = log.get('reference_id') or ''

        # Classify the type from the reason string
        r_lower = reason.lower()
        if 'audit' in r_lower:
            ev_type = 'Audit Adjustment'
        elif 'returned' in r_lower or 'cancelled' in r_lower or 'reversal' in r_lower:
            ev_type = 'PO Return'
        elif 'production' in r_lower or 'consumed' in r_lower or 'manufactured' in r_lower:
            ev_type = 'Production'
        elif 'purchase' in r_lower or 'received' in r_lower:
            ev_type = 'PO Received'
        elif 'manual add' in r_lower or 'manual adjustment' in r_lower:
            ev_type = 'Manual'
        else:
            ev_type = 'Inflow' if delta > 0 else 'Outflow'

        events.append({
            'iso_date':  iso,
            'date_str':  date_str,
            'type':      ev_type,
            'reference': reason,
            'delta':     delta,
        })

    # ── 3. Sort oldest → newest, then reverse for newest-first display ────
    events.sort(key=lambda e: e['iso_date'])
    events.reverse()
    return jsonify(events)


@inventory_bp.route('/api/raw-material-price-history/<material_id>', methods=['GET'])
def api_raw_material_price_history(material_id):
    """
    Return the price_history array from a raw_material document.
    Dates are serialised to ISO-8601 strings for JSON transport.
    """
    db = get_db()
    doc = db.collection('raw_materials').document(material_id).get()
    if not doc.exists:
        return jsonify({'error': 'Material not found'}), 404

    history = doc.to_dict().get('price_history', [])

    # Serialise any datetime objects so JSON encoding never fails
    serialised = []
    for entry in history:
        e = dict(entry)
        if hasattr(e.get('date'), 'isoformat'):
            e['date'] = e['date'].isoformat()
        serialised.append(e)

    # Sort newest-first
    serialised.sort(key=lambda x: x.get('date', ''), reverse=True)
    return jsonify(serialised)
