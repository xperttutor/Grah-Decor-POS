import time
import os
from firebase_admin import storage


def upload_receipt(file):
    """Upload a receipt file to Firebase Storage and return its public URL.

    Path in bucket: receipts/{timestamp}_{original_filename}

    Returns the public HTTPS download URL on success, or None on any failure.
    """
    if not file or not file.filename:
        return None

    try:
        timestamp = int(time.time())
        safe_name = os.path.basename(file.filename).replace(' ', '_')
        blob_path = f"receipts/{timestamp}_{safe_name}"

        bucket = storage.bucket()
        blob = bucket.blob(blob_path)

        # Seek to start in case the stream was already partially read
        file.seek(0)

        blob.upload_from_file(
            file,
            content_type=file.content_type or 'application/octet-stream',
        )

        # Make the file publicly readable
        blob.make_public()

        return blob.public_url

    except Exception as exc:
        print(f"[storage_service] upload_receipt failed: {exc}")
        return None
