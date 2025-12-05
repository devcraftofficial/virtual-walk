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
from bson.objectid import ObjectId

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
    timeout=180
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
# Helpers
# --------------------------------------------------------
def upload_glb_supabase(file):
    filename = f"models/{uuid.uuid4()}.glb"
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "model/gltf-binary",
    }

    res = requests.post(upload_url, headers=headers, data=file.read())

    if res.status_code in (200, 201):
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
    else:
        raise Exception(f"Supabase Upload Failed: {res.text}")


def upload_video_cloudinary(file):
    upload = cloudinary.uploader.upload(
        file,
        folder="streetwalk_videos",
        resource_type="video",
        timeout=180,
    )
    return upload["secure_url"]


def get_street_by_id(street_id):
    if not street_id:
        return None
    try:
        doc = streets_collection.find_one({"_id": ObjectId(street_id)})
    except Exception:
        return None
    if not doc:
        return None
    doc["_id"] = str(doc["_id"])
    return doc


def list_with_str_id(cursor):
    items = list(cursor)
    for s in items:
        s["_id"] = str(s["_id"])
    return items


# --------------------------------------------------------
# Home Page - show all streets
# --------------------------------------------------------
@app.route("/")
def index():
    streets = list_with_str_id(streets_collection.find())
    return render_template("index.html", streets=streets)


# --------------------------------------------------------
# Generic World Page (avatar WALK + 3D only)
# Optional street_id to load specific street
# --------------------------------------------------------
@app.route("/world")
def world():
    # all streets so you can test everything here if you want
    streets = list_with_str_id(streets_collection.find())

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    street_id = request.args.get("street_id")
    selected_street = get_street_by_id(street_id)

    return render_template(
        "world.html",          # avatar version
        streets=streets,
        center=center,
        selected_street=selected_street,
    )


# --------------------------------------------------------
# WALK world (avatar, same world.html but filtered to walk videos + 3D)
# --------------------------------------------------------
@app.route("/world/walk")
def world_walk():
    streets = list_with_str_id(
        streets_collection.find(
            {
                "$or": [
                    {"type": "3d"},
                    {"type": "video", "mode": "walk"},
                ]
            }
        )
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    street_id = request.args.get("street_id")
    selected_street = get_street_by_id(street_id)

    # safety: only allow walk/3d here
    if selected_street and not (
        selected_street.get("type") == "3d"
        or (selected_street.get("type") == "video" and selected_street.get("mode") == "walk")
    ):
        selected_street = None

    return render_template(
        "world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
    )


# --------------------------------------------------------
# DRIVE world (no avatar) – uses drive_world.html
# --------------------------------------------------------
@app.route("/world/drive")
def world_drive():
    streets = list_with_str_id(
        streets_collection.find({"type": "video", "mode": "drive"})
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    street_id = request.args.get("street_id")
    selected_street = get_street_by_id(street_id)
    if selected_street and (
        selected_street.get("type") != "video"
        or selected_street.get("mode") != "drive"
    ):
        selected_street = None

    return render_template(
        "drive_world.html",    # <— separate template without avatar
        streets=streets,
        center=center,
        selected_street=selected_street,
    )


# --------------------------------------------------------
# FLY world (no avatar) – uses fly_world.html
# --------------------------------------------------------
@app.route("/world/fly")
def world_fly():
    streets = list_with_str_id(
        streets_collection.find({"type": "video", "mode": "fly"})
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    street_id = request.args.get("street_id")
    selected_street = get_street_by_id(street_id)
    if selected_street and (
        selected_street.get("type") != "video"
        or selected_street.get("mode") != "fly"
    ):
        selected_street = None

    return render_template(
        "fly_world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
    )


# --------------------------------------------------------
# SIT world (no avatar) – uses sit_world.html
# --------------------------------------------------------
@app.route("/world/sit")
def world_sit():
    streets = list_with_str_id(
        streets_collection.find({"type": "video", "mode": "sit"})
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    street_id = request.args.get("street_id")
    selected_street = get_street_by_id(street_id)
    if selected_street and (
        selected_street.get("type") != "video"
        or selected_street.get("mode") != "sit"
    ):
        selected_street = None

    return render_template(
        "sit_world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
    )


# --------------------------------------------------------
# LIST PAGES for each mode (selection before entering world)
# --------------------------------------------------------
@app.route("/walk")
def walk():
    walk_streets = list_with_str_id(
        streets_collection.find({"type": "video", "mode": "walk"})
    )
    return render_template("walk.html", streets=walk_streets)


@app.route("/drive")
def drive():
    drive_streets = list_with_str_id(
        streets_collection.find({"type": "video", "mode": "drive"})
    )
    return render_template("drive.html", streets=drive_streets)


@app.route("/fly")
def fly():
    fly_streets = list_with_str_id(
        streets_collection.find({"type": "video", "mode": "fly"})
    )
    return render_template("fly.html", streets=fly_streets)


@app.route("/sit")
def sit():
    sit_streets = list_with_str_id(
        streets_collection.find({"type": "video", "mode": "sit"})
    )
    return render_template("sit.html", streets=sit_streets)


# --------------------------------------------------------
# (Optional legacy detail views – safe to delete later)
# --------------------------------------------------------
@app.route("/walk/<street_id>")
def walk_view(street_id):
    street = get_street_by_id(street_id)
    if not street:
        return "Street not found", 404

    streets = list_with_str_id(
        streets_collection.find({"type": "video", "mode": "walk"})
    )
    return render_template("walk.html", street=street, streets=streets)


@app.route("/drive/<street_id>")
def drive_view(street_id):
    street = get_street_by_id(street_id)
    if not street:
        return "Street not found", 404

    streets = list_with_str_id(
        streets_collection.find({"type": "video", "mode": "drive"})
    )
    return render_template("drive.html", street=street, streets=streets)


# --------------------------------------------------------
# Upload Route - MULTIPLE VIDEOS SUPPORTED
# Now includes 'mode' for video streets
# --------------------------------------------------------
@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        street_type = request.form.get("street_type")   # "video" or "3d"
        mode = request.form.get("mode")                 # "walk" | "drive" | "fly" | "sit" (for video)
        name = request.form.get("name")
        city = request.form.get("city")
        country = request.form.get("country")
        lat = float(request.form.get("lat"))
        lng = float(request.form.get("lng"))

        # ---------------- VIDEO STREET ----------------
        if street_type == "video":
            # basic safety: default to "walk" if missing
            if mode not in ["walk", "drive", "fly", "sit"]:
                mode = "walk"

            files = request.files.getlist("video")
            links_raw = request.form.get("video_links")

            video_urls = []

            if files:
                for file in files:
                    if file and file.filename:
                        try:
                            url = upload_video_cloudinary(file)
                            video_urls.append(url)
                        except Exception as e:
                            flash("Video upload failed: " + str(e), "error")
                            return redirect(url_for("upload"))

            if links_raw:
                links = [
                    link.strip()
                    for link in links_raw.replace("\n", ",").split(",")
                ]
                video_urls.extend([link for link in links if link])

            if not video_urls:
                flash("Upload one or more videos or paste links!", "error")
                return redirect(url_for("upload"))

            street_doc = {
                "type": "video",
                "mode": mode,  # walk / drive / fly / sit
                "name": name,
                "city": city,
                "country": country,
                "lat": lat,
                "lng": lng,
                "videos": [
                    {"url": url, "title": f"Part {i + 1}"}
                    for i, url in enumerate(video_urls)
                ],
                "likes": 0,
                "createdAt": datetime.utcnow(),
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
                "glbUrl": glb_url,
                "likes": 0,
                "createdAt": datetime.utcnow(),
            }

        else:
            flash("Invalid street type!", "error")
            return redirect(url_for("upload"))

        streets_collection.insert_one(street_doc)
        flash("Street added successfully!", "success")
        return redirect(url_for("index"))

    return render_template("upload.html")

# --------------------------------------------------------
# Dashboard Page
# --------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    # Get all streets (for map and stats)
    streets = list_with_str_id(streets_collection.find())

    # Simple stats (you can improve later)
    total_streets = len(streets)
    total_likes = sum(s.get("likes", 0) for s in streets)
    walk_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "walk")
    drive_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "drive")
    fly_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "fly")

    # sort by createdAt desc for recent list
    recent_streets = sorted(
        streets,
        key=lambda s: s.get("createdAt") or datetime.min,
        reverse=True
    )[:8]

    return render_template(
        "dash.html",          # your dashboard template file
        streets=streets,
        total_streets=total_streets,
        total_likes=total_likes,
        walk_count=walk_count,
        drive_count=drive_count,
        fly_count=fly_count,
        recent_streets=recent_streets,
    )

# --------------------------------------------------------
# Like Endpoint
# --------------------------------------------------------
@app.post("/like/<street_id>")
def like_street(street_id):
    streets_collection.update_one(
        {"_id": ObjectId(street_id)},
        {"$inc": {"likes": 1}},
    )

    street = streets_collection.find_one(
        {"_id": ObjectId(street_id)}, {"likes": 1}
    )
    return {"likes": street.get("likes", 0)}


# --------------------------------------------------------
# Start Server
# --------------------------------------------------------
  if __name__ == "__main__":
    app.run(debug=True)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

