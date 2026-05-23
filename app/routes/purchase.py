from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from datetime import datetime
from app.services.purchase_service import (
    get_all_purchase_orders,
    add_purchase_order,
    mark_po_sent,
    mark_po_received,
    mark_po_paid,
    cancel_po,
    return_po,
    # Phase 2 — partial fulfillment
    partial_receive_po,
    partial_pay_po,
    partial_return_po,
    # Phase 3 — decoupled refund
    log_refund,
    close_eligible_pos,
)
from app.services.inventory_service import get_all_raw_materials
from app.services.contact_service import get_all_vendors

purchase_bp = Blueprint('purchase', __name__, url_prefix='/purchases')

@purchase_bp.route('/')
def purchase_list():
    close_eligible_pos()
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

    status = request.args.get('status')
    cursor_id = request.args.get('cursor_id')
    direction = request.args.get('direction', 'next')

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

    purchases, has_prev, has_next = get_all_purchase_orders(
        date_from=df, date_to=dt,
        status=status or None,
        cursor_id=cursor_id,
        direction=direction,
        limit=20
    )
    total_spent = sum(p.get('total_cost', 0) for p in purchases if p.get('status') != 'Cancelled')
    raw_materials = get_all_raw_materials()
    vendors = get_all_vendors()

    return render_template('purchase.html', purchases=purchases, total_spent=total_spent,
                           raw_materials=raw_materials, vendors=vendors, 
                           date_from=date_from or '', date_to=date_to or '',
                           filter_status=status or '',
                           has_prev=has_prev, has_next=has_next)

@purchase_bp.route('/add', methods=['POST'])
def purchase_add():
    vendor_name = request.form.get('vendor_name', '').strip()

    items = []
    item_names = request.form.getlist('item[]')
    quantities = request.form.getlist('quantity[]')
    unit_costs = request.form.getlist('unit_cost[]')

    for i in range(len(item_names)):
        name = item_names[i].strip()
        if not name:
            continue
        try:
            qty  = float(quantities[i]) if i < len(quantities) and quantities[i] else 0.0
            cost = float(unit_costs[i])  if i < len(unit_costs)  and unit_costs[i]  else 0.0
        except (ValueError, TypeError):
            flash('Invalid quantity or unit cost — only numbers are allowed.', 'error')
            return redirect(url_for('purchase.purchase_list'))
        items.append({
            'item': name,
            'quantity': qty,
            'unit_cost': cost
        })

    if not vendor_name or not items:
        flash('Invalid purchase details. At least one valid item is required.', 'error')
        return redirect(url_for('purchase.purchase_list'))

    add_purchase_order(vendor_name, items)
    flash('Draft Purchase Order created.', 'success')
    return redirect(url_for('purchase.purchase_list'))

@purchase_bp.route('/sent/<po_id>', methods=['POST'])
def purchase_sent(po_id):
    mark_po_sent(po_id)
    flash('Purchase Order marked as Sent.', 'success')
    return redirect(url_for('purchase.purchase_list'))

@purchase_bp.route('/received/<po_id>', methods=['POST'])
def purchase_received(po_id):
    if mark_po_received(po_id):
        flash('Items Received and added to inventory.', 'success')
    else:
        flash('Could not receive items (maybe already received).', 'error')
    return redirect(url_for('purchase.purchase_list'))

@purchase_bp.route('/paid/<po_id>', methods=['POST'])
def purchase_paid(po_id):
    payment_id = request.form.get('payment_id', '').strip()
    if not payment_id:
        flash('Payment ID or UTR is required to mark as paid.', 'error')
        return redirect(url_for('purchase.purchase_list'))

    if mark_po_paid(po_id, payment_id):
        flash('Payment logged to cashbook. PO marked as Paid.', 'success')
    else:
        flash('Could not mark as paid.', 'error')
    return redirect(url_for('purchase.purchase_list'))

@purchase_bp.route('/cancel/<po_id>', methods=['POST'])
def purchase_cancel(po_id):
    if cancel_po(po_id):
        flash('Purchase Order cancelled.', 'success')
    else:
        flash('Failed to cancel PO.', 'error')
    return redirect(url_for('purchase.purchase_list'))

@purchase_bp.route('/return/<po_id>', methods=['POST'])
def purchase_return(po_id):
    refund_amount = request.form.get('refund_amount', 0)
    if return_po(po_id, refund_amount=refund_amount):
        flash('Purchase Order returned successfully.', 'success')
    else:
        flash('Failed to return PO.', 'error')
    return redirect(url_for('purchase.purchase_list'))


# =============================================================================
# Phase 2 — JSON API routes for partial fulfillment
# =============================================================================
# All three endpoints accept JSON bodies and return JSON.
# They do NOT redirect or flash — designed to be called via fetch() from JS.
#
# POST /purchases/api/partial-receive/<po_id>
# POST /purchases/api/partial-pay/<po_id>
# POST /purchases/api/partial-return/<po_id>
# =============================================================================

def _bad(msg: str, code: int = 400):
    """Helper: return a JSON error response."""
    return jsonify({'success': False, 'message': msg}), code


@purchase_bp.route('/api/partial-receive/<po_id>', methods=['POST'])
def api_partial_receive(po_id):
    """
    Partially receive items from a PO.

    Expected JSON body:
    {
        "received_quantities": {
            "Item Name A": 50,
            "Item Name B": 120
        }
    }

    Only items listed in received_quantities are updated.
    Quantities are clamped to what is still outstanding per item.
    """
    body = request.get_json(silent=True) or {}

    received_quantities = body.get('received_quantities')
    if not received_quantities or not isinstance(received_quantities, dict):
        return _bad('received_quantities must be a non-empty object mapping item names to quantities.')

    # Sanitise: ensure all values are numeric and non-negative
    sanitised = {}
    for name, qty in received_quantities.items():
        try:
            q = float(qty)
        except (TypeError, ValueError):
            return _bad(f'Invalid quantity for item "{name}": must be a number.')
        if q < 0:
            return _bad(f'Quantity for "{name}" cannot be negative.')
        sanitised[name] = q

    result = partial_receive_po(po_id, sanitised)
    status_code = 200 if result.get('success') else 400
    return jsonify(result), status_code


@purchase_bp.route('/api/partial-pay/<po_id>', methods=['POST'])
def api_partial_pay(po_id):
    """
    Record a partial (or full) payment against a PO, optionally with
    extra vendor charges that are folded into total_cost first.

    Expected JSON body:
    {
        "payment_amount": 5000,
        "payment_reference": "UTR123456",          // optional
        "extra_charges": [                          // optional
            {"label": "Freight", "amount": 350},
            {"label": "Packing",  "amount": 50}
        ]
    }

    Rules:
    - payment_amount is clamped to (total_cost + extra_charges) - amount_paid.
    - extra_charges expand total_cost permanently (audit trail kept).
    - One combined cashbook outflow is created per call.
    """
    body = request.get_json(silent=True) or {}

    try:
        payment_amount = float(body.get('payment_amount', 0))
    except (TypeError, ValueError):
        return _bad('payment_amount must be a number.')

    if payment_amount <= 0:
        return _bad('payment_amount must be greater than zero.')

    payment_reference = str(body.get('payment_reference', '')).strip()

    # Validate extra_charges if present
    extra_charges = body.get('extra_charges', [])
    if not isinstance(extra_charges, list):
        return _bad('extra_charges must be an array.')

    validated_charges = []
    for idx, charge in enumerate(extra_charges):
        if not isinstance(charge, dict):
            return _bad(f'extra_charges[{idx}] must be an object with "label" and "amount".')
        label = str(charge.get('label', '')).strip()
        if not label:
            return _bad(f'extra_charges[{idx}] is missing a "label".')
        try:
            amount = float(charge.get('amount', 0))
        except (TypeError, ValueError):
            return _bad(f'extra_charges[{idx}].amount must be a number.')
        if amount < 0:
            return _bad(f'extra_charges[{idx}].amount cannot be negative.')
        validated_charges.append({'label': label, 'amount': amount})

    result = partial_pay_po(
        po_id,
        payment_amount=payment_amount,
        payment_reference=payment_reference,
        extra_charges=validated_charges,
    )
    status_code = 200 if result.get('success') else 400
    return jsonify(result), status_code


@purchase_bp.route('/api/partial-return/<po_id>', methods=['POST'])
def api_partial_return(po_id):
    """
    Partially return received items to the vendor.

    Expected JSON body:
    {
        "return_quantities": {
            "Item Name A": 10
        },
        "refund_amount": 800,          // optional — logs cashbook inflow
        "reason_note": "Defective"     // optional — stored in status_history
    }

    Quantities are clamped to (received_qty - returned_qty) per item.
    Physical inventory is reduced immediately.
    """
    body = request.get_json(silent=True) or {}

    return_quantities = body.get('return_quantities')
    if not return_quantities or not isinstance(return_quantities, dict):
        return _bad('return_quantities must be a non-empty object mapping item names to quantities.')

    sanitised = {}
    for name, qty in return_quantities.items():
        try:
            q = float(qty)
        except (TypeError, ValueError):
            return _bad(f'Invalid quantity for item "{name}": must be a number.')
        if q < 0:
            return _bad(f'Quantity for "{name}" cannot be negative.')
        sanitised[name] = q

    try:
        refund_amount = float(body.get('refund_amount', 0))
    except (TypeError, ValueError):
        return _bad('refund_amount must be a number.')

    reason_note = str(body.get('reason_note', '')).strip()

    result = partial_return_po(
        po_id,
        return_quantities=sanitised,
        refund_amount=refund_amount,
        reason_note=reason_note,
    )
    status_code = 200 if result.get('success') else 400
    return jsonify(result), status_code


@purchase_bp.route('/api/log-refund/<po_id>', methods=['POST'])
def api_log_refund(po_id):
    """
    Record collection of a vendor refund on a PO whose balance_due is negative
    (i.e. the vendor owes us money after a partial return).

    Expected JSON body:
    {
        "refund_amount": 500,
        "payment_reference": "HDFC-5678"   // required
    }
    """
    body = request.get_json(silent=True) or {}

    try:
        refund_amount = float(body.get('refund_amount', 0))
    except (TypeError, ValueError):
        return _bad('refund_amount must be a number.')

    if refund_amount <= 0:
        return _bad('refund_amount must be greater than zero.')

    reference = str(body.get('payment_reference', '')).strip()
    if not reference:
        return _bad('payment_reference (UTR / Ref) is required.')

    result = log_refund(po_id, refund_amount=refund_amount, reference=reference)
    status_code = 200 if result.get('success') else 400
    return jsonify(result), status_code
