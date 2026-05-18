from flask import Blueprint, render_template, request, flash, redirect, url_for
from datetime import datetime, timezone
from app.services.dashboard_service import get_dashboard_data

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')


def _parse_month_param(month_str: str):
    """
    Parse a 'YYYY-MM' string. Returns (year, month) ints.
    Falls back to current UTC month on any error.
    """
    if month_str:
        try:
            dt = datetime.strptime(month_str, '%Y-%m')
            return dt.year, dt.month
        except ValueError:
            pass
    now = datetime.now(timezone.utc)
    return now.year, now.month


def _parse_date_param(date_str: str):
    """
    Parse a 'YYYY-MM-DD' string into a date object, or None on failure.
    """
    if date_str:
        try:
            return datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            pass
    return None


def _adjacent_month_keys(year: int, month: int):
    """Return (prev_key, next_key) as 'YYYY-MM' strings."""
    if month == 1:
        prev_key = f'{year - 1:04d}-12'
    else:
        prev_key = f'{year:04d}-{month - 1:02d}'

    if month == 12:
        next_key = f'{year + 1:04d}-01'
    else:
        next_key = f'{year:04d}-{month + 1:02d}'

    return prev_key, next_key


@dashboard_bp.route('/')
def dashboard():
    # ── Custom date range params ─────────────────────────────────────────
    date_from_str = request.args.get('date_from', '').strip()
    date_to_str   = request.args.get('date_to',   '').strip()

    date_from = _parse_date_param(date_from_str)
    date_to   = _parse_date_param(date_to_str)

    custom_start = None
    custom_end   = None

    if date_from and date_to:
        if date_from > date_to:
            flash('Start date cannot be after end date.', 'error')
            return redirect(url_for('dashboard.dashboard'))
        # Both valid and in order — build exact UTC bounds
        custom_start = datetime(date_from.year, date_from.month, date_from.day,
                                0, 0, 0, tzinfo=timezone.utc)
        custom_end   = datetime(date_to.year,   date_to.month,   date_to.day,
                                23, 59, 59, 999999, tzinfo=timezone.utc)
        # Anchor the trend chart to the end month of the range
        year, month = date_to.year, date_to.month
    elif date_from_str or date_to_str:
        # One field filled, the other missing
        flash('Both start date and end date are required for custom filtering.', 'error')
        return redirect(url_for('dashboard.dashboard'))
    else:
        # No date range — fall back to monthly mode
        month_param = request.args.get('month', '').strip()
        year, month = _parse_month_param(month_param)

    data = get_dashboard_data(year, month,
                              custom_start=custom_start,
                              custom_end=custom_end)

    prev_month_key, next_month_key = _adjacent_month_keys(year, month)

    return render_template(
        'dashboard.html',
        prev_month_key=prev_month_key,
        next_month_key=next_month_key,
        filter_date_from=date_from_str,
        filter_date_to=date_to_str,
        **data,
    )
