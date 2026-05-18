/* ═══════════════════════════════════════════════════════════════
   Grah Decor POS — Client-side JavaScript
   ═══════════════════════════════════════════════════════════════ */

// ── Sidebar Toggle (Mobile) ─────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
    const toggle = document.getElementById('sidebarToggle');
    const sidebar = document.getElementById('sidebar');

    if (toggle && sidebar) {
        toggle.addEventListener('click', function () {
            sidebar.classList.toggle('open');
        });

        // Close sidebar when clicking outside
        document.addEventListener('click', function (e) {
            if (sidebar.classList.contains('open') &&
                !sidebar.contains(e.target) &&
                !toggle.contains(e.target)) {
                sidebar.classList.remove('open');
            }
        });
    }

    // ── Auto-dismiss flash messages ─────────────────────────
    const flashes = document.querySelectorAll('.flash');
    flashes.forEach(function (flash) {
        setTimeout(function () {
            flash.style.transition = 'opacity 0.3s, transform 0.3s';
            flash.style.opacity = '0';
            flash.style.transform = 'translateY(-8px)';
            setTimeout(function () { flash.remove(); }, 300);
        }, 4000);
    });
});


// ── Toggle form visibility ──────────────────────────────────
function toggleForm(formId) {
    var el = document.getElementById(formId);
    if (el) el.classList.toggle('hidden');
}


// ── Close modal ─────────────────────────────────────────────
function closeModal(modalId) {
    var el = document.getElementById(modalId);
    if (el) el.classList.add('hidden');
}

// Close modal on overlay click
document.addEventListener('click', function (e) {
    if (e.target.classList.contains('modal-overlay')) {
        e.target.classList.add('hidden');
    }
});

// Close modal on Escape key
document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal-overlay').forEach(function (m) {
            m.classList.add('hidden');
        });
    }
});

// ── Disable Scroll on Number Inputs ────────────────────────
// This prevents accidental value changes when scrolling the page
document.addEventListener('wheel', function (e) {
    if (document.activeElement.type === 'number') {
        document.activeElement.blur();
    }
}, { passive: false });


// ── Double-Submit Guard ─────────────────────────────────────
// Fires on every POST form in the app. On the first submit:
//   1. Disables the submit button immediately (blocks all further clicks).
//   2. Replaces its text with a spinner + "Saving…" label.
// The page will reload (Flask redirect) on completion, restoring the button.
// For modal forms that might be re-opened without a page reload, call
// resetFormSubmitGuard(formEl) when the modal is opened to restore the button.
(function () {
    document.addEventListener('submit', function (e) {
        var form = e.target;

        // Only guard POST forms — GET forms (filter/search) are fine to resubmit.
        if (!form || (form.method || '').toUpperCase() !== 'POST') return;

        // Find the primary submit button inside this form.
        // Prefer a button[type=submit]; fall back to input[type=submit].
        var btn = form.querySelector('button[type="submit"]:not([data-no-guard])');
        if (!btn) btn = form.querySelector('input[type="submit"]:not([data-no-guard])');
        if (!btn) return;

        // Already guarded — block the duplicate submit entirely.
        if (btn.dataset.submitting === '1') {
            e.preventDefault();
            return;
        }

        // Arm the guard.
        btn.dataset.submitting    = '1';
        btn.dataset.originalText  = btn.innerHTML;
        btn.disabled              = true;
        btn.innerHTML             = '<span style="display:inline-flex;align-items:center;gap:.4rem;">'
                                  + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" '
                                  + 'stroke="currentColor" stroke-width="2.5" stroke-linecap="round" '
                                  + 'stroke-linejoin="round" style="animation:spin .7s linear infinite;">'
                                  + '<path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83'
                                  + 'M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>'
                                  + 'Saving…</span>';
    }, true); // capture phase — fires before any inline onsubmit handlers

    // Inject the spinner keyframe once into the document head.
    var style = document.createElement('style');
    style.textContent = '@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }';
    document.head.appendChild(style);
}());


// ── Modal Form Reset Helper ─────────────────────────────────
// Call this whenever a modal is opened so that any previously-guarded
// submit button is restored to its original state.
// Usage: resetFormSubmitGuard(document.getElementById('variantForm'))
function resetFormSubmitGuard(formEl) {
    if (!formEl) return;
    var btn = formEl.querySelector('button[type="submit"], input[type="submit"]');
    if (!btn) return;
    if (btn.dataset.submitting === '1') {
        btn.disabled           = false;
        btn.innerHTML          = btn.dataset.originalText || btn.innerHTML;
        btn.dataset.submitting = '0';
    }
}

