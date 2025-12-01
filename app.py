# --------------------------------------------------------
# Disable HTTP/2 for Windows (Cloudinary upload fix)
# --------------------------------------------------------
import os
os.environ["HTTPX_DISABLE_HTTP2"] = "1"

import uuid
from flask import Flask, render_template, request, redirect, url_for, flash
from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
import cloudinary.api
import requests

# --------------------------------------------------------
# Load .env variables
# --------------------------------------------------------
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

SUPABASE_URL = "https://cepabjmlengczyiezdqd.supabase.co"
SUPABASE_BUCKET = "streetwalk"
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# --------------------------------------------------------
# Cloudinary Config
# --------------------------------------------------------
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
    secure=True,
    timeout=180  # Avoid Windows abort issue
)

# --------------------------------------------------------
# Flask App
# --------------------------------------------------------
app = Flask(__name__)
app.secret_key = "super-secret-key"

# --------------------------------------------------------
# MongoDB Setup
# --------------------------------------------------------
client = MongoClient(MONGO_URI)
db = client["streetwalk"]
streets_collection = db["streets"]

# --------------------------------------------------------
# Upload GLB to Supabase
# --------------------------------------------------------
def upload_glb_supabase(file):
    filename = f"models/{uuid.uuid4()}.glb"
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "model/gltf-binary"
    }

    res = requests.post(upload_url, headers=headers, data=file.read())

    if res.status_code in (200, 201):
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
    else:
        raise Exception(f"Supabase Upload Failed: {res.text}")


# --------------------------------------------------------
# Upload VIDEO to Cloudinary
# --------------------------------------------------------
def upload_video_cloudinary(file):
    upload = cloudinary.uploader.upload(
        file,
        folder="streetwalk_videos",
        resource_type="video",
        timeout=180
    )
    return upload["secure_url"]


# --------------------------------------------------------
# Home Page
# --------------------------------------------------------
@app.route("/")
def index():
    streets = list(streets_collection.find())
    for s in streets:
        s["_id"] = str(s["_id"])
    return render_template("index.html", streets=streets)

# --------------------------------------------------------
# World Page
# --------------------------------------------------------
@app.route("/world")
def world():
    streets = list(streets_collection.find())

    for s in streets:
        s["_id"] = str(s["_id"])

    # Default center
    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    return render_template("world.html", streets=streets, center=center)

# --------------------------------------------------------
# Upload Route
# --------------------------------------------------------
@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":

        street_type = request.form.get("street_type")
        name = request.form.get("name")
        city = request.form.get("city")
        country = request.form.get("country")
        lat = float(request.form.get("lat"))
        lng = float(request.form.get("lng"))

        # ---------------- VIDEO STREET ----------------
        if street_type == "video":
            file = request.files.get("video")
            link = request.form.get("video_link")

            if file and file.filename:
                try:
                    video_url = upload_video_cloudinary(file)
                except Exception as e:
                    flash("Video upload failed: " + str(e), "error")
                    return redirect(url_for("upload"))
            elif link:
                video_url = link.strip()
            else:
                flash("Upload a video or paste a video link!", "error")
                return redirect(url_for("upload"))

            street_doc = {
                "type": "video",
                "name": name,
                "city": city,
                "country": country,
                "lat": lat,
                "lng": lng,
                "videoUrl": video_url,
                "createdAt": datetime.utcnow()
            }

        # ---------------- 3D GLB STREET ----------------
        elif street_type == "3d":
            file = request.files.get("model")
            link = request.form.get("model_link")

            if file and file.filename:
                try:
                    glb_url = upload_glb_supabase(file)
                except Exception as e:
                    flash("GLB Upload failed: " + str(e), "error")
                    return redirect(url_for("upload"))
            elif link:
                glb_url = link.strip()
            else:
                flash("Upload a GLB or paste a model URL", "error")
                return redirect(url_for("upload"))

            street_doc = {
                "type": "3d",
                "name": name,
                "city": city,
                "country": country,
                "lat": lat,
                "lng": lng,
                "glbUrl": glb_url,      # <-- IMPORTANT FIX
                "createdAt": datetime.utcnow()
            }

        else:
            flash("Invalid street type!", "error")
            return redirect(url_for("upload"))

        streets_collection.insert_one(street_doc)
        flash("Street added successfully!", "success")
        return redirect(url_for("index"))

    return render_template("upload.html")


# --------------------------------------------------------
# Start Server
# --------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
