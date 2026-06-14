"""
Backfill content_hash for existing Firestore image records.
Downloads each original from GCS, computes MD5, writes back to Firestore.
Run from Cloud Shell (us-central1) to avoid GCS egress charges.
"""
import hashlib
import io
from google.cloud import storage, firestore
from tqdm import tqdm

PROJECT_ID = "life-begins-at-40"
BUCKET_NAME = "lb40-bucket"
DATABASE = "media-metadata"

storage_client = storage.Client(project=PROJECT_ID)
db = firestore.Client(project=PROJECT_ID, database=DATABASE)
bucket = storage_client.bucket(BUCKET_NAME)

def backfill():
    docs = list(db.collection("images").stream())
    to_process = [d for d in docs if not d.to_dict().get("content_hash")]
    print(f"{len(to_process)} images need backfill (out of {len(docs)} total)")

    skipped, updated, errors = 0, 0, 0
    for doc in tqdm(to_process, unit="photo"):
        data = doc.to_dict()
        filename = data.get("name")
        if not filename:
            skipped += 1
            continue
        try:
            blob = bucket.blob(f"originals/{filename}")
            file_bytes = io.BytesIO()
            blob.download_to_file(file_bytes)
            content_hash = hashlib.md5(file_bytes.getvalue()).hexdigest()
            doc.reference.update({"content_hash": content_hash})
            updated += 1
        except Exception as e:
            print(f"\nError on {filename}: {e}")
            errors += 1

    print(f"\n✅ Done. {updated} updated, {skipped} skipped, {errors} errors.")

if __name__ == "__main__":
    backfill()
