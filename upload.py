import hashlib
import os
import argparse
import io
from google.cloud import storage, firestore
from PIL import Image
from PIL.ExifTags import TAGS
from tqdm import tqdm

# --- CONFIGURATION ---
PROJECT_ID = "life-begins-at-40"
BUCKET_NAME = "lb40-bucket"
KEY_PATH = "life-begins-at-40-a0cf724dc4fe.json"

# --- INITIALIZATION ---
storage_client = storage.Client.from_service_account_json(KEY_PATH)
db = firestore.Client.from_service_account_json(
    KEY_PATH, project=PROJECT_ID, database="media-metadata"
)

def get_exif_data(path):
    try:
        img = Image.open(path)
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

def upload_image(file_path, quiet=False):
    filename = os.path.basename(file_path)

    with open(file_path, "rb") as f:
        content_hash = hashlib.md5(f.read()).hexdigest()

    existing = db.collection("images").where("content_hash", "==", content_hash).limit(1).stream()
    if any(True for _ in existing):
        return False  # duplicate

    bucket = storage_client.bucket(BUCKET_NAME)

    # 1. Upload Original
    orig_blob = bucket.blob(f"originals/{filename}")
    orig_blob.upload_from_filename(file_path)
    orig_url = f"https://storage.googleapis.com/{BUCKET_NAME}/originals/{filename}"

    # 2. Create and Upload Thumbnail
    img = Image.open(file_path)
    img.thumbnail((200, 200))
    thumb_io = io.BytesIO()
    img.save(thumb_io, format="JPEG")
    thumb_io.seek(0)
    
    thumb_blob = bucket.blob(f"thumbnails/{filename}")
    thumb_blob.upload_from_file(thumb_io, content_type="image/jpeg")
    thumb_url = f"https://storage.googleapis.com/{BUCKET_NAME}/thumbnails/{filename}"

    # 3. Get Metadata
    metadata = get_exif_data(file_path)

    # 4. Save to Firestore with auto-tags
    record = {
        "name": filename,
        "orig_url": orig_url,
        "thumb_url": thumb_url,
        "camera": metadata.get('Model', 'Unknown'),
        "make": metadata.get('Make', 'Unknown'),
        "date_taken": metadata.get('DateTimeOriginal', 'Unknown'),
        "uploaded_at": firestore.SERVER_TIMESTAMP,
        "content_hash": content_hash,
        "auto_tagged": True,
    }
    record["tags"] = auto_tags_from_record(record)
    db.collection("images").document(filename).set(record)
    if not quiet:
        print(f"Uploaded: {filename}")
    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Path to image or directory")
    args = parser.parse_args()

    target_path = os.path.abspath(args.dir)

    if os.path.isfile(target_path):
        uploaded = upload_image(target_path)
        if not uploaded:
            print("Skipped: duplicate.")
    elif os.path.isdir(target_path):
        files_to_upload = [f for f in os.listdir(target_path) 
                           if f.lower().endswith(('jpg', 'jpeg', 'png'))]
        
        if not files_to_upload:
            print("No images found.")
        else:
            print(f"🚀 Starting upload of {len(files_to_upload)} images...")
            uploaded, skipped = 0, 0
            for f in tqdm(files_to_upload, desc="Progress", unit="photo"):
                try:
                    if upload_image(os.path.join(target_path, f), quiet=True):
                        uploaded += 1
                    else:
                        skipped += 1
                except Exception:
                    print(f"Error uploading {f}")
            print(f"\n✅ {uploaded} uploaded, {skipped} skipped (duplicates).")