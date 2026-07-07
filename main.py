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

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

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
        docs = db.collection("images").stream()
        all_tags = set()
        total = 0
        total_size = 0
        for doc in docs:
            data = doc.to_dict()
            all_tags.update(data.get('tags', []))
            total += 1
            total_size += data.get('file_size', 0)
        return render_template("gallery.html", all_tags=sorted(all_tags),
                               total=total, total_size=total_size)
    except Exception as e:
        return f"Error: {e}", 500

@app.route("/photo/<filename>/tags", methods=["POST"])
@login_required
def update_tags(filename):
    tags = [t.strip().lower() for t in request.form.get("tags", "").split(",") if t.strip()]
    db.collection("images").document(filename).update({"tags": tags})
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
    return redirect(url_for("index"))


@app.route("/photos/location/bulk", methods=["POST"])
@login_required
def bulk_location():
    filenames = request.form.getlist("filenames")
    if not filenames:
        flash("No photos selected.")
        return redirect(url_for("index"))

    updated, skipped, errors = 0, 0, 0
    bucket = storage_client.bucket(BUCKET_NAME)

    for filename in filenames:
        try:
            doc_ref = db.collection("images").document(filename)
            doc = doc_ref.get()
            if not doc.exists:
                errors += 1
                continue
            data = doc.to_dict()

            # Skip if location already set
            if data.get("location"):
                skipped += 1
                continue

            location, lat, lon = None, None, None

            # Strategy 1: use stored coords — no download
            stored_lat = data.get("latitude")
            stored_lon = data.get("longitude")
            if stored_lat is not None and stored_lon is not None:
                from gps_location import reverse_geocode
                location = reverse_geocode(float(stored_lat), float(stored_lon))
                lat, lon = float(stored_lat), float(stored_lon)

            # Strategy 2: download first 64 KB from GCS
            if location is None:
                blob = bucket.blob(f"originals/{filename}")
                if blob.exists():
                    buf = io.BytesIO()
                    blob.download_to_file(buf, start=0, end=65535, timeout=30)
                    buf.seek(0)
                    location_tag, lat, lon = get_location_tag(buf)
                    if location_tag:
                        from gps_location import reverse_geocode
                        location = reverse_geocode(lat, lon)

            if not location:
                skipped += 1
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

        except Exception:
            errors += 1

    msg = f"Location tagged: {updated} photo(s)."
    if skipped:
        msg += f" {skipped} had no GPS data or already tagged."
    if errors:
        msg += f" {errors} error(s)."
    flash(msg)
    return redirect(url_for("index"))

@app.route("/tag/<tag_name>")
def tag(tag_name):
    try:
        all_docs = db.collection("images").stream()
        all_tags = set()
        for doc in all_docs:
            all_tags.update(doc.to_dict().get('tags', []))

        docs = db.collection("images").where("tags", "array_contains", tag_name.lower()).stream()
        items = []
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
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 50
        total = len(items)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        items = items[(page - 1) * per_page : page * per_page]
        return render_template("gallery.html", items=items, active_tag=tag_name.lower(),
                               all_tags=sorted(all_tags), page=page, total_pages=total_pages)
    except Exception as e:
        return f"Error: {e}", 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)