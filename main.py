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
        search_query = request.args.get("search")
        images_ref = db.collection("images")
        
        if search_query:
            docs = images_ref.where("metadata.Model", "==", search_query).stream()
        else:
            docs = images_ref.stream()

        items = []
        for doc in docs:
            data = doc.to_dict()
            metadata = data.get('metadata', {})
            # We look for both short and long keys just in case old data remains
            items.append({
                'name': data.get('filename', 'Unknown'),
                'camera': metadata.get('Model', 'Unknown'),
                'thumb_url': data.get('thumb_url') or data.get('thumbnail_url'),
                'orig_url': data.get('orig_url') or data.get('original_url')
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
            # 1. Upload Original
            orig_blob = bucket.blob(f"originals/{file.filename}")
            orig_blob.upload_from_file(file, content_type=file.content_type)
            
            # 2. Upload Thumbnail
            file.seek(0)
            thumb_io = create_thumbnail(file)
            thumb_blob = bucket.blob(f"thumbnails/{file.filename}")
            thumb_blob.upload_from_file(thumb_io, content_type="image/jpeg")
            
            # 3. Save to Firestore (Using SHORT KEYS)
            db.collection("images").document(file.filename).set({
                "filename": file.filename,
                "orig_url": orig_blob.public_url,
                "thumb_url": thumb_blob.public_url,
                "metadata": {}, # Web upload metadata extraction is optional
                "uploaded_at": firestore.SERVER_TIMESTAMP
            })
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)