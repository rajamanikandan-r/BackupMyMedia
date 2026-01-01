import io
from flask import Flask, render_template, request, redirect, url_for
from google.cloud import storage, firestore
from PIL import Image
from PIL.ExifTags import TAGS

app = Flask(__name__)

# --- CONFIGURATION ---
BUCKET_NAME = "lb40-bucket"
PROJECT_ID = "life-begins-at-40"
KEY_PATH = "life-begins-at-40-a0cf724dc4fe.json"

storage_client = storage.Client.from_service_account_json(KEY_PATH)
db = firestore.Client.from_service_account_json(
    KEY_PATH, project=PROJECT_ID, database="media-metadata"
)

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

def create_thumbnail(file_stream):
    img = Image.open(file_stream)
    img.thumbnail((200, 200))
    thumb_io = io.BytesIO()
    img.save(thumb_io, "JPEG")
    thumb_io.seek(0)
    return thumb_io

@app.route("/")
def index():
    try:
        docs = db.collection("images").stream()
        items = []
        for doc in docs:
            data = doc.to_dict()
            items.append({
                'name': data.get('name', 'Unknown'),
                'camera': data.get('camera', 'Unknown'),
                'make': data.get('make', 'Unknown'),
                'date_taken': data.get('date_taken', 'Unknown'),
                'thumb_url': data.get('thumb_url'),
                'orig_url': data.get('orig_url')
            })
        return render_template("gallery.html", items=items)
    except Exception as e:
        return f"Error: {e}", 500

@app.route("/upload", methods=["POST"])
def upload():
    files = request.files.getlist("photos")
    bucket = storage_client.bucket(BUCKET_NAME)
    for file in files:
        if file.filename:
            # 1. Extract Metadata
            file.seek(0)
            metadata = get_exif_data(file)
            
            # 2. Upload Original
            file.seek(0)
            orig_blob = bucket.blob(f"originals/{file.filename}")
            orig_blob.upload_from_file(file, content_type=file.content_type)
            
            # 3. Upload Thumbnail
            file.seek(0)
            thumb_io = create_thumbnail(file)
            thumb_blob = bucket.blob(f"thumbnails/{file.filename}")
            thumb_blob.upload_from_file(thumb_io, content_type="image/jpeg")
            
            # 4. Save to Firestore (FLAT FORMAT)
            db.collection("images").document(file.filename).set({
                "name": file.filename,
                "orig_url": orig_blob.public_url,
                "thumb_url": thumb_blob.public_url,
                "camera": metadata.get('Model', 'Unknown'),
                "make": metadata.get('Make', 'Unknown'),
                "date_taken": metadata.get('DateTimeOriginal', 'Unknown'),
                "uploaded_at": firestore.SERVER_TIMESTAMP
            })
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)