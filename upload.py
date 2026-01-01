import os
import argparse
import io
from google.cloud import storage, firestore
from PIL import Image
from PIL.ExifTags import TAGS
from tqdm import tqdm  # Make sure to run 'pip install tqdm'

# --- 1. CONFIGURATION ---
PROJECT_ID = "life-begins-at-40"
BUCKET_NAME = "lb40-bucket"
KEY_PATH = "life-begins-at-40-a0cf724dc4fe.json"

# --- 2. INITIALIZATION (Must be before functions) ---
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

def upload_image(file_path, quiet=False):
    # This function now "sees" storage_client because it's defined at the top
    filename = os.path.basename(file_path)
    bucket = storage_client.bucket(BUCKET_NAME)

    # 1. Upload Original
    orig_blob = bucket.blob(f"originals/{filename}")
    orig_blob.upload_from_filename(file_path)
    orig_url = f"https://storage.googleapis.com/{BUCKET_NAME}/originals/{filename}"

    # 2. Create and Upload Thumbnail
    img = Image.open(file_path)
    img.thumbnail((200, 200))
    
    # Use BytesIO to avoid creating extra temp files on your disk
    thumb_io = io.BytesIO()
    img.save(thumb_io, format="JPEG")
    thumb_io.seek(0)
    
    thumb_blob = bucket.blob(f"thumbnails/{filename}")
    thumb_blob.upload_from_file(thumb_io, content_type="image/jpeg")
    thumb_url = f"https://storage.googleapis.com/{BUCKET_NAME}/thumbnails/{filename}"

    # 3. Get Metadata
    metadata = get_exif_data(file_path)

    # 4. Save to Firestore
    db.collection("images").document(filename).set({
        "filename": filename,
        "orig_url": orig_url,
        "thumb_url": thumb_url,
        "metadata": metadata,
        "uploaded_at": firestore.SERVER_TIMESTAMP
    })
    
    if not quiet:
        print(f"Uploaded: {filename}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", required=True, help="Path to image or directory")
    args = parser.parse_args()

    if os.path.isfile(args.dir):
        upload_image(args.dir)
    elif os.path.isdir(args.dir):
        files_to_upload = [f for f in os.listdir(args.dir) 
                           if f.lower().endswith(('jpg', 'jpeg', 'png'))]
        
        if not files_to_upload:
            print("No images found in that directory.")
        else:
            print(f"🚀 Starting upload of {len(files_to_upload)} images...")
            # Wrapping the loop in tqdm creates the progress bar
            for f in tqdm(files_to_upload, desc="Uploading Gallery", unit="photo"):
                upload_image(os.path.join(args.dir, f), quiet=True)
            print("\n✅ All uploads complete!")