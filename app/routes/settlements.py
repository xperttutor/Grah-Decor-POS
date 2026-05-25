from flask import Blueprint, render_template, request, redirect, url_for, flash
from app.services.settlement_service import (
    get_unsettled_orders,
    create_payment_settlement,
    get_settlement_batches,
    process_order_return,
    get_returned_orders,
    delete_settlement_batch,
    get_settled_orders_charges
)
from app.services.order_service import PLATFORMS
from app.routes.dashboard import _parse_month_param, _adjacent_month_keys

settlements_bp = Blueprint('settlements', __name__, url_prefix='/settlements')

@settlements_bp.route('/')
def settlements_list():
    platform_filter = request.args.get('platform', '')
    tab = request.args.get('tab', 'unsettled')
    
    unsettled_orders = get_unsettled_orders(platform=platform_filter if platform_filter else None)
    total_expected = sum(o.get('bank_settlement', 0) for o in unsettled_orders)
    
    cursor_id = request.args.get('cursor_id')
    direction = request.args.get('direction', 'next')
    
    batches, has_prev, has_next = get_settlement_batches(
        cursor_id=cursor_id,
        direction=direction,
        limit=20
    )
    returned = get_returned_orders()

    # Metrics for Returns tab
    total_damaged = sum(1 for o in returned if o.get('item_condition') == 'damaged')
    total_penalties = sum(float(o.get('penalty_amount', 0)) for o in returned)
    
    # Other Charges / Settled Orders metrics
    month_param = request.args.get('month', '').strip()
    year, month = _parse_month_param(month_param)
    prev_month_key, next_month_key = _adjacent_month_keys(year, month)
    
    settled_orders, settled_summary = get_settled_orders_charges(year, month, platform=platform_filter if platform_filter else None)
    
    return render_template('settlements.html',
                           orders=unsettled_orders,
                           platforms=PLATFORMS,
                           filter_platform=platform_filter,
                           total_expected=total_expected,
                           batches=batches,
                           active_tab=tab,
                           returned_orders=returned,
                           total_damaged=total_damaged,
                           total_penalties=total_penalties,
                           has_prev_batch=has_prev,
                           has_next_batch=has_next,
                           settled_orders=settled_orders,
                           settled_summary=settled_summary,
                           month_key=f"{year:04d}-{month:02d}",
                           prev_month_key=prev_month_key,
                           next_month_key=next_month_key)

@settlements_bp.route('/add', methods=['POST'])
def add_settlement():
    platform = request.form.get('platform', '')
    utr_number = request.form.get('utr_number', '').strip()
    amount_received = request.form.get('amount_received', 0)
    settlement_date = request.form.get('settlement_date', '')
    notes = request.form.get('notes', '').strip()
    platform_deductions = request.form.get('platform_deductions', 0)
    
    order_ids = request.form.getlist('order_ids')
    
    if not order_ids:
        flash("No orders selected for settlement.", "error")
        return redirect(url_for('settlements.settlements_list'))

    if not utr_number:
        flash("UTR Number is required.", "error")
        return redirect(url_for('settlements.settlements_list'))

    try:
        amount_received = float(amount_received)
    except (ValueError, TypeError):
        flash('Amount received must be a valid number.', 'error')
        return redirect(url_for('settlements.settlements_list'))
        
    create_payment_settlement(
        platform=platform,
        utr_number=utr_number,
        amount_received=amount_received,
        order_ids=order_ids,
        settlement_date=settlement_date,
        notes=notes,
        platform_deductions=platform_deductions
    )
    
    flash(f"Settlement logged. {len(order_ids)} orders marked as Settled.", "success")
    return redirect(url_for('settlements.settlements_list'))


@settlements_bp.route('/process_return', methods=['POST'])
def process_return():
    order_id       = request.form.get('order_id', '').strip()
    return_type    = request.form.get('return_type', 'rto')
    penalty_amount = request.form.get('penalty_amount', 0)
    item_condition = request.form.get('item_condition', 'damaged')
    
    if not order_id:
        flash("No order specified.", "error")
        return redirect(url_for('settlements.settlements_list'))
    
    try:
        penalty = float(penalty_amount) if penalty_amount else 0
    except (ValueError, TypeError):
        penalty = 0
    
    if process_order_return(order_id, return_type, penalty, item_condition):
        status_label = 'RTO' if return_type == 'rto' else 'Returned'
        restock_label = ' Items restocked.' if item_condition == 'restock' else ' Items marked as damaged.'
        flash(f"Order marked as {status_label}.{restock_label}", "success")
    else:
        flash("Could not process return.", "error")
    
    return redirect(url_for('settlements.settlements_list'))

@settlements_bp.route('/delete/<batch_id>', methods=['POST'])
def delete_batch(batch_id):
    if delete_settlement_batch(batch_id):
        flash("Settlement batch deleted and orders reverted.", "success")
    else:
        flash("Could not delete settlement batch.", "error")
    return redirect(url_for('settlements.settlements_list', tab='batches'))
