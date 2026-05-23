import os
import time

# TEMPORARY: Files are saved to local /static/receipts/ folder.
# Replace upload_receipt() with Firebase Storage implementation
# when migrating to office Firebase account with Blaze plan.
# Do not use this in production as-is — local files are not persistent on Render.

_RECEIPTS_DIR = os.path.join(os.path.dirname(__file__), '..', 'static', 'receipts')

def upload_receipt(file):
    """Temporarily saves receipt to local static/receipts/. Returns a relative URL."""
    if not file or not file.filename:
        return None

    os.makedirs(_RECEIPTS_DIR, exist_ok=True)

    timestamp = int(time.time())
    safe_name = file.filename.replace(' ', '_')
    filename = f"{timestamp}_{safe_name}"
    save_path = os.path.join(_RECEIPTS_DIR, filename)
    file.save(save_path)

    return f"/static/receipts/{filename}"
