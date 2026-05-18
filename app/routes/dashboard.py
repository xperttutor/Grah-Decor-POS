from flask import Blueprint, render_template, request
from datetime import datetime, timezone
from app.services.dashboard_service import get_dashboard_data

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')


def _parse_month_param(month_str: str):
    """
    Parse a 'YYYY-MM' string from the query param.
    Returns (year, month) ints. Falls back to current UTC month on any error.
    """
    if month_str:
        try:
            dt = datetime.strptime(month_str, '%Y-%m')
            return dt.year, dt.month
        except ValueError:
            pass
    now = datetime.now(timezone.utc)
    return now.year, now.month


def _adjacent_month_keys(year: int, month: int):
    """Return (prev_key, next_key) as 'YYYY-MM' strings."""
    # Previous month
    if month == 1:
        prev_key = f'{year - 1:04d}-12'
    else:
        prev_key = f'{year:04d}-{month - 1:02d}'

    # Next month
    if month == 12:
        next_key = f'{year + 1:04d}-01'
    else:
        next_key = f'{year:04d}-{month + 1:02d}'

    return prev_key, next_key


@dashboard_bp.route('/')
def dashboard():
    month_param = request.args.get('month', '').strip()
    year, month = _parse_month_param(month_param)

    data = get_dashboard_data(year, month)

    prev_month_key, next_month_key = _adjacent_month_keys(year, month)

    return render_template(
        'dashboard.html',
        prev_month_key=prev_month_key,
        next_month_key=next_month_key,
        **data,
    )
