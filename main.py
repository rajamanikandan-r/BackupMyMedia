import os
import io # Used to handle image data in memory
from flask import Flask, render_template, request, redirect, url_for
from google.cloud import storage
from PIL import Image # The Pillow library
from google.cloud import firestore
from PIL.ExifTags import TAGS, GPSTAGS

app = Flask(__name__)
BUCKET_NAME = os.environ.get("BUCKET_NAME", "lb40-bucket")
storage_client = storage.Client()

db = firestore.Client(database = "media-metadata")

def get_exif_data(file_stream):
    """Extracts basic EXIF metadata from an image."""
    img = Image.open(file_stream)
    exif_data = {}
    info = img._getexif()
    
    if info:
        for tag, value in info.items():
            decoded = TAGS.get(tag, tag)
            # We specifically want Camera, Date, and potentially GPS
            if decoded in ['Make', 'Model', 'DateTimeOriginal', 'Software']:
                exif_data[decoded] = str(value)
    
    # Reset stream for next use
    file_stream.seek(0)
    return exif_data

def create_thumbnail(file_stream, size=(200, 200)):
    """Generates a thumbnail from an image stream."""
    img = Image.open(file_stream)
    img.thumbnail(size)
    
    # Save the thumbnail to a byte stream instead of a file on disk
    thumb_io = io.BytesIO()
    # If the original is PNG/GIF, we save accordingly; otherwise, JPEG is safe
    img_format = img.format if img.format else "JPEG"
    img.save(thumb_io, img_format)
    thumb_io.seek(0)
    return thumb_io, img_format

@app.route("/")
def index():
    try:
        # 1. Get the search term from the URL (e.g., /?search=iPhone)
        query_param = request.args.get("search")
        images_ref = db.collection("images")
        
        # 2. Query Firestore
        if query_param:
            # Note: Firestore '==' is case-sensitive. 
            # If you saved "iPhone", searching "iphone" won't work.
            docs = images_ref.where("camera", "==", query_param).stream()
        else:
            # If no search, get everything
            docs = images_ref.stream()

        items = []
        for doc in docs:
            # .to_dict() converts the Firestore document into a Python dictionary
            data = doc.to_dict()
            
            # --- CRITICAL SYNC CHECK ---
            # Ensure these keys match your gallery.html: {{ item.thumb_url }} and {{ item.orig_url }}
            # If your database uses different keys, we map them here:
            items.append({
                'name': data.get('name', 'Unknown'),
                'camera': data.get('camera', 'Unknown'),
                'date_taken': data.get('date_taken', 'Unknown'),
                'thumb_url': data.get('thumb_url'), # This MUST match the HTML
                'orig_url': data.get('orig_url')    # This MUST match the HTML
            })
            
        # 3. Render the page with the items list
        return render_template("gallery.html", items=items)

    except Exception as e:
        # This helps you see what went wrong in the browser if the DB fails
        print(f"Error in index(): {e}")
        return f"Database Error: {e}", 500

@app.route("/upload", methods=["POST"])
def upload():
    # Use getlist to handle multiple files
    files = request.files.getlist("photos")
    
    if files:
        bucket = storage_client.bucket(BUCKET_NAME)
        
        for file in files:
            # Check if a file was actually selected (prevents empty submissions)
            if file.filename == '':
                continue
                
            file_name = file.filename

            metadata = get_exif_data(file)
            
            try:
                # 1. Upload the Original
                orig_blob = bucket.blob(f"originals/{file_name}")
                orig_blob.upload_from_file(file)
                
                # 2. Reset file pointer and generate Thumbnail
                file.seek(0) 
                thumb_io, img_format = create_thumbnail(file)
                
                # 3. Upload the Thumbnail
                thumb_blob = bucket.blob(f"thumbnails/{file_name}")
                thumb_blob.upload_from_file(
                    thumb_io, 
                    content_type=f"image/{img_format.lower()}"
                )
                print(f"Successfully processed: {file_name}")
                
            except Exception as e:
                print(f"Error processing {file_name}: {e}")
                # We continue to the next file even if one fails
                continue
                
            db.collection("images").document(file.filename).set({
            "name": file.filename,
            "camera": metadata.get("Model", "Unknown"),
            "make": metadata.get("Make", "Unknown"),
            "date_taken": metadata.get("DateTimeOriginal", "Unknown"),
            "thumb_url": f"https://storage.googleapis.com/{BUCKET_NAME}/thumbnails/{file_name}",
            "orig_url": f"https://storage.googleapis.com/{BUCKET_NAME}/originals/{file_name}"
        })
    
        
    return redirect(url_for("index"))