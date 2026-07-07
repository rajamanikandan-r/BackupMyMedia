"""
Backfill GPS-based location tags for existing Firestore image records.

Only photos from GPS-capable smartphones are processed — dedicated cameras,
scanners, and unknown devices are skipped immediately without any GCS download.

Strategy (per phone record, in order):
  1. If the record already has latitude + longitude stored, reverse-geocode
     directly — no GCS download needed.
  2. Otherwise, download the first 64 KB from GCS and extract GPS EXIF.
     (EXIF/APP1 is always at the start of a JPEG, so 64 KB is sufficient.)
  3. If no GPS data can be found by either method, the record is skipped.

For each matched record the script:
  - Sets the `location` field  (e.g. "San Francisco, United States")
  - Sets the `latitude` / `longitude` fields
  - Appends the lowercased location tag to the `tags` array (deduped)
  - Sets `location_backfilled = True` so the script is safe to re-run
    (use --force to re-process already-backfilled records)

Usage:
  python backfill_location.py [--force] [--dry-run] [--limit N]
"""

import argparse
import io

from google.cloud import firestore, storage
from tqdm import tqdm

from gps_location import get_location_tag, reverse_geocode

# ── Configuration ──────────────────────────────────────────────────────────────
PROJECT_ID = "life-begins-at-40"
BUCKET_NAME = "lb40-bucket"
DATABASE    = "media-metadata"
# ───────────────────────────────────────────────────────────────────────────────

# ── Phone detection ────────────────────────────────────────────────────────────
# Camera makes/models that reliably embed GPS. Matched case-insensitively.
PHONE_MAKES = (
    "apple",
    "samsung",
    "google",
    "huawei",
    "xiaomi",
    "oneplus",
    "oppo",
    "vivo",
    "motorola",
    "nokia",
    "lg",
    "sony",       # Sony Xperia phones (dedicated Sony cameras report "SONY" in caps)
)

PHONE_MODEL_PREFIXES = (
    "iphone",
    "ipad",       # iPads with cellular also embed GPS
    "pixel",
    "nexus",
    "galaxy",
    "redmi",
    "mi ",
)


def is_phone(data: dict) -> bool:
    """Return True if the camera make/model looks like a GPS-capable smartphone."""
    make  = (data.get("make")   or "").lower().strip()
    model = (data.get("camera") or "").lower().strip()
    if any(make.startswith(m) for m in PHONE_MAKES):
        return True
    if any(model.startswith(m) for m in PHONE_MODEL_PREFIXES):
        return True
    return False
# ───────────────────────────────────────────────────────────────────────────────


def build_clients():
    storage_client = storage.Client(project=PROJECT_ID)
    db = firestore.Client(project=PROJECT_ID, database=DATABASE)
    return storage_client, db


def geocode_from_stored_coords(data: dict) -> tuple:
    """Return (location_str, lat, lon) using lat/lon already in the record."""
    lat = data.get("latitude")
    lon = data.get("longitude")
    if lat is None or lon is None:
        return None, None, None
    location = reverse_geocode(float(lat), float(lon))
    return location, float(lat), float(lon)


def geocode_from_gcs(bucket, filename: str) -> tuple:
    """
    Download only the first 64 KB of the original from GCS and extract GPS EXIF.
    Returns (location_str, lat, lon) or (None, None, None) if no GPS found.
    """
    blob = bucket.blob(f"originals/{filename}")
    if not blob.exists():
        return None, None, None
    buf = io.BytesIO()
    blob.download_to_file(buf, start=0, end=65535, timeout=300)
    buf.seek(0)
    location_tag, lat, lon = get_location_tag(buf)
    if location_tag is None:
        return None, None, None
    location = reverse_geocode(lat, lon)
    return location, lat, lon


def backfill(force: bool = False, dry_run: bool = False, limit: int | None = None):
    storage_client, db = build_clients()
    bucket = storage_client.bucket(BUCKET_NAME)

    docs = list(db.collection("images").stream())
    print(f"Total records in Firestore: {len(docs)}")

    if force:
        candidates = docs
    else:
        candidates = [d for d in docs if not d.to_dict().get("location_backfilled")]

    # Only process phone photos — skip dedicated cameras, scanners, unknowns
    to_process = [d for d in candidates if is_phone(d.to_dict())]
    skipped_non_phone = len(candidates) - len(to_process)

    if limit:
        to_process = to_process[:limit]

    suffix = " (--force)" if force else ""
    suffix += " (--dry-run)" if dry_run else ""
    print(f"Phone photos to process : {len(to_process)}{suffix}")
    print(f"Non-phone skipped       : {skipped_non_phone}")

    if not to_process:
        print("Nothing to do.")
        return

    updated = skipped_no_gps = skipped_error = 0

    for doc in tqdm(to_process, unit="photo"):
        data = doc.to_dict()
        filename = data.get("name", "")

        try:
            # Strategy 1: use stored coords (no network call)
            location, lat, lon = geocode_from_stored_coords(data)

            # Strategy 2: download first 64 KB from GCS and extract EXIF
            if location is None:
                location, lat, lon = geocode_from_gcs(bucket, filename)

            if location is None:
                tqdm.write(f"  ⏭  {filename}: no GPS data")
                skipped_no_gps += 1
                continue

            location_tag = location.lower()
            existing_tags: list = data.get("tags") or []
            new_tags = list(set(existing_tags + [location_tag]))

            update_payload = {
                "location": location,
                "latitude": lat,
                "longitude": lon,
                "tags": new_tags,
                "location_backfilled": True,
            }

            if dry_run:
                tqdm.write(f"  🔍 {filename}: would set → {location}")
            else:
                doc.reference.update(update_payload)
                tqdm.write(f"  ✅ {filename}: {location}")

            updated += 1

        except Exception as e:
            tqdm.write(f"  ❌ {filename}: error — {e}")
            skipped_error += 1

    action = "Would update" if dry_run else "Updated"
    print(f"\n✅ Done. {action}: {updated} | No GPS in EXIF: {skipped_no_gps} | Errors: {skipped_error}")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill GPS location tags for phone photos in Firestore."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-process records that already have location_backfilled=True."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be updated without writing to Firestore."
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Process at most N records (useful for testing)."
    )
    args = parser.parse_args()
    backfill(force=args.force, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
