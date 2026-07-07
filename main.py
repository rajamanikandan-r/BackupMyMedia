import hashlib
import io
import os
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from google.cloud import storage, firestore
from PIL import Image
from PIL.ExifTags import TAGS
from gps_location import get_location_tag

load_dotenv()

from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# --- CONFIGURATION ---
BUCKET_NAME = "lb40-bucket"
PROJECT_ID = "life-begins-at-40"
KEY_PATH = os.environ.get("KEY_PATH", "life-begins-at-40-a0cf724dc4fe.json")

ALLOWED_EMAILS = {
    "rmnforever@gmail.com",
    "sumitradevi@gmail.com",
    "sumitradevi.usa@gmail.com",
}

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email"},
)

storage_client = storage.Client.from_service_account_json(KEY_PATH)
db = firestore.Client.from_service_account_json(
    KEY_PATH, project=PROJECT_ID, database="media-metadata"
)

# --- IN-MEMORY CACHE ---
# Stores tag groups + stats so the index route doesn't scan all docs on every request.
# Invalidated by calling _invalidate_cache() after any write operation.
_cache: dict = {}

def _invalidate_cache():
    _cache.clear()

def _get_cached_stats():
    """
    Return (tag_groups, total, total_size) from cache, or rebuild by scanning
    all Firestore docs if the cache is empty.
    Always repopulates flask.g with the exclusive category sets so that
    tag_toggle_url works correctly on cache hits.
    """
    from flask import g

    if not _cache:
        all_docs = list(db.collection("images").stream())
        total = len(all_docs)
        total_size = 0
        for doc in all_docs:
            total_size += doc.to_dict().get("file_size", 0)

        tag_groups = categorize_tags(all_docs)  # also sets g._tag_* for this request

        _cache["tag_groups"]      = tag_groups
        _cache["total"]           = total
        _cache["total_size"]      = total_size
        # Persist the raw sets so cache hits can restore them
        _cache["_tag_locations"]  = g._tag_locations
        _cache["_tag_years"]      = g._tag_years
        _cache["_tag_cameras"]    = g._tag_cameras
    else:
        # Restore category sets onto g so tag_toggle_url works on cache hits
        g._tag_locations = _cache["_tag_locations"]
        g._tag_years     = _cache["_tag_years"]
        g._tag_cameras   = _cache["_tag_cameras"]

    return _cache["tag_groups"], _cache["total"], _cache["total_size"]

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

@app.template_global()
def tag_toggle_url(tag):
    """
    Return a URL that toggles a tag in the active filter.
    - If the tag is already active, remove it.
    - If adding a tag from location/year/camera category, replace any existing
      tag from the same category (only one per category makes sense).
    - User tags stack freely.
    """
    active = [t.lower() for t in request.args.getlist("tags")]
    tag = tag.lower()

    if tag in active:
        # Remove it
        updated = [t for t in active if t != tag]
    else:
        # Determine which category this tag belongs to using the current tag_groups
        # We re-derive category sets from the template context via a quick check
        # on the tag_groups that were computed for this request — but since
        # tag_toggle_url is called from the template, we derive it inline.
        from flask import g
        exclusive_groups = [
            getattr(g, "_tag_locations", set()),
            getattr(g, "_tag_years",     set()),
            getattr(g, "_tag_cameras",   set()),
        ]
        updated = list(active)
        for group in exclusive_groups:
            if tag in group:
                updated = [t for t in updated if t not in group]
                break
        updated.append(tag)

    if not updated:
        return "/"
    return "/?" + "&".join(f"tags={t}" for t in updated)


def categorize_tags(all_docs):
    """
    Split all tags into four groups: location, year, camera, user-created.
    Returns a dict with keys: locations, years, cameras, user.
    Also stores the exclusive-category sets on flask.g for tag_toggle_url.
    """
    import re
    from flask import g
    locations, years, cameras, user = set(), set(), set(), set()
    known_locations = set()
    known_cameras = set()

    for doc in all_docs:
        data = doc.to_dict()
        loc = (data.get("location") or "").lower().strip()
        cam = (data.get("camera") or "").lower().strip()
        if loc:
            known_locations.add(loc)
        if cam and cam not in ("unknown", "--", ""):
            known_cameras.add(cam)

    for doc in all_docs:
        data = doc.to_dict()
        for tag in data.get("tags", []):
            tag = tag.strip()
            if not tag:
                continue
            if re.match(r"^\d{4}$", tag):
                years.add(tag)
            elif tag in known_locations:
                locations.add(tag)
            elif tag in known_cameras:
                cameras.add(tag)
            else:
                user.add(tag)

    # Store for tag_toggle_url to use during this request
    g._tag_locations = locations
    g._tag_years     = years
    g._tag_cameras   = cameras

    return {
        "locations": sorted(locations),
        "years":     sorted(years, reverse=True),
        "cameras":   sorted(cameras),
        "user":      sorted(user),
    }

@app.route("/login")
def login():
    if session.get("user"):
        return redirect(url_for("index"))
    return render_template("login.html")

@app.route("/login/google")
def login_google():
    redirect_uri = url_for("auth_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    user_info = token.get("userinfo")
    email = user_info.get("email", "").lower()
    if email not in ALLOWED_EMAILS:
        return render_template("login.html", error="Access denied. Your account is not authorised.")
    session["user"] = email
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

def get_exif_data(file_stream):
    try:
        img = Image.open(file_stream)
        exif_data = {}
        info = img._getexif()
        if info:
            for tag, value in info.items():
                decoded = TAGS.get(tag, tag)
                if decoded in ['Make', 'Model', 'DateTimeOriginal']:
                    exif_data[decoded] = str(value)
        return exif_data
    except Exception:
        return {}

def auto_tags_from_record(data):
    tags = []
    date_str = data.get('date_taken', '')
    if date_str and len(date_str) >= 4 and date_str[:4].isdigit():
        tags.append(date_str[:4])
    camera = data.get('camera', '')
    if camera and camera != 'Unknown':
        tags.append(camera.strip().lower())
    return tags

def create_thumbnail(file_stream):
    img = Image.open(file_stream)
    img.thumbnail((200, 200))
    thumb_io = io.BytesIO()
    img.save(thumb_io, "JPEG")
    thumb_io.seek(0)
    return thumb_io

@app.route("/")
@login_required
def index():
    try:
        active_tags = [t.lower() for t in request.args.getlist("tags") if t.strip()]

        # Single cache read instead of streaming all 13K docs
        tag_groups, total, total_size = _get_cached_stats()
        all_tags = (
            tag_groups["locations"] + tag_groups["years"] +
            tag_groups["cameras"]   + tag_groups["user"]
        )

        items = []
        if active_tags:
            if len(active_tags) == 1:
                docs = db.collection("images").where(
                    "tags", "array_contains", active_tags[0]
                ).stream()
                for doc in docs:
                    data = doc.to_dict()
                    items.append({
                        'name': data.get('name', 'Unknown'),
                        'camera': data.get('camera', 'Unknown'),
                        'make': data.get('make', 'Unknown'),
                        'date_taken': data.get('date_taken', 'Unknown'),
                        'thumb_url': data.get('thumb_url'),
                        'orig_url': data.get('orig_url'),
                        'tags': data.get('tags', [])
                    })
            else:
                # AND: fetch by first tag, filter rest client-side
                docs = db.collection("images").where(
                    "tags", "array_contains", active_tags[0]
                ).stream()
                rest = set(active_tags[1:])
                for doc in docs:
                    data = doc.to_dict()
                    if rest.issubset(set(data.get('tags', []))):
                        items.append({
                            'name': data.get('name', 'Unknown'),
                            'camera': data.get('camera', 'Unknown'),
                            'make': data.get('make', 'Unknown'),
                            'date_taken': data.get('date_taken', 'Unknown'),
                            'thumb_url': data.get('thumb_url'),
                            'orig_url': data.get('orig_url'),
                            'tags': data.get('tags', [])
                        })

        # Pagination
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 50
        filtered_total = len(items)
        total_pages = max(1, (filtered_total + per_page - 1) // per_page)
        page = min(page, total_pages)
        paged_items = items[(page - 1) * per_page : page * per_page]

        return render_template("gallery.html",
                               all_tags=sorted(all_tags),
                               tag_groups=tag_groups,
                               active_tags=active_tags,
                               items=paged_items if active_tags else [],
                               total=total,
                               total_size=total_size,
                               page=page,
                               total_pages=total_pages if active_tags else 1)
    except Exception as e:
        return f"Error: {e}", 500

@app.route("/photo/<filename>/tags", methods=["POST"])
@login_required
def update_tags(filename):
    tags = [t.strip().lower() for t in request.form.get("tags", "").split(",") if t.strip()]
    db.collection("images").document(filename).update({"tags": tags})
    _invalidate_cache()
    return redirect(url_for("index"))

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    files = request.files.getlist("photos")
    tags = [t.strip().lower() for t in request.form.get("tags", "").split(",") if t.strip()]
    bucket = storage_client.bucket(BUCKET_NAME)
    uploaded, skipped = 0, 0
    for file in files:
        if not file.filename:
            continue
        file.seek(0)
        file_bytes = file.read()
        file_size = len(file_bytes)
        content_hash = hashlib.md5(file_bytes).hexdigest()
        existing = db.collection("images").where("content_hash", "==", content_hash).limit(1).stream()
        if any(True for _ in existing):
            skipped += 1
            continue

        file.seek(0)
        metadata = get_exif_data(file)

        # Extract GPS location from EXIF
        file.seek(0)
        location_tag, lat, lon = get_location_tag(file)

        file.seek(0)
        orig_blob = bucket.blob(f"originals/{file.filename}")
        orig_blob.upload_from_file(file, content_type=file.content_type)

        file.seek(0)
        thumb_io = create_thumbnail(file)
        thumb_blob = bucket.blob(f"thumbnails/{file.filename}")
        thumb_blob.upload_from_file(thumb_io, content_type="image/jpeg")

        record = {
            "name": file.filename,
            "orig_url": orig_blob.public_url,
            "thumb_url": thumb_blob.public_url,
            "camera": metadata.get('Model', 'Unknown'),
            "make": metadata.get('Make', 'Unknown'),
            "date_taken": metadata.get('DateTimeOriginal', 'Unknown'),
            "uploaded_at": firestore.SERVER_TIMESTAMP,
            "content_hash": content_hash,
            "file_size": file_size,
            "auto_tagged": False,
            "tags": tags,
            "location": location_tag,
            "latitude": lat,
            "longitude": lon,
        }
        auto_tags = auto_tags_from_record(record)
        if location_tag:
            auto_tags.append(location_tag)
        record["tags"] = list(set(tags + auto_tags))
        record["auto_tagged"] = True
        db.collection("images").document(file.filename).set(record)
        uploaded += 1

    msg = f"{uploaded} photo(s) uploaded."
    if skipped:
        msg += f" {skipped} skipped (duplicate)."
    if uploaded:
        _invalidate_cache()
    flash(msg)
    return redirect(url_for("index"))

@app.route("/autotag")
@login_required
def autotag():
    force = request.args.get("force", "0") == "1"
    docs = list(db.collection("images").stream())
    batch = db.batch()
    count = 0
    for doc in docs:
        data = doc.to_dict()
        if not force and data.get("auto_tagged"):
            continue
        new_tags = auto_tags_from_record(data)
        if not new_tags:
            continue
        merged = list(set(data.get("tags", []) + new_tags))
        batch.update(doc.reference, {"tags": merged, "auto_tagged": True})
        count += 1
        if count % 500 == 0:  # Firestore batch limit
            batch.commit()
            batch = db.batch()
    if count % 500 != 0:
        batch.commit()
    _invalidate_cache()
    return redirect(url_for("index"))

@app.route("/photos/tags/bulk", methods=["POST"])
@login_required
def bulk_tag():
    filenames = request.form.getlist("filenames")
    tag = request.form.get("tag", "").strip().lower()
    if tag and filenames:
        batch = db.batch()
        for filename in filenames:
            ref = db.collection("images").document(filename)
            batch.update(ref, {"tags": firestore.ArrayUnion([tag])})
        batch.commit()
        _invalidate_cache()
    return redirect(url_for("index"))


@app.route("/photos/location/bulk", methods=["POST"])
@login_required
def bulk_location():
    filenames = request.form.getlist("filenames")
    if not filenames:
        flash("No photos selected.")
        return redirect(url_for("index"))

    updated, skipped_no_gps, skipped_already, errors = 0, 0, 0, 0
    error_details = []
    bucket = storage_client.bucket(BUCKET_NAME)

    for filename in filenames:
        try:
            doc_ref = db.collection("images").document(filename)
            doc = doc_ref.get()
            if not doc.exists:
                errors += 1
                error_details.append(f"{filename}: not found")
                continue
            data = doc.to_dict()

            # Skip if location already tagged
            if data.get("location"):
                skipped_already += 1
                continue

            location, lat, lon = None, None, None

            # Strategy 1: use stored coords — no GCS download needed
            stored_lat = data.get("latitude")
            stored_lon = data.get("longitude")
            if stored_lat is not None and stored_lon is not None:
                from gps_location import reverse_geocode
                lat, lon = float(stored_lat), float(stored_lon)
                location = reverse_geocode(lat, lon)

            # Strategy 2: download first 64 KB from GCS and extract EXIF
            if location is None:
                blob = bucket.blob(f"originals/{filename}")
                if blob.exists():
                    buf = io.BytesIO()
                    blob.download_to_file(buf, start=0, end=524287, timeout=120)
                    buf.seek(0)
                    location_tag, lat, lon = get_location_tag(buf)
                    if location_tag:
                        from gps_location import reverse_geocode
                        location = reverse_geocode(lat, lon)

            if not location:
                skipped_no_gps += 1
                continue

            location_tag = location.lower()
            existing_tags = data.get("tags") or []
            new_tags = list(set(existing_tags + [location_tag]))
            doc_ref.update({
                "location": location,
                "latitude": lat,
                "longitude": lon,
                "tags": new_tags,
                "location_backfilled": True,
            })
            updated += 1

        except Exception as e:
            errors += 1
            error_details.append(f"{filename}: {e}")

    parts = []
    if updated:
        parts.append(f"{updated} photo(s) location tagged")
    if skipped_already:
        parts.append(f"{skipped_already} already had a location")
    if skipped_no_gps:
        parts.append(f"{skipped_no_gps} had no GPS data")
    if errors:
        parts.append(f"{errors} error(s): {'; '.join(error_details[:3])}")
    flash(". ".join(parts) + "." if parts else "Nothing to update.")
    _invalidate_cache()
    return redirect(url_for("index"))

def _docs_matching_tags(filter_tags):
    """Return all Firestore docs whose tags contain ALL of filter_tags."""
    if not filter_tags:
        return []
    if len(filter_tags) == 1:
        return list(db.collection("images").where(
            "tags", "array_contains", filter_tags[0]
        ).stream())
    # AND: fetch by first tag then filter client-side
    docs = db.collection("images").where(
        "tags", "array_contains", filter_tags[0]
    ).stream()
    rest = set(filter_tags[1:])
    return [d for d in docs if rest.issubset(set(d.to_dict().get("tags", [])))]


@app.route("/photos/tags/bulk-all", methods=["POST"])
@login_required
def bulk_tag_all():
    filter_tags = [t.lower() for t in request.form.getlist("filter_tags") if t.strip()]
    tag = request.form.get("tag", "").strip().lower()
    if not tag or not filter_tags:
        flash("Missing tag or filter.")
        return redirect(url_for("index"))

    docs = _docs_matching_tags(filter_tags)
    batch = db.batch()
    count = 0
    for doc in docs:
        batch.update(doc.reference, {"tags": firestore.ArrayUnion([tag])})
        count += 1
        if count % 500 == 0:
            batch.commit()
            batch = db.batch()
    if count % 500 != 0:
        batch.commit()

    flash(f"Tag '{tag}' applied to {count} photo(s).")
    _invalidate_cache()
    return redirect("/?" + "&".join(f"tags={t}" for t in filter_tags))


@app.route("/photos/location/bulk-all", methods=["POST"])
@login_required
def bulk_location_all():
    filter_tags = [t.lower() for t in request.form.getlist("filter_tags") if t.strip()]
    if not filter_tags:
        flash("No filter tags provided.")
        return redirect(url_for("index"))

    docs = _docs_matching_tags(filter_tags)
    bucket = storage_client.bucket(BUCKET_NAME)
    updated, skipped_no_gps, skipped_already, errors = 0, 0, 0, 0
    error_details = []

    for doc in docs:
        data = doc.to_dict()
        filename = data.get("name", "")
        try:
            if data.get("location"):
                skipped_already += 1
                continue

            location, lat, lon = None, None, None
            stored_lat = data.get("latitude")
            stored_lon = data.get("longitude")
            if stored_lat is not None and stored_lon is not None:
                from gps_location import reverse_geocode
                lat, lon = float(stored_lat), float(stored_lon)
                location = reverse_geocode(lat, lon)

            if location is None:
                blob = bucket.blob(f"originals/{filename}")
                if blob.exists():
                    buf = io.BytesIO()
                    blob.download_to_file(buf, start=0, end=524287, timeout=120)
                    buf.seek(0)
                    location_tag, lat, lon = get_location_tag(buf)
                    if location_tag:
                        from gps_location import reverse_geocode
                        location = reverse_geocode(lat, lon)

            if not location:
                skipped_no_gps += 1
                continue

            location_tag = location.lower()
            existing_tags = data.get("tags") or []
            new_tags = list(set(existing_tags + [location_tag]))
            doc.reference.update({
                "location": location,
                "latitude": lat,
                "longitude": lon,
                "tags": new_tags,
                "location_backfilled": True,
            })
            updated += 1
        except Exception as e:
            errors += 1
            error_details.append(f"{filename}: {e}")

    parts = []
    if updated:
        parts.append(f"{updated} photo(s) location tagged")
    if skipped_already:
        parts.append(f"{skipped_already} already had a location")
    if skipped_no_gps:
        parts.append(f"{skipped_no_gps} had no GPS data")
    if errors:
        parts.append(f"{errors} error(s): {'; '.join(error_details[:3])}")
    flash(". ".join(parts) + "." if parts else "Nothing to update.")
    _invalidate_cache()
    return redirect("/?" + "&".join(f"tags={t}" for t in filter_tags))


@app.route("/tag/<tag_name>")
def tag(tag_name):
    # Redirect old single-tag URLs into the new multi-tag filter
    active = request.args.getlist("tags") or []
    if tag_name.lower() not in active:
        active = active + [tag_name.lower()]
    return redirect(url_for("index") + "?" + "&".join(f"tags={t}" for t in active))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)