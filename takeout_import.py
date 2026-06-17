"""
Download a Google Takeout zip, extract, upload photos, then clean up.
Usage: python takeout_import.py <url> [--cookies <path>] [--work-dir <path>]
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="Google Takeout download URL")
    parser.add_argument("--cookies", default=os.path.expanduser("~/Downloads/cookies.txt"))
    parser.add_argument("--work-dir", default=os.path.expanduser("~/Downloads/takeout_tmp"))
    args = parser.parse_args()

    os.makedirs(args.work_dir, exist_ok=True)
    zip_path = os.path.join(args.work_dir, "takeout.zip")
    extract_dir = os.path.join(args.work_dir, "extracted")

    try:
        # 1. Download
        print("⬇️  Downloading...")
        result = subprocess.run([
            "wget", "--load-cookies", args.cookies,
            "--content-disposition", "-O", zip_path,
            args.url
        ], check=True)

        # 2. Extract
        print("📦 Extracting...")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(extract_dir)
        print("Extraction done")

        # 3. Upload
        print("🚀 Uploading...")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        subprocess.run([
            sys.executable, os.path.join(script_dir, "upload.py"),
            "--dir", extract_dir
        ], check=True)

    finally:
        # 4. Cleanup
        print("🧹 Cleaning up...")
        shutil.rmtree(args.work_dir, ignore_errors=True)
        print("✅ Done.")


if __name__ == "__main__":
    main()
