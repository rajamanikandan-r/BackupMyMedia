"""
Backfill GPS-based location tags for existing Firestore image records.

Strategy (per record, in order):
  1. If the record already has latitude + longitude stored, reverse-geocode
     directly — no download needed.
  2. Otherwise, download the original file from GCS, extract GPS EXIF data,
     then reverse-geocode.
  3. If no GPS data can be found by either method, the record is skipped.

For each matched record the script:
  - Sets the `location` field  (e.g. "San Francisco, United States")
  - Sets the `latitude` / `longitude` fields
  - Appends the lowercased location tag to the `tags` array (deduped)
  - Sets `location_backfilled = True` so the script is safe to re-run
    (use --force to re-process already-backfilled records)

Run from Cloud Shell (us-central1) to avoid GCS egress charges.

Usage:
  python backfill_location.py [--force] [--dry-run] [--limit N]

Options:
  --force    Re-process records that already have a location tag.
  --dry-run  Print what would be updated without writing to Firestore.
  --limit N  Process at most N records (useful for testing).
"""

import argparse
import io
import sys

from google.cloud import firestore, storage
from tqdm import tqdm

from gps_location import extract_gps_coords, get_location_tag, reverse_geocode

# ── Configuration ──────────────────────────────────────────────────────────────
PROJECT_ID = "life-begins-at-40"
BUCKET_NAME = "lb40-bucket"
DATABASE    = "media-metadata"
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
    """Download the original from GCS, extract GPS EXIF, reverse-geocode."""
    blob = bucket.blob(f"originals/{filename}")
    if not blob.exists():
        return None, None, None
    buf = io.BytesIO()
    blob.download_to_file(buf)
    buf.seek(0)
    location_tag, lat, lon = get_location_tag(buf)
    if location_tag is None:
        return None, None, None
    # get_location_tag already lowercases; we want the original casing for the
    # `location` field, so reverse-geocode again (hits cache, no extra request).
    location = reverse_geocode(lat, lon)
    return location, lat, lon


def backfill(force: bool = False, dry_run: bool = False, limit: int | None = None):
    storage_client, db = build_clients()
    bucket = storage_client.bucket(BUCKET_NAME)

    docs = list(db.collection("images").stream())
    print(f"Total records in Firestore: {len(docs)}")

    if not force:
        to_process = [d for d in docs if not d.to_dict().get("location_backfilled")]
    else:
        to_process = docs

    if limit:
        to_process = to_process[:limit]

    print(f"Records to process: {len(to_process)}"
          + (" (--force mode)" if force else "")
          + (" (--dry-run)" if dry_run else ""))

    if not to_process:
        print("Nothing to do.")
        return

    updated = skipped_no_gps = skipped_error = 0

    for doc in tqdm(to_process, unit="photo"):
        data = doc.to_dict()
        filename = data.get("name", "")

        try:
            # Strategy 1: use stored coords
            location, lat, lon = geocode_from_stored_coords(data)

            # Strategy 2: download from GCS and extract EXIF
            if location is None:
                location, lat, lon = geocode_from_gcs(bucket, filename)

            if location is None:
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
                tqdm.write(f"[dry-run] {filename}: would set location='{location}', "
                           f"tag='{location_tag}'")
            else:
                doc.reference.update(update_payload)

            updated += 1

        except Exception as e:
            tqdm.write(f"\nError processing {filename}: {e}")
            skipped_error += 1

    action = "Would update" if dry_run else "Updated"
    print(f"\n✅ Done. {action}: {updated} | No GPS: {skipped_no_gps} | Errors: {skipped_error}")


def main():
    parser = argparse.ArgumentParser(
        description="Backfill GPS location tags for existing Firestore image records."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-process records that already have a location tag."
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
