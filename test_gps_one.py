"""
Quick test: find the first Firestore record with a known camera, download
its first 64 KB from GCS, and attempt GPS extraction + reverse geocoding.

Usage:
  python test_gps_one.py
  python test_gps_one.py --all        # sample 5 records regardless of camera
  python test_gps_one.py --name <filename>  # test a specific file by name
"""

import argparse
import io
import json

from google.cloud import firestore, storage

from gps_location import get_location_tag

PROJECT_ID  = "life-begins-at-40"
BUCKET_NAME = "lb40-bucket"
DATABASE    = "media-metadata"
KEY_PATH    = "life-begins-at-40-a0cf724dc4fe.json"


def test_record(bucket, data: dict):
    filename = data.get("name", "")
    print(f"\nFile    : {filename}")
    print(f"Camera  : {data.get('make', '?')} {data.get('camera', '?')}")
    print(f"Date    : {data.get('date_taken', '?')}")
    print(f"Stored lat/lon: {data.get('latitude')}, {data.get('longitude')}")

    blob = bucket.blob(f"originals/{filename}")
    if not blob.exists():
        print("❌ Blob not found in GCS")
        return

    print("Downloading first 64 KB from GCS...")
    buf = io.BytesIO()
    blob.download_to_file(buf, start=0, end=65535, timeout=60)
    buf.seek(0)

    tag, lat, lon = get_location_tag(buf)
    if tag:
        print(f"✅ GPS found  : {lat:.6f}, {lon:.6f}")
        print(f"   Location   : {tag}")
    else:
        print("❌ No GPS data in EXIF")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true",
                        help="Sample 5 records regardless of camera")
    parser.add_argument("--name", default=None,
                        help="Test a specific file by its Firestore document name")
    args = parser.parse_args()

    db = firestore.Client.from_service_account_json(
        KEY_PATH, project=PROJECT_ID, database=DATABASE
    )
    storage_client = storage.Client.from_service_account_json(KEY_PATH)
    bucket = storage_client.bucket(BUCKET_NAME)

    if args.name:
        doc = db.collection("images").document(args.name).get()
        if not doc.exists:
            print(f"No Firestore record found for: {args.name}")
            return
        test_record(bucket, doc.to_dict())
        return

    # Find records to test
    records = []
    for doc in db.collection("images").stream():
        data = doc.to_dict()
        if args.all or data.get("camera", "Unknown") != "Unknown":
            records.append(data)
        if len(records) >= 5:
            break

    if not records:
        print("No matching records found in Firestore.")
        return

    for data in records:
        test_record(bucket, data)


if __name__ == "__main__":
    main()
