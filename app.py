import os
os.environ["HTTPX_DISABLE_HTTP2"] = "1"  # Cloudinary upload fix on Windows

import uuid
import logging
from datetime import datetime

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    current_app,
    session,
)
from pymongo import MongoClient
from bson.objectid import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
import cloudinary.api
import requests

# --------------------------------------------------------
# Logging
# --------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------------------------------------------------------
# Load .env variables
# --------------------------------------------------------
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

SUPABASE_URL = os.getenv("SUPABASE_URL") or "https://cepabjmlengczyiezdqd.supabase.co"
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET") or "streetwalk"
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

# ---------------- MapTiler config ----------------
MAPTILER_KEY = os.getenv("MAPTILER_KEY")
MAP_STYLE_URL = (
    f"https://api.maptiler.com/maps/streets-v2/style.json?key={MAPTILER_KEY}"
    if MAPTILER_KEY
    else ""
)

# --------------------------------------------------------
# Cloudinary Config
# --------------------------------------------------------
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
    secure=True,
    timeout=180,
)

# --------------------------------------------------------
# Flask App
# --------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(
    os.getenv("MAX_CONTENT_LENGTH", str(1024 * 1024 * 1024))
)  # 1GB default
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(24)

# expose map style to templates
app.config["MAP_STYLE_URL"] = MAP_STYLE_URL

# --------------------------------------------------------
# MongoDB Setup
# --------------------------------------------------------
client = MongoClient(MONGO_URI)
db = client["streetwalk"]
streets_collection = db["streets"]

# --------------------------------------------------------
# MongoDB Indexes
# --------------------------------------------------------
streets_collection.create_index([("type", 1), ("mode", 1)])
streets_collection.create_index([("createdAt", -1)])
streets_collection.create_index([("likes", -1)])
streets_collection.create_index([("lat", 1), ("lng", 1)])

# --------------------------------------------------------
# Simple admin protection for /upload
# --------------------------------------------------------
@app.before_request
def protect_upload():
    # Allow static and make-admin itself
    if request.endpoint in (None, "static", "make_admin"):
        return

    if request.endpoint == "upload" and not session.get("is_admin"):
        return redirect(url_for("index"))


@app.route("/make-admin")
def make_admin():
    session["is_admin"] = True
    return "Admin enabled"


# --------------------------------------------------------
# Helpers
# --------------------------------------------------------
MAX_VIDEO_SIZE = 100 * 1024 * 1024  # 100MB
MAX_GLB_SIZE = 50 * 1024 * 1024     # 50MB


def clean_text(value, max_len=200):
    if not value:
        return None
    return value.strip()[:max_len]


def upload_glb_supabase(file):
    filename = f"models/{uuid.uuid4()}.glb"
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "model/gltf-binary",
    }

    try:
        res = requests.post(upload_url, headers=headers, data=file.read())
    except Exception:
        logger.error("Supabase upload failed (network error)", exc_info=True)
        raise

    if res.status_code in (200, 201):
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
    else:
        logger.error("Supabase upload failed: %s", res.text)
        raise Exception(f"Supabase Upload Failed: {res.text}")


def upload_video_cloudinary(file):
    try:
        upload = cloudinary.uploader.upload(
            file,
            folder="streetwalk_videos",
            resource_type="video",
            timeout=180,
        )
        return upload["secure_url"]
    except Exception:
        logger.error("Cloudinary video upload failed", exc_info=True)
        raise


def get_street_by_id(street_id):
    if not street_id:
        return None
    try:
        oid = ObjectId(street_id)
    except InvalidId:
        return None
    try:
        doc = streets_collection.find_one({"_id": oid})
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


def published_not_deleted(extra=None):
    base = {"status": "published", "deleted": False}
    if extra:
        base.update(extra)
    return base


def distinct_categories_for_mode(mode: str):
    """
    Return sorted unique category names for a given mode.
    Only applied to video streets with that mode.
    """
    cats = streets_collection.distinct(
        "category",
        {"type": "video", "mode": mode, "status": "published", "deleted": False},
    )
    cats = [c for c in cats if c]
    return sorted(cats)


# --------------------------------------------------------
# Home Page - show all published, not deleted streets
# --------------------------------------------------------
@app.route("/")
def index():
    streets = list_with_str_id(streets_collection.find(published_not_deleted()))
    return render_template("index.html", streets=streets)


# --------------------------------------------------------
# Generic World Page (avatar WALK + 3D only)
# --------------------------------------------------------
@app.route("/world")
def world():
    streets = list_with_str_id(streets_collection.find(published_not_deleted()))

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    street_id = request.args.get("street_id")
    selected_street = get_street_by_id(street_id)
    if selected_street and (
        selected_street.get("status") != "published"
        or selected_street.get("deleted", False)
    ):
        selected_street = None

    return render_template(
        "world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        map_style_url=current_app.config["MAP_STYLE_URL"],
    )


# --------------------------------------------------------
# WALK world (avatar, video walk + 3D)
# --------------------------------------------------------
@app.route("/world/walk")
def world_walk():
    streets = list_with_str_id(
        streets_collection.find(
            published_not_deleted(
                {
                    "$or": [
                        {"type": "3d"},
                        {"type": "video", "mode": "walk"},
                    ]
                }
            )
        )
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    street_id = request.args.get("street_id")
    selected_street = get_street_by_id(street_id)

    if selected_street and not (
        selected_street.get("status") == "published"
        and not selected_street.get("deleted", False)
        and (
            selected_street.get("type") == "3d"
            or (
                selected_street.get("type") == "video"
                and selected_street.get("mode") == "walk"
            )
        )
    ):
        selected_street = None

    return render_template(
        "world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        map_style_url=current_app.config["MAP_STYLE_URL"],
    )


# --------------------------------------------------------
# DRIVE world (no avatar) – drive_world.html
# --------------------------------------------------------
@app.route("/world/drive")
def world_drive():
    streets = list_with_str_id(
        streets_collection.find(
            published_not_deleted({"type": "video", "mode": "drive"})
        )
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    street_id = request.args.get("street_id")
    selected_street = get_street_by_id(street_id)
    if selected_street and not (
        selected_street.get("status") == "published"
        and not selected_street.get("deleted", False)
        and selected_street.get("type") == "video"
        and selected_street.get("mode") == "drive"
    ):
        selected_street = None

    return render_template(
        "drive_world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        map_style_url=current_app.config["MAP_STYLE_URL"],
    )


# --------------------------------------------------------
# FLY world (no avatar) – fly_world.html
# --------------------------------------------------------
@app.route("/world/fly")
def world_fly():
    streets = list_with_str_id(
        streets_collection.find(
            published_not_deleted({"type": "video", "mode": "fly"})
        )
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    street_id = request.args.get("street_id")
    selected_street = get_street_by_id(street_id)
    if selected_street and not (
        selected_street.get("status") == "published"
        and not selected_street.get("deleted", False)
        and selected_street.get("type") == "video"
        and selected_street.get("mode") == "fly"
    ):
        selected_street = None

    return render_template(
        "fly_world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        map_style_url=current_app.config["MAP_STYLE_URL"],
    )


# --------------------------------------------------------
# SIT world (no avatar) – sit_world.html
# --------------------------------------------------------
@app.route("/world/sit")
def world_sit():
    streets = list_with_str_id(
        streets_collection.find(
            published_not_deleted({"type": "video", "mode": "sit"})
        )
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}

    street_id = request.args.get("street_id")
    selected_street = get_street_by_id(street_id)
    if selected_street and not (
        selected_street.get("status") == "published"
        and not selected_street.get("deleted", False)
        and selected_street.get("type") == "video"
        and selected_street.get("mode") == "sit"
    ):
        selected_street = None

    return render_template(
        "sit_world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        map_style_url=current_app.config["MAP_STYLE_URL"],
    )


# --------------------------------------------------------
# LIST PAGES for each mode (published + not deleted)
# --------------------------------------------------------
@app.route("/walk")
def walk():
    category = request.args.get("category", "").strip() or None

    query = published_not_deleted({"type": "video", "mode": "walk"})
    if category and category.lower() != "all":
        query["category"] = category

    walk_streets = list_with_str_id(streets_collection.find(query))
    categories = distinct_categories_for_mode("walk")

    return render_template(
        "walk.html",
        streets=walk_streets,
        categories=categories,
        active_category=category or "all",
    )


@app.route("/drive")
def drive():
    category = request.args.get("category", "").strip() or None

    query = published_not_deleted({"type": "video", "mode": "drive"})
    if category and category.lower() != "all":
        query["category"] = category

    drive_streets = list_with_str_id(streets_collection.find(query))
    categories = distinct_categories_for_mode("drive")

    return render_template(
        "drive.html",
        streets=drive_streets,
        categories=categories,
        active_category=category or "all",
    )


@app.route("/fly")
def fly():
    category = request.args.get("category", "").strip() or None

    query = published_not_deleted({"type": "video", "mode": "fly"})
    if category and category.lower() != "all":
        query["category"] = category

    fly_streets = list_with_str_id(streets_collection.find(query))
    categories = distinct_categories_for_mode("fly")

    return render_template(
        "fly.html",
        streets=fly_streets,
        categories=categories,
        active_category=category or "all",
    )


@app.route("/sit")
def sit():
    category = request.args.get("category", "").strip() or None

    query = published_not_deleted({"type": "video", "mode": "sit"})
    if category and category.lower() != "all":
        query["category"] = category

    sit_streets = list_with_str_id(streets_collection.find(query))
    categories = distinct_categories_for_mode("sit")

    return render_template(
        "sit.html",
        streets=sit_streets,
        categories=categories,
        active_category=category or "all",
    )


# --------------------------------------------------------
# Upload Route - MULTIPLE VIDEOS SUPPORTED
# (unchanged logic, but adds deleted=False)
# --------------------------------------------------------
@app.route("/upload", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        street_type = request.form.get("street_type")

        mode = request.form.get("mode")
        name = clean_text(request.form.get("name"), 100)
        city = clean_text(request.form.get("city"), 50)
        country = clean_text(request.form.get("country"), 50)

        try:
            lat = float(request.form.get("lat"))
            lng = float(request.form.get("lng"))
        except Exception:
            flash("Invalid latitude/longitude", "error")
            return redirect(url_for("upload"))

        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            flash("Latitude/longitude out of range", "error")
            return redirect(url_for("upload"))

        category = clean_text(request.form.get("category"), 80)
        description = clean_text(request.form.get("description"), 500)

        if street_type == "video":
            if mode not in ["walk", "drive", "fly", "sit"]:
                mode = "walk"

            files = request.files.getlist("video")
            links_raw = request.form.get("video_links")

            video_urls = []

            if files:
                for file in files:
                    if file and file.filename:
                        file.seek(0, os.SEEK_END)
                        size = file.tell()
                        file.seek(0)

                        if size > MAX_VIDEO_SIZE:
                            flash("Each video must be under 100MB", "error")
                            return redirect(url_for("upload"))

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
                "mode": mode,
                "category": category,
                "name": name,
                "city": city,
                "country": country,
                "lat": lat,
                "lng": lng,
                "description": description,
                "videos": [
                    {"url": url, "title": f"Part {i + 1}"}
                    for i, url in enumerate(video_urls)
                ],
                "likes": 0,
                "createdAt": datetime.utcnow(),
                "status": "published",
                "deleted": False,
            }

        elif street_type == "3d":
            file = request.files.get("model")
            link = request.form.get("model_link")

            glb_url = None

            if file and file.filename:
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(0)

                if size > MAX_GLB_SIZE:
                    flash("GLB file must be under 50MB", "error")
                    return redirect(url_for("upload"))

                try:
                    glb_url = upload_glb_supabase(file)
                except Exception as e:
                    flash("GLB upload failed: " + str(e), "error")
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
                "description": description,
                "lat": lat,
                "lng": lng,
                "glbUrl": glb_url,
                "likes": 0,
                "createdAt": datetime.utcnow(),
                "status": "published",
                "deleted": False,
            }

        else:
            flash("Invalid street type!", "error")
            return redirect(url_for("upload"))

        streets_collection.insert_one(street_doc)
        flash("Street added successfully!", "success")
        return redirect(url_for("index"))

    return render_template("upload.html")


# --------------------------------------------------------
# Dashboard Page (keeps all, including deleted/draft)
# --------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    streets = list_with_str_id(streets_collection.find())

    total_streets = len(streets)
    total_likes = sum(s.get("likes", 0) for s in streets)
    walk_count = sum(
        1 for s in streets if s.get("type") == "video" and s.get("mode") == "walk"
    )
    drive_count = sum(
        1 for s in streets if s.get("type") == "video" and s.get("mode") == "drive"
    )
    fly_count = sum(
        1 for s in streets if s.get("type") == "video" and s.get("mode") == "fly"
    )
    sit_count = sum(
        1 for s in streets if s.get("type") == "video" and s.get("mode") == "sit"
    )

    recent_streets = sorted(
        streets,
        key=lambda s: s.get("createdAt") or datetime.min,
        reverse=True,
    )[:8]

    return render_template(
        "dash.html",
        streets=streets,
        total_streets=total_streets,
        total_likes=total_likes,
        walk_count=walk_count,
        drive_count=drive_count,
        fly_count=fly_count,
        sit_count=sit_count,
        recent_streets=recent_streets,
    )


# --------------------------------------------------------
# Like Endpoint with safer ObjectId + spam guard
# --------------------------------------------------------
@app.post("/like/<street_id>")
def like_street(street_id):
    try:
        oid = ObjectId(street_id)
    except InvalidId:
        return {"error": "Invalid ID"}, 400

    liked = set(session.get("liked", []))
    if street_id in liked:
        street = streets_collection.find_one({"_id": oid}, {"likes": 1})
        return {"likes": street.get("likes", 0) if street else 0}

    streets_collection.update_one({"_id": oid}, {"$inc": {"likes": 1}})

    liked.add(street_id)
    session["liked"] = list(liked)

    street = streets_collection.find_one({"_id": oid}, {"likes": 1})
    return {"likes": street.get("likes", 0) if street else 0}


# --------------------------------------------------------
# Start Server
# --------------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
