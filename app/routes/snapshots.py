from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from datetime import datetime, timezone
from app import get_db
from app.services.snapshot_service import (
    get_all_snapshots,
    get_open_snapshot,
    get_snapshot_by_id,
    take_opening_snapshot,
    take_closing_snapshot,
    get_all_ready_stock_snapshots,
    get_ready_stock_snapshot,
    get_ready_stock_snapshot_live,
    generate_ready_stock_snapshot,
    backfill_ready_stock_snapshots,
)

snapshots_bp = Blueprint('snapshots', __name__, url_prefix='/snapshots')


# ── Raw Material Audit — List ──────────────────────────────────────────────────

@snapshots_bp.route('/')
def snapshots_list():
    snapshots   = get_all_snapshots()
    open_period = get_open_snapshot()

    # Pass existing ready-stock snapshots so the tab can show a month picker
    rs_snapshots = get_all_ready_stock_snapshots()

    # Build a default "view month" for the ready stock tab: current month
    now = datetime.now(timezone.utc)
    default_year  = now.year
    default_month = now.month

    return render_template(
        'snapshots.html',
        snapshots=snapshots,
        open_period=open_period,
        rs_snapshots=rs_snapshots,
        default_year=default_year,
        default_month=default_month,
        active_tab=request.args.get('tab', 'raw'),
    )


# ── Raw Material Audit — Opening ───────────────────────────────────────────────

@snapshots_bp.route('/opening', methods=['POST'])
def take_opening():
    result, doc_id = take_opening_snapshot()
    if result == 'ok':
        flash('New audit period opened successfully.', 'success')
    elif result == 'already_open':
        flash('An audit period is already open. Close it before starting a new one.', 'error')
    else:
        flash('Could not open a new audit period.', 'error')
    return redirect(url_for('snapshots.snapshots_list'))


# ── Raw Material Audit — Closing form ─────────────────────────────────────────

@snapshots_bp.route('/closing/<doc_id>', methods=['GET'])
def closing_form(doc_id):
    snapshot = get_snapshot_by_id(doc_id)
    if not snapshot or not snapshot.get('opening'):
        flash('Opening snapshot must be taken before closing.', 'error')
        return redirect(url_for('snapshots.snapshots_list'))
    if snapshot.get('status') == 'closed':
        flash('This period is already closed.', 'error')
        return redirect(url_for('snapshots.snapshots_list'))

    db = get_db()
    rm_docs = db.collection('raw_materials').stream()
    system_qty_map = {d.to_dict().get('name'): float(d.to_dict().get('quantity', 0)) for d in rm_docs}

    return render_template('snapshots_closing.html', snapshot=snapshot, doc_id=doc_id, system_qty_map=system_qty_map)


# ── Raw Material Audit — Closing submit ───────────────────────────────────────

@snapshots_bp.route('/closing/<doc_id>', methods=['POST'])
def take_closing(doc_id):
    snapshot = get_snapshot_by_id(doc_id)
    if not snapshot:
        flash('Snapshot not found.', 'error')
        return redirect(url_for('snapshots.snapshots_list'))

    opening_materials = snapshot.get('opening', {}).get('materials', [])
    closing_counts = {}
    for m in opening_materials:
        name = m['name']
        raw = request.form.get(f'closing_{name}', '').strip()
        try:
            closing_counts[name] = float(raw)
        except (ValueError, TypeError):
            closing_counts[name] = 0.0

    result = take_closing_snapshot(doc_id, closing_counts)

    if result == 'ok':
        flash('Audit period closed. Raw material quantities have been updated.', 'success')
    elif result == 'already_closed':
        flash('This period is already closed.', 'error')
    elif result == 'no_opening':
        flash('No opening snapshot found for this period.', 'error')
    elif result.startswith('invalid_count:'):
        mat_name = result.split(':', 1)[1]
        flash(f'Validation failed: Closing count cannot exceed current stock for {mat_name}.', 'error')
    else:
        flash('Could not close this period. Please try again.', 'error')

    return redirect(url_for('snapshots.snapshots_list'))


# ═══════════════════════════════════════════════════════════════════════════════
# ── Ready Stock Monthly Reports ────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════════

@snapshots_bp.route('/ready-stock')
def rs_report_view():
    """
    View a single month's Ready Stock report.
    - Current month  → always calculated live (no caching).
    - Past months    → served from cache; if not cached, generate now and cache it.
    Query params: year (int), month (int).
    """
    now = datetime.now(timezone.utc)

    try:
        year  = int(request.args.get('year',  now.year))
        month = int(request.args.get('month', now.month))
        if not (1 <= month <= 12) or year < 2020:
            raise ValueError
    except (ValueError, TypeError):
        year, month = now.year, now.month

    is_current = (year == now.year and month == now.month)

    if is_current:
        snapshot = get_ready_stock_snapshot_live(year, month)
        cache_status = 'live'
    else:
        snapshot = get_ready_stock_snapshot(year, month)
        if snapshot is None:
            # Auto-generate on first request (on-demand cache)
            status, key = generate_ready_stock_snapshot(year, month)
            if status in ('ok', 'already_exists'):
                snapshot = get_ready_stock_snapshot(year, month)
            else:
                flash(f'Could not generate snapshot for {year}-{month:02d}: {status}', 'error')
                return redirect(url_for('snapshots.snapshots_list', tab='ready_stock'))
        cache_status = 'cached'

    # Build month navigation (previous / next month for quick nav)
    prev_month = month - 1
    prev_year  = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1
    next_month = month + 1
    next_year  = year
    if next_month > 12:
        next_month = 1
        next_year += 1

    rs_snapshots = get_all_ready_stock_snapshots()

    return render_template(
        'snapshots.html',
        active_tab='ready_stock',
        # Current month state
        snapshot=None,
        open_period=get_open_snapshot(),
        snapshots=get_all_snapshots(),
        # Ready Stock specific
        rs_snapshots=rs_snapshots,
        rs_report=snapshot,
        rs_year=year,
        rs_month=month,
        rs_is_current=is_current,
        rs_cache_status=cache_status,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month,
        default_year=now.year,
        default_month=now.month,
    )


@snapshots_bp.route('/ready-stock/regenerate', methods=['POST'])
def rs_regenerate():
    """Force-regenerate a cached snapshot for a closed month."""
    try:
        year  = int(request.form.get('year',  0))
        month = int(request.form.get('month', 0))
        if not (1 <= month <= 12) or year < 2020:
            raise ValueError
    except (ValueError, TypeError):
        flash('Invalid year/month for regeneration.', 'error')
        return redirect(url_for('snapshots.snapshots_list', tab='ready_stock'))

    status, key = generate_ready_stock_snapshot(year, month, force=True)
    if status == 'ok':
        flash(f'Snapshot for {key} regenerated successfully.', 'success')
    elif status == 'current_month':
        flash('The current month is always calculated live and cannot be cached.', 'error')
    else:
        flash(f'Regeneration failed: {status}', 'error')

    return redirect(url_for('snapshots.rs_report_view', year=year, month=month))


@snapshots_bp.route('/ready-stock/backfill', methods=['POST'])
def rs_backfill():
    """
    Retroactively generate snapshots for all past months starting from the given date.
    Only generates months that don't already have a cached snapshot (safe to run multiple times).
    """
    try:
        start_year  = int(request.form.get('start_year',  2026))
        start_month = int(request.form.get('start_month', 1))
        if not (1 <= start_month <= 12) or start_year < 2020:
            raise ValueError
    except (ValueError, TypeError):
        flash('Invalid start date for backfill.', 'error')
        return redirect(url_for('snapshots.snapshots_list', tab='ready_stock'))

    results = backfill_ready_stock_snapshots(start_year, start_month)

    ok_count      = sum(1 for s, _ in results if s == 'ok')
    skipped_count = sum(1 for s, _ in results if s == 'already_exists')
    total         = len(results)

    if total == 0:
        flash('No past months to backfill — all months from that date are already in the current month.', 'error')
    else:
        flash(
            f'Backfill complete: {ok_count} snapshot(s) generated, '
            f'{skipped_count} already existed (skipped), out of {total} months processed.',
            'success'
        )

    return redirect(url_for('snapshots.snapshots_list', tab='ready_stock'))

