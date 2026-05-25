from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from datetime import datetime
from app.services.order_service import (
    get_all_orders, add_order, update_order, delete_order,
    PLATFORMS, STATUSES, REVIEWS, TERMINAL_STATUSES
)
from app.services.inventory_service import get_all_ready_stock
from app.services.contact_service import get_all_customers, add_customer, update_customer_metadata

orders_bp = Blueprint('orders', __name__, url_prefix='/orders')

def parse_order_items(form):
    products = form.getlist('product[]')
    colors = form.getlist('color[]')
    quantities = form.getlist('quantity[]')
    prices = form.getlist('price[]')
    
    order_items = []
    for i in range(len(products)):
        if products[i].strip() and products[i].strip() != '__other__':
            try:
                qty   = float(quantities[i]) if i < len(quantities) and quantities[i] else 1.0
                price = float(prices[i])     if i < len(prices)     and prices[i]     else 0.0
            except (ValueError, TypeError):
                # Skip malformed items; order_add will reject if result is empty
                continue
            color = colors[i].strip() if i < len(colors) else ''
            if color == '__other__':
                color = ''
            order_items.append({
                'product':  products[i].strip(),
                'color':    color,
                'quantity': qty,
                'price':    price,
            })
    return order_items

@orders_bp.route('/')
def orders_list():
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    platform = request.args.get('platform', '')
    status = request.args.get('status', '')
    review_status = request.args.get('review_status', '')

    df = None
    dt = None
    if date_from:
        try:
            df = datetime.strptime(date_from, '%Y-%m-%d').date()
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.strptime(date_to, '%Y-%m-%d').date()
        except ValueError:
            pass

    cursor_id = request.args.get('cursor_id')
    direction = request.args.get('direction', 'next')

    orders, has_prev, has_next = get_all_orders(
        date_from=df, date_to=dt,
        platform=platform or None,
        status=status or None,
        review_status=review_status or None,
        cursor_id=cursor_id,
        direction=direction,
        limit=20
    )

    total_sales = sum(o.get('selling_price', 0) for o in orders)
    total_settlement = sum(o.get('bank_settlement', 0) for o in orders)

    ready_stock = get_all_ready_stock()
    products = sorted(list(set(s['name'] for s in ready_stock if s.get('name'))))
    colors = sorted(list(set(s['color'] for s in ready_stock if s.get('color'))))
    customers, _, _ = get_all_customers()

    return render_template('orders.html',
                           orders=orders,
                           platforms=PLATFORMS,
                           statuses=STATUSES,
                           reviews=REVIEWS,
                           products=products,
                           colors=colors,
                           customers=customers,
                           total_sales=total_sales,
                           total_settlement=total_settlement,
                           filter_date_from=date_from or '',
                           filter_date_to=date_to or '',
                           filter_platform=platform,
                           filter_status=status,
                           filter_review=review_status,
                           has_prev=has_prev,
                           has_next=has_next)


@orders_bp.route('/add', methods=['POST'])
def order_add():
    order_items = parse_order_items(request.form)
    
    order_id = request.form.get('order_id', '').strip()
    platform = request.form.get('platform', '')
    
    # Handle Customer Logic
    customer_mode = request.form.get('customer_mode', 'new')
    customer_name = ''
    customer_id = ''
    phone_number = request.form.get('number', '').strip()
    
    if customer_mode == 'unknown':
        customer_name = 'Unknown'
        phone_number = ''
        customer_id = add_customer(customer_name, [], platform_used=platform, recent_order_id=order_id)
    elif customer_mode == 'existing':
        cust_val = request.form.get('existing_customer', '')
        if cust_val:
            parts = cust_val.split(' - ', 1)
            customer_id = parts[0]
            customer_name = parts[1] if len(parts) > 1 else parts[0]
            
            # Find the customer doc ID to update metadata
            customers = get_all_customers()
            c_doc = next((c for c in customers if c['customer_id'] == customer_id), None)
            if c_doc:
                update_customer_metadata(c_doc['id'], platform_used=platform, recent_order_id=order_id)
                phone_number = c_doc['phone_numbers'][0] if c_doc['phone_numbers'] else phone_number
    else:  # 'new'
        customer_name = request.form.get('new_customer_name', '').strip()
        phone_number = request.form.get('new_customer_phone', '').strip()
        phones = [phone_number] if phone_number else []
        if customer_name:
            c_doc_id = add_customer(customer_name, phones, platform_used=platform, recent_order_id=order_id)
            customer_id = c_doc_id

    data = {
        'date': request.form.get('date', ''),
        'order_id': order_id,
        'customer': customer_name,
        'customer_id': customer_id,
        'number': phone_number,
        'order_items': order_items,
        'platform': platform,
        'shipping': request.form.get('shipping', 0),
        'refund': request.form.get('refund', 0),
        'tax': request.form.get('tax', 0),
        'marketplace_fee': request.form.get('marketplace_fee', 0),
        'other_charges': request.form.get('other_charges', 0),
        'status': request.form.get('status', 'Pending'),
        'reviews': request.form.get('reviews', '').strip(),
    }

    if data['customer'] and len(order_items) > 0:
        add_order(data)
        flash('Order logged successfully.', 'success')
    else:
        flash('Customer and at least one Product are required.', 'error')

    return redirect(url_for('orders.orders_list'))


@orders_bp.route('/edit/<doc_id>', methods=['POST'])
def order_edit(doc_id):
    # Guard: block edits on terminal orders
    from app import get_db
    db = get_db()
    doc = db.collection('orders').document(doc_id).get()
    if doc.exists:
        current_status = doc.to_dict().get('status', '')
        if current_status in TERMINAL_STATUSES:
            flash(f'Cannot edit a {current_status} order. It is locked.', 'error')
            return redirect(url_for('orders.orders_list'))

    order_items = parse_order_items(request.form)

    # Handle customer re-assignment for Unknown orders
    customer_name = request.form.get('customer', '').strip()
    phone_number  = request.form.get('number', '').strip()
    customer_id_field = request.form.get('customer_id', '').strip()
    platform = request.form.get('platform', '')
    order_id_field = request.form.get('order_id', '').strip()

    edit_customer_mode = request.form.get('edit_customer_mode', '')
    if edit_customer_mode == 'assign_existing':
        cust_val = request.form.get('edit_existing_customer', '')
        if cust_val:
            parts = cust_val.split(' - ', 1)
            customer_id_field = parts[0]
            customer_name = parts[1] if len(parts) > 1 else parts[0]
            customers = get_all_customers()
            c_doc = next((c for c in customers if c['customer_id'] == customer_id_field), None)
            if c_doc:
                update_customer_metadata(c_doc['id'], platform_used=platform, recent_order_id=order_id_field)
                phone_number = c_doc['phone_numbers'][0] if c_doc.get('phone_numbers') else phone_number
    elif edit_customer_mode == 'assign_new':
        new_name = request.form.get('edit_new_customer_name', '').strip()
        new_phone = request.form.get('edit_new_customer_phone', '').strip()
        if new_name:
            phones = [new_phone] if new_phone else []
            new_cid = add_customer(new_name, phones, platform_used=platform, recent_order_id=order_id_field)
            customer_name = new_name
            customer_id_field = new_cid
            phone_number = new_phone
    
    data = {
        'date': order_id_field and request.form.get('date', ''),
        'order_id': order_id_field,
        'customer': customer_name,
        'customer_id': customer_id_field,
        'number': phone_number,
        'order_items': order_items,
        'platform': platform,
        'shipping': request.form.get('shipping', 0),
        'refund': request.form.get('refund', 0),
        'tax': request.form.get('tax', 0),
        'marketplace_fee': request.form.get('marketplace_fee', 0),
        'other_charges': request.form.get('other_charges', 0),
        'reviews': request.form.get('reviews', '').strip(),
    }
    
    # Only update status if it's explicitly provided in the form (e.g. from bulk or a future field)
    if 'status' in request.form:
        data['status'] = request.form.get('status')

    # Restore date (was accidentally set to falsy above)
    data['date'] = request.form.get('date', '')
    update_order(doc_id, data)
    flash('Order updated.', 'success')
    return redirect(url_for('orders.orders_list'))


@orders_bp.route('/delete/<doc_id>', methods=['POST'])
def orders_delete(doc_id):
    if delete_order(doc_id):
        flash('Order and related records deleted.', 'success')
    else:
        flash('Order not found.', 'error')
    return redirect(url_for('orders.orders_list'))

@orders_bp.route('/set_status/<doc_id>', methods=['POST'])
def order_set_status(doc_id):
    from app import get_db
    db = get_db()
    doc = db.collection('orders').document(doc_id).get()
    if not doc.exists or doc.to_dict().get('status') in TERMINAL_STATUSES:
        flash('Order not found or status is locked.', 'error')
        return redirect(url_for('orders.orders_list'))
    new_status = request.form.get('status', '')
    if not new_status:
        return redirect(url_for('orders.orders_list'))
    order_data = {'status': new_status}
    if new_status == 'Shipped':
        order_data['shipping_id'] = request.form.get('shipping_id', '')
    update_order(doc_id, order_data)
    return redirect(url_for('orders.orders_list'))


@orders_bp.route('/set_review/<doc_id>', methods=['POST'])
def order_set_review(doc_id):
    new_review = request.form.get('review', '')
    if new_review:
        from app import get_db
        get_db().collection('orders').document(doc_id).update({'reviews': new_review})
    return redirect(url_for('orders.orders_list'))


# ── JSON API: full order document for modal / contacts page ────────────────
@orders_bp.route('/api/order-detail/<order_id>')
def api_order_detail(order_id):
    """
    Returns the full Firestore document for an order looked up by its
    'order_id' field value (e.g. "test2312312313"), NOT the Firestore doc ID.
    Timestamps are ISO-serialised so they are JSON-safe.
    """
    from app import get_db
    from google.cloud.firestore_v1 import FieldFilter
    db = get_db()

    # Query by the user-facing order_id field
    matches = list(
        db.collection('orders')
          .where(filter=FieldFilter('order_id', '==', order_id))
          .limit(1)
          .stream()
    )
    if not matches:
        return jsonify({'error': 'Order not found', 'order_id': order_id}), 404

    doc = matches[0]
    data = {'doc_id': doc.id, **doc.to_dict()}

    # Ensure shipping_id is always present (older docs may not have it)
    data.setdefault('shipping_id', '')
    data.setdefault('other_charges', 0)

    # Serialize all top-level datetime / Firestore timestamp fields
    for key, val in list(data.items()):
        if hasattr(val, 'isoformat'):
            data[key] = val.isoformat()

    # Serialize timestamps inside order_items too
    serialised_items = []
    for item in data.get('order_items', []):
        serialised_item = {}
        for k, v in item.items():
            serialised_item[k] = v.isoformat() if hasattr(v, 'isoformat') else v
        serialised_items.append(serialised_item)
    data['order_items'] = serialised_items

    # Serialize status_history timestamps
    history = []
    for entry in data.get('status_history', []):
        history.append({
            k: v.isoformat() if hasattr(v, 'isoformat') else v
            for k, v in entry.items()
        })
    data['status_history'] = history

    return jsonify(data)
