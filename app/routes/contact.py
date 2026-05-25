from flask import Blueprint, render_template, request, flash, redirect, url_for, jsonify
from app.services.contact_service import (
    get_all_vendors, get_all_customers, add_vendor,
    update_vendor, update_customer, get_customer_lifetime_value
)

contact_bp = Blueprint('contact', __name__, url_prefix='/contacts')

@contact_bp.route('/')
def contacts_list():
    tab = request.args.get('tab', 'vendors')
    vendors = get_all_vendors()
    cursor_id = request.args.get('cursor_id')
    direction = request.args.get('direction', 'next')
    
    customers, has_prev, has_next = get_all_customers(
        cursor_id=cursor_id,
        direction=direction,
        limit=20
    )
    return render_template('contacts.html', vendors=vendors, customers=customers, active_tab=tab,
                           has_prev_customer=has_prev, has_next_customer=has_next)

@contact_bp.route('/vendor/add', methods=['POST'])
def add_vendor_route():
    name = request.form.get('name', '').strip()
    
    # Extract multiple phone numbers dynamically
    phone_numbers = []
    for key, value in request.form.items():
        if key.startswith('phone_') and value.strip():
            phone_numbers.append(value.strip())
            
    if not name:
        flash('Vendor name is required.', 'error')
        return redirect(url_for('contact.contacts_list', tab='vendors'))
        
    add_vendor(name, phone_numbers)
    flash('Vendor added successfully.', 'success')
    return redirect(url_for('contact.contacts_list', tab='vendors'))

@contact_bp.route('/vendor/update/<vendor_id>', methods=['POST'])
def update_vendor_route(vendor_id):
    name = request.form.get('name', '').strip()
    
    # In inline edit, we might send phone numbers as a comma-separated string or multiple fields
    # Let's support both for flexibility.
    phone_numbers_raw = request.form.get('phone_numbers', '')
    if phone_numbers_raw:
        phone_numbers = [p.strip() for p in phone_numbers_raw.split(',') if p.strip()]
    else:
        phone_numbers = []
        for key, value in request.form.items():
            if key.startswith('phone_') and value.strip():
                phone_numbers.append(value.strip())
            
    if not name:
        flash('Vendor name is required.', 'error')
        return redirect(url_for('contact.contacts_list', tab='vendors'))
        
    update_vendor(vendor_id, name, phone_numbers)
    flash('Vendor updated successfully.', 'success')
    return redirect(url_for('contact.contacts_list', tab='vendors'))

@contact_bp.route('/customer/update/<customer_id>', methods=['POST'])
def update_customer_route(customer_id):
    name = request.form.get('name', '').strip()

    phone_numbers_raw = request.form.get('phone_numbers', '')
    if phone_numbers_raw:
        phone_numbers = [p.strip() for p in phone_numbers_raw.split(',') if p.strip()]
    else:
        phone_numbers = []

    if not name:
        flash('Customer name is required.', 'error')
        return redirect(url_for('contact.contacts_list', tab='customers'))

    update_customer(customer_id, name, phone_numbers)
    flash('Customer updated successfully.', 'success')
    return redirect(url_for('contact.contacts_list', tab='customers'))


# ── JSON API: all orders for a customer ─────────────────────────────────────
@contact_bp.route('/api/customer-orders/<customer_id>')
def api_customer_orders(customer_id):
    """
    Returns all orders associated with a customer_id as JSON.
    Queries the orders collection by the 'customer_id' field (GDC-XXXX)
    which is now persisted on every order document by add_order().
    Fields: order_id, date, status, platform, bank_settlement, order_items summary.
    """
    from app import get_db
    from google.cloud.firestore_v1 import FieldFilter
    db = get_db()

    # Resolve customer name for display only
    customers, _, _ = get_all_customers()
    cust = next((c for c in customers if c.get('customer_id') == customer_id), None)
    if not cust:
        return jsonify({'customer_id': customer_id, 'orders': [], 'error': 'Customer not found'}), 404
    customer_name = cust.get('name', '')

    # Query by customer_id field — unique per contact record
    docs = (
        db.collection('orders')
          .where(filter=FieldFilter('customer_id', '==', customer_id))
          .stream()
    )
    orders = []
    for d in docs:
        data = d.to_dict()
        # Serialize date safely
        order_date = data.get('date')
        if hasattr(order_date, 'isoformat'):
            order_date = order_date.isoformat()
        else:
            order_date = str(order_date) if order_date else ''
        # Summarise order items
        items = data.get('order_items', [])
        items_summary = ', '.join(
            f"{it.get('product', '')}{' (' + it.get('color') + ')' if it.get('color') else ''} x{int(float(it.get('quantity', 1)))}"
            for it in items if it.get('product')
        )
        orders.append({
            'doc_id':          d.id,
            'order_id':        data.get('order_id', ''),
            'date':            order_date,
            'status':          data.get('status', ''),
            'platform':        data.get('platform', ''),
            'bank_settlement': float(data.get('bank_settlement', 0) or 0),
            'items_summary':   items_summary,
        })
    # Sort newest first by date string
    orders.sort(key=lambda x: x['date'], reverse=True)
    return jsonify({'customer_id': customer_id, 'customer_name': customer_name, 'orders': orders})


# ── JSON API: Customer Lifetime Value ────────────────────────────────────────
@contact_bp.route('/api/customer-clv/<customer_id>')
def api_customer_clv(customer_id):
    """
    Returns the Customer Lifetime Value (sum of bank_settlement for
    Delivered + Settled orders) as JSON.
    Queries by customer_id field — correct even for multiple Unknown records.
    """
    customers, _, _ = get_all_customers()
    cust = next((c for c in customers if c.get('customer_id') == customer_id), None)
    customer_name = cust.get('name', '') if cust else ''
    clv = get_customer_lifetime_value(customer_id)
    return jsonify({'customer_id': customer_id, 'customer_name': customer_name, 'clv': clv})
