# BackupMyMedia

A personal photo backup and gallery web app backed entirely by Google Cloud. Photos are stored in Google Cloud Storage (GCS) and metadata in Firestore, with a Flask web frontend deployed on Cloud Run.

**Live URL:** https://gallery-service-89549779745.us-central1.run.app

---

## Architecture

```
Browser → Flask (Cloud Run)
              ├── Google Cloud Storage (GCS)  — originals/ + thumbnails/
              ├── Firestore (media-metadata)   — image records + tags
              └── Google OAuth2               — login gate
```

| Resource | Value |
|---|---|
| GCP Project | `life-begins-at-40` |
| GCS Bucket | `lb40-bucket` |
| Firestore Database | `media-metadata` |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | Flask 3.0 |
| Auth | Google OAuth2 via Authlib |
| Object storage | Google Cloud Storage |
| Database | Firestore (Native mode) |
| Image processing | Pillow |
| Reverse geocoding | Nominatim (OpenStreetMap) |
| Server | Gunicorn |
| Hosting | Google Cloud Run |
| Container | Docker (Python 3.11-slim) |

---

## Features

- **Google OAuth2 login** — access restricted to an allowlist of authorised email addresses
- **Photo upload** — multi-file upload via the web UI or CLI batch script; deduplicates by MD5 hash
- **EXIF extraction** — reads camera make, model, and date taken from image metadata
- **GPS location tagging** — extracts GPS coordinates from EXIF, reverse-geocodes to a city/country string via Nominatim, and applies it as a tag automatically
- **Auto-tagging** — derives tags from the year taken and camera model at upload time
- **Thumbnail generation** — creates 200×200 JPEG thumbnails stored alongside originals in GCS
- **Tag management** — per-photo tag editing and bulk tagging across multiple selected photos
- **Tag-based browsing** — sticky sidebar with all tags; click any tag to filter the gallery
- **Pagination** — tag views paginate at 50 photos per page

---

## Components

### `main.py` — Flask web app
The core application. Handles Google OAuth2 login, the photo gallery UI, web-based upload, auto-tagging, and tag management (per-photo and bulk).

### `upload.py` — CLI batch uploader
Standalone script for uploading a single file or a full directory from the local filesystem. Applies the same deduplication, EXIF extraction, thumbnail generation, and tagging logic as the web app. Shows a progress bar via `tqdm`.

```bash
python upload.py --dir /path/to/photos
python upload.py --dir /path/to/single.jpg
```

### `gps_location.py` — GPS location helper
Shared module used by all upload paths. Extracts GPS coordinates from image EXIF, reverse-geocodes them to a human-readable location string using Nominatim (free, no API key required), and returns a lowercased tag (e.g. `"san francisco, united states"`). Includes an in-process cache and a 1 req/sec rate limit to comply with Nominatim's usage policy.

### `backfill_location.py` — Location backfill script
Processes existing Firestore records that predate GPS tagging. For each record it first tries to use already-stored `latitude`/`longitude` fields; if absent, it downloads the original from GCS and extracts GPS from the file. Sets a `location_backfilled` flag so the script is safe to re-run.

```bash
python backfill_location.py --dry-run   # preview without writing
python backfill_location.py --limit 10  # test on a small batch
python backfill_location.py             # run for real
python backfill_location.py --force     # re-process already-backfilled records
```

> Run from Cloud Shell in `us-central1` to avoid GCS egress charges on the download fallback.

### `backfill_hashes.py` — MD5 hash backfill script
One-off migration script that backfills `content_hash` on records that predate the deduplication feature. Downloads originals from GCS, computes MD5, and writes back to Firestore.

### `takeout_import.py` — Google Takeout importer
Downloads a Google Takeout zip via `wget` (with cookies for authentication), extracts it, pipes the contents through `upload.py`, then cleans up temporary files.

```bash
python takeout_import.py "<takeout-url>" --cookies ~/Downloads/cookies.txt
```

---

## Firestore Record Schema

Each document in the `images` collection stores:

| Field | Type | Description |
|---|---|---|
| `name` | string | Original filename |
| `orig_url` | string | GCS URL of the full-resolution original |
| `thumb_url` | string | GCS URL of the 200×200 thumbnail |
| `camera` | string | Camera model from EXIF |
| `make` | string | Camera make from EXIF |
| `date_taken` | string | DateTimeOriginal from EXIF |
| `uploaded_at` | timestamp | Server timestamp at upload time |
| `content_hash` | string | MD5 of the file bytes (used for dedup) |
| `file_size` | number | File size in bytes |
| `tags` | array | All tags (auto-generated + manual) |
| `auto_tagged` | bool | Whether auto-tagging has been applied |
| `location` | string | Human-readable location (e.g. "San Francisco, United States") |
| `latitude` | number | Decimal-degree latitude from GPS EXIF |
| `longitude` | number | Decimal-degree longitude from GPS EXIF |
| `location_backfilled` | bool | Set by the backfill script |

---

## Run Locally

```bash
source .venv/bin/activate
python main.py
```

Open http://localhost:8080

---

## Deploy to Google Cloud

### First-time setup

```bash
# Enable required APIs
gcloud services enable secretmanager.googleapis.com --project=life-begins-at-40

# Upload service account key to Secret Manager
gcloud secrets create gcp-service-account-key \
  --data-file=life-begins-at-40-a0cf724dc4fe.json \
  --project=life-begins-at-40

# Grant Cloud Run access to the secret
gcloud secrets add-iam-policy-binding gcp-service-account-key \
  --member="serviceAccount:89549779745-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=life-begins-at-40
```

### Build and deploy

```bash
# Build image
gcloud builds submit \
  --tag us-central1-docker.pkg.dev/life-begins-at-40/cloud-run-source-deploy/gallery-service \
  --project=life-begins-at-40

# Deploy
gcloud run deploy gallery-service \
  --image us-central1-docker.pkg.dev/life-begins-at-40/cloud-run-source-deploy/gallery-service \
  --region us-central1 \
  --project life-begins-at-40 \
  --update-secrets=/secrets/key.json=gcp-service-account-key:latest \
  --set-env-vars KEY_PATH=/secrets/key.json,GOOGLE_CLIENT_ID=<your-client-id>,GOOGLE_CLIENT_SECRET=<your-client-secret>,SECRET_KEY=<random-secret-string>
```
