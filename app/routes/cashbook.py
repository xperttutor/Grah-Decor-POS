from flask import Blueprint, render_template, request, flash, redirect, url_for
from datetime import datetime
from app.services.cashbook_service import get_all_transactions, get_running_balance, add_cashbook_entry

cashbook_bp = Blueprint('cashbook', __name__, url_prefix='/cashbook')


@cashbook_bp.route('/')
def dashboard():
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')

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

    transactions, has_prev, has_next = get_all_transactions(
        date_from=df, date_to=dt,
        cursor_id=cursor_id,
        direction=direction,
        limit=20
    )

    balance = get_running_balance()

    filtered_inflow = sum(t['amount'] for t in transactions if t.get('type') == 'inflow')
    filtered_outflow = sum(t['amount'] for t in transactions if t.get('type') == 'outflow')
    filtered_net = filtered_inflow - filtered_outflow

    return render_template('cashbook.html',
                           transactions=transactions,
                           balance=balance,
                           filtered_inflow=filtered_inflow,
                           filtered_outflow=filtered_outflow,
                           filtered_net=filtered_net,
                           date_from=date_from or '',
                           date_to=date_to or '',
                           has_prev=has_prev,
                           has_next=has_next)

@cashbook_bp.route('/add_expense', methods=['POST'])
def add_expense():
    amount = request.form.get('amount')
    category = request.form.get('category', 'Misc')
    custom_category = request.form.get('custom_category', '').strip()
    item_name = request.form.get('item_name', '').strip()
    notes = request.form.get('notes', '').strip()
    expense_date = request.form.get('date', '').strip()
    receipt_file = request.files.get('receipt') or None

    if category == 'Other' and custom_category:
        category = custom_category

    if item_name:
        parts = [f"Item: {item_name}", category]
        if notes:
            parts.append(notes)
        notes = " - ".join(parts)

    if not amount:
        flash('Amount is required.', 'error')
        return redirect(url_for('cashbook.dashboard'))

    try:
        amount_val = float(amount)
        if amount_val <= 0:
            raise ValueError
    except ValueError:
        flash('Invalid amount.', 'error')
        return redirect(url_for('cashbook.dashboard'))

    if receipt_file and receipt_file.filename:
        receipt_file.seek(0, 2)
        file_size = receipt_file.tell()
        receipt_file.seek(0)
        if file_size > 3 * 1024 * 1024:
            flash('Receipt file must be under 3MB.', 'error')
            return redirect(url_for('cashbook.dashboard'))

    add_cashbook_entry(
        entry_type='outflow',
        category=category,
        description=notes,
        amount=amount_val,
        source='manual_expense',
        entry_date=expense_date,
        receipt_file=receipt_file
    )

    flash('Manual expense logged successfully.', 'success')
    return redirect(url_for('cashbook.dashboard'))
