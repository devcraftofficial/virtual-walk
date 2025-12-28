import os
os.environ["HTTPX_DISABLE_HTTP2"] = "1"  # Cloudinary upload fix on Windows

import uuid
import logging
from datetime import datetime, timezone, timedelta

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

from authlib.integrations.flask_client import OAuth
from werkzeug.security import generate_password_hash, check_password_hash

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

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

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
# OAuth (Google)
# --------------------------------------------------------
oauth = OAuth(app)
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
else:
    logger.warning("Google OAuth client ID/secret not set; Google login disabled.")

# --------------------------------------------------------
# MongoDB Setup
# --------------------------------------------------------
client = MongoClient(MONGO_URI)
db = client["streetwalk"]
streets_collection = db["streets"]
users_collection = db["users"]
reset_tokens = db["password_resets"]

# --------------------------------------------------------
# MongoDB Indexes
# --------------------------------------------------------
streets_collection.create_index([("type", 1), ("mode", 1)])
streets_collection.create_index([("createdAt", -1)])
streets_collection.create_index([("likes", -1)])
streets_collection.create_index([("lat", 1), ("lng", 1)])

users_collection.create_index("email", unique=True)
users_collection.create_index("googleId", unique=True, sparse=True)
reset_tokens.create_index("expiresAt", expireAfterSeconds=0)

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
    cats = streets_collection.distinct(
        "category",
        {"type": "video", "mode": mode, "status": "published", "deleted": False},
    )
    cats = [c for c in cats if c]
    return sorted(cats)

def format_date(dt):
    if not dt:
        return ""
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).strftime("%d %b %Y")
    return ""

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    try:
        oid = ObjectId(uid)
        user = users_collection.find_one({"_id": oid})
        if user:
            user["_id"] = str(user["_id"])
        return user
    except InvalidId:
        return None

@app.context_processor
def inject_current_user():
    return {"current_user": current_user()}

# --------------------------------------------------------
# Route protection for upload & dashboard
# --------------------------------------------------------
@app.before_request
def protect_protected_routes():
    # Allow public routes and static/auth
    public_endpoints = (
        None, "static", "index", "world", "world_walk", "world_drive", 
        "world_fly", "world_sit", "walk", "drive", "fly", "sit",
        "login", "signup", "forgot_password", "reset_password",
        "login_google", "auth_google_callback", "make_admin", "logout"
    )
    
    if request.endpoint in public_endpoints:
        return

    # Protect upload and dashboard
    if request.endpoint in ("upload", "dashboard") and not session.get("user_id"):
        session["next"] = request.path
        return redirect(url_for("login"))

@app.route("/make-admin")
def make_admin():
    session["is_admin"] = True
    return "Admin enabled"

# --------------------------------------------------------
# Home Page
# --------------------------------------------------------
@app.route("/")
def index():
    streets = list_with_str_id(streets_collection.find(published_not_deleted()))
    return render_template("index.html", streets=streets, map_style_url=MAP_STYLE_URL)

# --------------------------------------------------------
# Generic World Page
# --------------------------------------------------------
@app.route("/world")
def world():
    street_id = request.args.get("street_id")
    streets = list_with_str_id(streets_collection.find(published_not_deleted()))
    
    center = {"lat": 25.2048, "lng": 55.2708}
    
    if street_id:
        selected_street = get_street_by_id(street_id)
        if selected_street and (
            selected_street.get("status") != "published" 
            or selected_street.get("deleted", False)
        ):
            selected_street = None
    else:
        selected_street = None
    
    # ✅ CRITICAL FIX: Route to correct template based on street MODE
    if selected_street:
        mode = selected_street.get("mode", "walk")
        template_map = {
            "walk": "world.html",
            "drive": "drive_world.html", 
            "fly": "fly_world.html",
            "sit": "sit_world.html"
        }
        template = template_map.get(mode, "world.html")
        
        # Filter streets by same mode for sidebar
        mode_streets = list_with_str_id(
            streets_collection.find(published_not_deleted({"mode": mode}))
        )
        
        if streets:
            center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}
            
        return render_template(
            template,
            streets=mode_streets,  # Same mode streets
            center=center,
            selected_street=selected_street,
            mode=mode,  # Pass mode to template
            map_style_url=MAP_STYLE_URL,
        )
    
    # No street selected - default to walk
    return render_template(
        "world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        map_style_url=MAP_STYLE_URL,
    )


# --------------------------------------------------------
# WALK world
# --------------------------------------------------------
@app.route("/world/walk")
def world_walk():
    streets = list_with_str_id(
        streets_collection.find(
            published_not_deleted({
                "$or": [
                    {"type": "3d"},
                    {"type": "video", "mode": "walk"},
                ]
            })
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
            or (selected_street.get("type") == "video" and selected_street.get("mode") == "walk")
        )
    ):
        selected_street = None

    return render_template(
        "world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        map_style_url=MAP_STYLE_URL,
    )

# --------------------------------------------------------
# Signup
# --------------------------------------------------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        name = request.form["name"].strip()
        password = request.form["password"]

        if users_collection.find_one({"email": email}):
            flash("Email already registered", "error")
            return redirect(url_for("signup"))

        users_collection.insert_one({
            "email": email,
            "name": name,
            "passwordHash": generate_password_hash(password),
            "createdAt": datetime.utcnow(),
            "lastLoginAt": None,
        })
        flash("Account created. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")

# --------------------------------------------------------
# Email/password Login - FIXED ✅
# --------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]

        user = users_collection.find_one({"email": email})
        if not user or not user.get("passwordHash"):
            flash("Invalid email or password", "error")
            return redirect(url_for("login"))

        if not check_password_hash(user["passwordHash"], password):
            flash("Invalid email or password", "error")
            return redirect(url_for("login"))

        users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {"lastLoginAt": datetime.utcnow()}}
        )

        session["user_id"] = str(user["_id"])
        session["user_name"] = user.get("name", user.get("email", "User"))  # ✅ Store user name
        session["is_admin"] = True

        next_url = session.pop("next", None) or url_for("index")  # ✅ Always to index
        return redirect(next_url)

    return render_template("login.html")

# --------------------------------------------------------
# Logout
# --------------------------------------------------------
@app.route("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("index"))

# --------------------------------------------------------
# Google Login - FIXED ✅
# --------------------------------------------------------
@app.route("/login/google")
def login_google():
    if "google" not in oauth._registry:
        flash("Google login is not configured.", "error")
        return redirect(url_for("login"))

    next_url = request.args.get("next") or session.get("next") or url_for("index")
    session["next"] = next_url

    redirect_uri = url_for("auth_google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/auth/google/callback")
def auth_google_callback():
    if "google" not in oauth._registry:
        flash("Google login is not configured.", "error")
        return redirect(url_for("login"))

    token = oauth.google.authorize_access_token()
    userinfo = oauth.google.userinfo()

    google_id = userinfo.get("sub")
    email = (userinfo.get("email") or "").lower()
    name = userinfo.get("name") or ""
    now = datetime.utcnow()

    # 1) Try find by googleId
    user = users_collection.find_one({"googleId": google_id})

    if not user and email:
        # 2) Try find by email (existing local account)
        user = users_collection.find_one({"email": email})

    if user:
        # 3) Update existing user, attach googleId if missing
        users_collection.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "googleId": google_id,
                    "email": email,
                    "name": name,
                    "lastLoginAt": now,
                },
                "$setOnInsert": {"createdAt": user.get("createdAt", now)},
            },
        )
    else:
        # 4) Create brand new user
        users_collection.insert_one(
            {
                "googleId": google_id,
                "email": email,
                "name": name,
                "createdAt": now,
                "lastLoginAt": now,
            }
        )
        user = users_collection.find_one({"googleId": google_id})

    session["user_id"] = str(user["_id"])
    session["user_name"] = user.get("name", user.get("email", "User"))
    session["is_admin"] = True
    session["google_user"] = {
        "id": google_id,
        "email": email,
        "name": name,
    }

    next_url = session.pop("next", None)
    return redirect(next_url or url_for("index"))

# --------------------------------------------------------
# Forgot / Reset Password
# --------------------------------------------------------
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        user = users_collection.find_one({"email": email})
        if user:
            token = uuid.uuid4().hex
            reset_tokens.insert_one({
                "userId": user["_id"],
                "token": token,
                "expiresAt": datetime.utcnow() + timedelta(hours=1),
            })
            reset_link = url_for("reset_password", token=token, _external=True)
            logger.info("Password reset link for %s: %s", email, reset_link)
        flash("If that email exists, a reset link has been sent.", "info")
        return redirect(url_for("login"))
    return render_template("forgot_password.html")

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    rec = reset_tokens.find_one({"token": token})
    if not rec:
        flash("Reset link is invalid or expired.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        new_pw = request.form["password"]
        users_collection.update_one(
            {"_id": rec["userId"]},
            {"$set": {"passwordHash": generate_password_hash(new_pw)}}
        )
        reset_tokens.delete_one({"_id": rec["_id"]})
        flash("Password updated. You can log in now.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html")

# --------------------------------------------------------
# DRIVE world
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
        map_style_url=MAP_STYLE_URL,
    )

# --------------------------------------------------------
# FLY world
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
        map_style_url=MAP_STYLE_URL,
    )

# --------------------------------------------------------
# SIT world
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
        map_style_url=MAP_STYLE_URL,
    )

# --------------------------------------------------------
# LIST PAGES
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
# Upload Route
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
                links = [link.strip() for link in links_raw.replace("\n", ",").split(",")]
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
                "videos": [{"url": url, "title": f"Part {i + 1}"} for i, url in enumerate(video_urls)],
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
# Dashboard Page
# --------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    streets = list_with_str_id(streets_collection.find())

    for s in streets:
        s["createdAtFmt"] = format_date(s.get("createdAt"))

    total_streets = len(streets)
    total_likes = sum(s.get("likes", 0) for s in streets)
    walk_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "walk")
    drive_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "drive")
    fly_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "fly")
    sit_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "sit")

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
        map_style_url=MAP_STYLE_URL,
    )

# --------------------------------------------------------
# Like Endpoint
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
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

