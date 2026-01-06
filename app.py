import os

# Keep your Windows Cloudinary workaround (harmless in prod; can disable via env if desired)
os.environ["HTTPX_DISABLE_HTTP2"] = os.getenv("HTTPX_DISABLE_HTTP2", "1")

import uuid
import logging
import math
import time
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta
from functools import wraps  # ✅ NEW

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    abort,
)
from pymongo import MongoClient
from bson.objectid import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
import cloudinary
import cloudinary.uploader
import requests

from authlib.integrations.flask_client import OAuth
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import RequestEntityTooLarge

# --------------------------------------------------------
# Logging (production-friendly)
# --------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
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
# Production toggles
# --------------------------------------------------------
ENV = os.getenv("FLASK_ENV", "").lower() or os.getenv("ENV", "").lower() or "production"
IS_PROD = ENV == "production"

# --------------------------------------------------------
# Upload limits (HARD server cap)
# --------------------------------------------------------
MAX_REQUEST_MB = int(os.getenv("MAX_REQUEST_MB", "120"))
MAX_CONTENT_LENGTH = MAX_REQUEST_MB * 1024 * 1024

# Per-file limits
MAX_VIDEO_SIZE = int(os.getenv("MAX_VIDEO_SIZE_MB", "100")) * 1024 * 1024  # default 100MB
MAX_GLB_SIZE = int(os.getenv("MAX_GLB_SIZE_MB", "50")) * 1024 * 1024      # default 50MB
MAX_THUMB_SIZE = int(os.getenv("MAX_THUMB_SIZE_MB", "5")) * 1024 * 1024  # default 5MB
ALLOWED_THUMB_EXT = {"jpg", "jpeg", "png", "webp"}


# --------------------------------------------------------
# Cloudinary Config
# --------------------------------------------------------
cloudinary.config(
    cloud_name=CLOUDINARY_CLOUD_NAME,
    api_key=CLOUDINARY_API_KEY,
    api_secret=CLOUDINARY_API_SECRET,
    secure=True,
    timeout=int(os.getenv("CLOUDINARY_TIMEOUT", "180")),
)

# --------------------------------------------------------
# Flask App
# --------------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

# Cookies hardened for production
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE=os.getenv("SESSION_COOKIE_SAMESITE", "Lax"),
)

# If you're behind HTTPS (Render/Cloudflare), set this true in prod:
if os.getenv("SESSION_COOKIE_SECURE", "1" if IS_PROD else "0") == "1":
    app.config["SESSION_COOKIE_SECURE"] = True

# If you use reverse proxy (Render, Nginx), set this so url_for(_external=True) uses https:
PREFERRED_URL_SCHEME = os.getenv("PREFERRED_URL_SCHEME")
if PREFERRED_URL_SCHEME:
    app.config["PREFERRED_URL_SCHEME"] = PREFERRED_URL_SCHEME

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
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is not set")

client = MongoClient(MONGO_URI)
db = client["streetwalk"]
streets_collection = db["streets"]
users_collection = db["users"]
reset_tokens = db["password_resets"]
activity_logs = db["activity_logs"]
geocode_cache = db["geocode_cache"]  # cache Nominatim responses

# --------------------------------------------------------
# MongoDB Indexes
# --------------------------------------------------------
streets_collection.create_index([("type", 1), ("mode", 1)])
streets_collection.create_index([("createdAt", -1)])
streets_collection.create_index([("likes", -1)])
streets_collection.create_index([("lat", 1), ("lng", 1)])
streets_collection.create_index([("ownerId", 1), ("deleted", 1)])

users_collection.create_index("email", unique=True)
users_collection.create_index("googleId", unique=True, sparse=True)

reset_tokens.create_index("token", unique=True)
reset_tokens.create_index("expiresAt", expireAfterSeconds=0)

activity_logs.create_index([("userId", 1), ("timestamp", -1)])

geocode_cache.create_index("q", unique=True)
geocode_cache.create_index("expiresAt", expireAfterSeconds=0)

# --------------------------------------------------------
# Helpers
# --------------------------------------------------------
def clean_text(value, max_len=200):
    if not value:
        return None
    return value.strip()[:max_len]


def make_json_safe(obj):
    if obj is None:
        return None

    if isinstance(obj, ObjectId):
        return str(obj)

    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, ObjectId):
                out[k] = str(v)
            elif isinstance(v, (dict, list)):
                out[k] = make_json_safe(v)
            else:
                out[k] = v
        return out

    if isinstance(obj, list):
        return [make_json_safe(x) for x in obj]

    return obj


def list_with_str_id(cursor):
    return [make_json_safe(s) for s in list(cursor)]


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
def allowed_thumb(filename: str) -> bool:
    if not filename:
        return False
    ext = filename.rsplit(".", 1)[-1].lower()
    return ext in ALLOWED_THUMB_EXT



def format_date(dt):
    if not dt:
        return ""
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc).strftime("%d %b %Y")
    return ""


def to_iso(dt):
    if not dt:
        return None
    if isinstance(dt, datetime):
        # always return UTC ISO
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return None


def start_date_for_range(days: int):
    # days=0 -> all time
    if not days or days <= 0:
        return None
    return datetime.utcnow() - timedelta(days=days)


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


def is_admin_user(user) -> bool:
    """
    Production-safe admin check.
    Use DB field 'role' == 'admin' instead of a session flag.
    """
    if not user:
        return False
    return user.get("role") == "admin"


# ✅ admin guard decorator
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            session["next"] = request.path
            return redirect(url_for("login"))
        if not is_admin_user(user):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def upload_glb_supabase(file):
    if not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_KEY not set")

    filename = f"models/{uuid.uuid4()}.glb"
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{filename}"

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "model/gltf-binary",
    }

    res = requests.post(upload_url, headers=headers, data=file.read(), timeout=30)

    if res.status_code in (200, 201):
        return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"
    logger.error("Supabase upload failed: %s", res.text)
    raise Exception(f"Supabase Upload Failed: {res.text}")


def upload_video_cloudinary(file):
    try:
        upload = cloudinary.uploader.upload(
            file,
            folder="streetwalk_videos",
            resource_type="video",
            timeout=int(os.getenv("CLOUDINARY_TIMEOUT", "180")),
        )
        return upload["secure_url"]
    except Exception:
        logger.error("Cloudinary video upload failed", exc_info=True)
        raise

def upload_image_cloudinary(file):
    try:
        upload = cloudinary.uploader.upload(
            file,
            folder="streetwalk_thumbs",
            resource_type="image",
            timeout=int(os.getenv("CLOUDINARY_TIMEOUT", "180")),
        )
        return upload["secure_url"]
    except Exception:
        logger.error("Cloudinary thumbnail upload failed", exc_info=True)
        raise

def get_street_by_id(street_id):
    if not street_id:
        return None
    try:
        oid = ObjectId(street_id)
    except InvalidId:
        return None
    doc = streets_collection.find_one({"_id": oid})
    if not doc:
        return None
    return make_json_safe(doc)


# ---------------- Email sender (Password reset) ----------------
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
SMTP_FROM = os.getenv("SMTP_FROM")  # e.g. "ABTO <no-reply@yourdomain.com>"
APP_BASE_URL = os.getenv("APP_BASE_URL")  # e.g. "https://abto.yourdomain.com"


def send_email(to_email: str, subject: str, body_text: str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and SMTP_FROM):
        logger.error("Email is not configured (SMTP_* missing).")
        raise RuntimeError("Email service not configured")

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body_text)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


# ---------------- Nominatim: caching + backoff ----------------
def _cache_key_for_query(query: str) -> str:
    return query.strip().lower()


def geocode_place(query: str):
    if not query:
        return None

    q = _cache_key_for_query(query)

    cached = geocode_cache.find_one({"q": q})
    if cached and ("data" in cached):
        return cached["data"]

    base_url = os.getenv("NOMINATIM_BASE_URL", "https://nominatim.openstreetmap.org/search")
    params = {"q": query, "format": "json", "limit": 1}

    headers = {
        "User-Agent": os.getenv(
            "NOMINATIM_USER_AGENT",
            "ABTO/1.0 (contact: support@yourdomain.com)",
        )
    }

    max_attempts = int(os.getenv("NOMINATIM_MAX_ATTEMPTS", "4"))
    base_sleep = float(os.getenv("NOMINATIM_BASE_SLEEP", "0.6"))
    timeout = float(os.getenv("NOMINATIM_TIMEOUT", "10"))

    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            res = requests.get(base_url, params=params, headers=headers, timeout=timeout)

            if res.status_code in (429, 500, 502, 503, 504):
                sleep_s = base_sleep * (2 ** (attempt - 1))
                logger.warning(
                    "Nominatim %s attempt %s/%s. Sleeping %.2fs",
                    res.status_code, attempt, max_attempts, sleep_s
                )
                time.sleep(sleep_s)
                continue

            res.raise_for_status()

            results = res.json() if res.text else []
            if not results:
                geocode_cache.update_one(
                    {"q": q},
                    {"$set": {"q": q, "data": None, "expiresAt": datetime.utcnow() + timedelta(hours=6)}},
                    upsert=True,
                )
                return None

            r = results[0]
            lat = float(r["lat"])
            lon = float(r["lon"])
            data = {"lat": lat, "lng": lon, "display_name": r.get("display_name", query)}

            geocode_cache.update_one(
                {"q": q},
                {"$set": {"q": q, "data": data, "expiresAt": datetime.utcnow() + timedelta(days=30)}},
                upsert=True,
            )
            return data

        except Exception as exc:
            last_exc = exc
            sleep_s = base_sleep * (2 ** (attempt - 1))
            logger.warning(
                "Nominatim error attempt %s/%s: %s. Sleeping %.2fs",
                attempt, max_attempts, exc, sleep_s
            )
            time.sleep(sleep_s)

    if last_exc:
        raise last_exc
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def estimate_price_aed(distance_km: float) -> dict:
    if distance_km <= 0:
        approx = 400.0
    else:
        base = 200.0
        if distance_km <= 2000:
            variable = distance_km * 0.45
        else:
            variable = 2000 * 0.45 + (distance_km - 2000) * 0.35
        approx = base + variable

    low = round(approx * 0.8 / 10) * 10
    high = round(approx * 1.2 / 10) * 10
    mid = round(approx / 10) * 10
    return {"currency": "AED", "low": float(low), "high": float(high), "mid": float(mid)}


def build_price_texts(distance_km: float, price_info: dict):
    distance_text = f"Distance: approx. {distance_km:,.1f} km (great-circle estimate)."
    currency = price_info.get("currency", "AED")
    low = price_info.get("low", 0.0)
    high = price_info.get("high", 0.0)
    mid = price_info.get("mid", 0.0)
    price_text = (
        f"Estimated one-way economy flight budget: "
        f"{currency} {low:,.0f} – {currency} {high:,.0f} "
        f"(typical ~ {currency} {mid:,.0f} per person)."
    )
    return distance_text, price_text


@app.context_processor
def inject_current_user():
    return {"current_user": current_user()}


# --------------------------------------------------------
# Error handling: large uploads (MAX_CONTENT_LENGTH)
# --------------------------------------------------------
@app.errorhandler(RequestEntityTooLarge)
def handle_large_upload(e):
    flash(f"Upload too large. Max request size is {MAX_REQUEST_MB}MB.", "error")
    ref = request.referrer or url_for("upload")
    return redirect(ref)


# --------------------------------------------------------
# Route protection for upload & dashboard
# --------------------------------------------------------
@app.before_request
def protect_protected_routes():
    public_endpoints = (
        None,
        "static",
        "index",
        "world",
        "world_walk",
        "world_drive",
        "world_fly",
        "world_sit",
        "walk",
        "drive",
        "fly",
        "sit",
        "how_it_works",  # ✅ NEW: public page
        "login",
        "signup",
        "forgot_password",
        "reset_password",
        "login_google",
        "auth_google_callback",
        "logout",
        "api_price",
        "like_street",
        # ✅ allow admin pages to be protected by decorator (not here)
    )

    if request.endpoint in public_endpoints:
        return

    protected = {"upload", "dashboard", "edit_street", "delete_street", "log_activity"}
    if request.endpoint in protected and not session.get("user_id"):
        session["next"] = request.path
        return redirect(url_for("login"))


# --------------------------------------------------------
# Home Page
# --------------------------------------------------------
@app.route("/")
def index():
    streets = list_with_str_id(streets_collection.find(published_not_deleted()))

    tour_cursor = streets_collection.find(
        published_not_deleted({"type": "video", "is_tour": True})
    ).sort("createdAt", -1).limit(8)
    tour_streets = list_with_str_id(tour_cursor)

    return render_template(
        "index.html",
        streets=streets,
        tour_streets=tour_streets,
        map_style_url=MAP_STYLE_URL,
    )


# --------------------------------------------------------
# ✅ NEW: How ABTO Works (separate page)
# --------------------------------------------------------
@app.route("/how-it-works")
def how_it_works():
    return render_template("how_it_works.html")


# --------------------------------------------------------
# Generic World Page
# --------------------------------------------------------
@app.route("/world")
def world():
    street_id = request.args.get("street_id")
    streets = list_with_str_id(streets_collection.find(published_not_deleted()))

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        try:
            center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}
        except KeyError:
            pass

    selected_street = None
    street_error = None

    if street_id:
        raw_doc = get_street_by_id(street_id)
        if not raw_doc:
            street_error = "not_found"
        elif raw_doc.get("status") != "published" or raw_doc.get("deleted", False):
            street_error = "unavailable"
        else:
            selected_street = raw_doc

    if selected_street:
        mode = selected_street.get("mode", "walk")
        template_map = {
            "walk": "world.html",
            "drive": "drive_world.html",
            "fly": "fly_world.html",
            "sit": "sit_world.html",
        }
        template = template_map.get(mode, "world.html")

        mode_streets = list_with_str_id(
            streets_collection.find(published_not_deleted({"mode": mode}))
        )

        if mode_streets:
            try:
                center = {"lat": mode_streets[0]["lat"], "lng": mode_streets[0]["lng"]}
            except KeyError:
                pass

        return render_template(
            template,
            streets=mode_streets,
            center=center,
            selected_street=selected_street,
            street_error=street_error,
            mode=mode,
            map_style_url=MAP_STYLE_URL,
        )

    return render_template(
        "world.html",
        streets=streets,
        center=center,
        selected_street=None,
        street_error=street_error,
        map_style_url=MAP_STYLE_URL,
    )


# --------------------------------------------------------
# WALK world
# --------------------------------------------------------
@app.route("/world/walk")
def world_walk():
    streets = list_with_str_id(
        streets_collection.find(
            published_not_deleted({"$or": [{"type": "3d"}, {"type": "video", "mode": "walk"}]})
        )
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        try:
            center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}
        except KeyError:
            pass

    street_id = request.args.get("street_id")
    selected_street = None
    street_error = None

    if street_id:
        candidate = get_street_by_id(street_id)
        if not candidate:
            street_error = "not_found"
        else:
            is_ok = (
                candidate.get("status") == "published"
                and not candidate.get("deleted", False)
                and (
                    candidate.get("type") == "3d"
                    or (candidate.get("type") == "video" and candidate.get("mode") == "walk")
                )
            )
            if is_ok:
                selected_street = candidate
            else:
                street_error = "unavailable"

    return render_template(
        "world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        street_error=street_error,
        map_style_url=MAP_STYLE_URL,
    )


# --------------------------------------------------------
# DRIVE world
# --------------------------------------------------------
@app.route("/world/drive")
def world_drive():
    streets = list_with_str_id(
        streets_collection.find(published_not_deleted({"type": "video", "mode": "drive"}))
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        try:
            center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}
        except KeyError:
            pass

    street_id = request.args.get("street_id")
    selected_street = None
    street_error = None

    if street_id:
        candidate = get_street_by_id(street_id)
        if not candidate:
            street_error = "not_found"
        else:
            is_ok = (
                candidate.get("status") == "published"
                and not candidate.get("deleted", False)
                and candidate.get("type") == "video"
                and candidate.get("mode") == "drive"
            )
            if is_ok:
                selected_street = candidate
            else:
                street_error = "unavailable"

    return render_template(
        "drive_world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        street_error=street_error,
        map_style_url=MAP_STYLE_URL,
    )


# --------------------------------------------------------
# FLY world
# --------------------------------------------------------
@app.route("/world/fly")
def world_fly():
    streets = list_with_str_id(
        streets_collection.find(published_not_deleted({"type": "video", "mode": "fly"}))
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        try:
            center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}
        except KeyError:
            pass

    street_id = request.args.get("street_id")
    selected_street = None
    street_error = None

    if street_id:
        candidate = get_street_by_id(street_id)
        if not candidate:
            street_error = "not_found"
        else:
            is_ok = (
                candidate.get("status") == "published"
                and not candidate.get("deleted", False)
                and candidate.get("type") == "video"
                and candidate.get("mode") == "fly"
            )
            if is_ok:
                selected_street = candidate
            else:
                street_error = "unavailable"

    return render_template(
        "fly_world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        street_error=street_error,
        map_style_url=MAP_STYLE_URL,
    )


# --------------------------------------------------------
# SIT world
# --------------------------------------------------------
@app.route("/world/sit")
def world_sit():
    streets = list_with_str_id(
        streets_collection.find(published_not_deleted({"type": "video", "mode": "sit"}))
    )

    center = {"lat": 25.2048, "lng": 55.2708}
    if streets:
        try:
            center = {"lat": streets[0]["lat"], "lng": streets[0]["lng"]}
        except KeyError:
            pass

    street_id = request.args.get("street_id")
    selected_street = None
    street_error = None

    if street_id:
        candidate = get_street_by_id(street_id)
        if not candidate:
            street_error = "not_found"
        else:
            is_ok = (
                candidate.get("status") == "published"
                and not candidate.get("deleted", False)
                and candidate.get("type") == "video"
                and candidate.get("mode") == "sit"
            )
            if is_ok:
                selected_street = candidate
            else:
                street_error = "unavailable"

    return render_template(
        "sit_world.html",
        streets=streets,
        center=center,
        selected_street=selected_street,
        street_error=street_error,
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
    return render_template("walk.html", streets=walk_streets, categories=categories, active_category=category or "all")


@app.route("/drive")
def drive():
    category = request.args.get("category", "").strip() or None
    query = published_not_deleted({"type": "video", "mode": "drive"})
    if category and category.lower() != "all":
        query["category"] = category
    drive_streets = list_with_str_id(streets_collection.find(query))
    categories = distinct_categories_for_mode("drive")
    return render_template("drive.html", streets=drive_streets, categories=categories, active_category=category or "all")


@app.route("/fly")
def fly():
    category = request.args.get("category", "").strip() or None
    query = published_not_deleted({"type": "video", "mode": "fly"})
    if category and category.lower() != "all":
        query["category"] = category
    fly_streets = list_with_str_id(streets_collection.find(query))
    categories = distinct_categories_for_mode("fly")
    return render_template("fly.html", streets=fly_streets, categories=categories, active_category=category or "all")


@app.route("/sit")
def sit():
    category = request.args.get("category", "").strip() or None
    query = published_not_deleted({"type": "video", "mode": "sit"})
    if category and category.lower() != "all":
        query["category"] = category
    sit_streets = list_with_str_id(streets_collection.find(query))
    categories = distinct_categories_for_mode("sit")
    return render_template("sit.html", streets=sit_streets, categories=categories, active_category=category or "all")


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

        users_collection.insert_one(
            {
                "email": email,
                "name": name,
                "passwordHash": generate_password_hash(password),
                "createdAt": datetime.utcnow(),
                "lastLoginAt": None,
                "role": "user",
            }
        )
        flash("Account created. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")


# --------------------------------------------------------
# Email/password Login
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

        users_collection.update_one({"_id": user["_id"]}, {"$set": {"lastLoginAt": datetime.utcnow()}})

        session["user_id"] = str(user["_id"])
        session["user_name"] = user.get("name", user.get("email", "User"))

        next_url = session.pop("next", None) or url_for("index")
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
# Google Login
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

    oauth.google.authorize_access_token()
    userinfo = oauth.google.userinfo()

    google_id = userinfo.get("sub")
    email = (userinfo.get("email") or "").lower()
    name = userinfo.get("name") or ""
    now = datetime.utcnow()

    user = users_collection.find_one({"googleId": google_id})
    if not user and email:
        user = users_collection.find_one({"email": email})

    if user:
        users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {"googleId": google_id, "email": email, "name": name, "lastLoginAt": now}},
        )
    else:
        users_collection.insert_one(
            {"googleId": google_id, "email": email, "name": name, "createdAt": now, "lastLoginAt": now, "role": "user"}
        )
        user = users_collection.find_one({"googleId": google_id})

    session["user_id"] = str(user["_id"])
    session["user_name"] = user.get("name", user.get("email", "User"))
    session["google_user"] = {"id": google_id, "email": email, "name": name}

    next_url = session.pop("next", None)
    return redirect(next_url or url_for("index"))


# --------------------------------------------------------
# Forgot / Reset Password (PRODUCTION READY)
# --------------------------------------------------------
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        user = users_collection.find_one({"email": email})

        flash("If that email exists, a reset link has been sent.", "info")

        if user:
            token = uuid.uuid4().hex
            reset_tokens.insert_one(
                {"userId": user["_id"], "token": token, "expiresAt": datetime.utcnow() + timedelta(hours=1), "createdAt": datetime.utcnow()}
            )

            if not APP_BASE_URL:
                reset_link = url_for("reset_password", token=token, _external=True)
            else:
                reset_link = f"{APP_BASE_URL.rstrip('/')}{url_for('reset_password', token=token)}"

            try:
                send_email(
                    to_email=email,
                    subject="Reset your ABTO password",
                    body_text=(
                        "We received a request to reset your password.\n\n"
                        f"Reset link (valid for 1 hour):\n{reset_link}\n\n"
                        "If you did not request this, you can ignore this email."
                    ),
                )
            except Exception as e:
                logger.error("Password reset email failed: %s", e)

        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    rec = reset_tokens.find_one({"token": token})
    if not rec or rec.get("expiresAt") < datetime.utcnow():
        flash("Reset link is invalid or expired.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        new_pw = request.form["password"]

        users_collection.update_one({"_id": rec["userId"]}, {"$set": {"passwordHash": generate_password_hash(new_pw)}})
        reset_tokens.delete_one({"_id": rec["_id"]})

        flash("Password updated. You can log in now.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html")


# --------------------------------------------------------
# Upload Route (CREATE with ownerId) - size hardened
# --------------------------------------------------------
@app.route("/upload", methods=["GET", "POST"])
def upload():
    user = current_user()
    if not user:
        session["next"] = request.path
        return redirect(url_for("login"))

    owner_oid = ObjectId(user["_id"])

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

        # ---------------- VIDEO ----------------
        if street_type == "video":
            if mode not in ["walk", "drive", "fly", "sit"]:
                mode = "walk"

            is_tour_flag = request.form.get("is_tour")
            is_tour = True if is_tour_flag in ("on", "true", "1") else False
            tour_category = clean_text(request.form.get("tour_category"), 80)
            tour_best_time = clean_text(request.form.get("tour_best_time"), 80)

            # ✅ NEW: Thumbnail (optional)
            thumbnail_url = None
            thumb_file = request.files.get("thumbnail")
            if thumb_file and thumb_file.filename:
                if not allowed_thumb(thumb_file.filename):
                    flash("Thumbnail must be JPG / PNG / WEBP.", "error")
                    return redirect(url_for("upload"))

                thumb_file.seek(0, os.SEEK_END)
                thumb_size = thumb_file.tell()
                thumb_file.seek(0)

                if thumb_size > MAX_THUMB_SIZE:
                    flash(f"Thumbnail must be under {MAX_THUMB_SIZE // (1024*1024)}MB", "error")
                    return redirect(url_for("upload"))

                try:
                    thumbnail_url = upload_image_cloudinary(thumb_file)
                except Exception as e:
                    flash("Thumbnail upload failed.", "error")
                    logger.error("Thumbnail upload failed: %s", e)
                    return redirect(url_for("upload"))

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
                            flash(f"Each video must be under {MAX_VIDEO_SIZE // (1024*1024)}MB", "error")
                            return redirect(url_for("upload"))

                        try:
                            url = upload_video_cloudinary(file)
                            video_urls.append(url)
                        except Exception as e:
                            flash("Video upload failed.", "error")
                            logger.error("Video upload failed: %s", e)
                            return redirect(url_for("upload"))

            if links_raw:
                links = [link.strip() for link in links_raw.replace("\n", ",").split(",")]
                video_urls.extend([link for link in links if link])

            if not video_urls:
                flash("Upload one or more videos or paste links!", "error")
                return redirect(url_for("upload"))

            street_doc = {
                "ownerId": owner_oid,
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
                "thumbnail_url": thumbnail_url,  # ✅ NEW FIELD
                "likes": 0,
                "createdAt": datetime.utcnow(),
                "updatedAt": datetime.utcnow(),
                "status": "published",
                "deleted": False,
                "is_tour": is_tour,
                "tour_category": tour_category,
                "tour_best_time": tour_best_time,
            }

        # ---------------- 3D ----------------
        elif street_type == "3d":
            file = request.files.get("model")
            link = request.form.get("model_link")

            glb_url = None

            if file and file.filename:
                file.seek(0, os.SEEK_END)
                size = file.tell()
                file.seek(0)

                if size > MAX_GLB_SIZE:
                    flash(f"GLB file must be under {MAX_GLB_SIZE // (1024*1024)}MB", "error")
                    return redirect(url_for("upload"))

                try:
                    glb_url = upload_glb_supabase(file)
                except Exception as e:
                    flash("GLB upload failed.", "error")
                    logger.error("GLB upload failed: %s", e)
                    return redirect(url_for("upload"))

            elif link:
                glb_url = link.strip()
            else:
                flash("Upload a GLB or paste a model URL", "error")
                return redirect(url_for("upload"))

            street_doc = {
                "ownerId": owner_oid,
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
                "updatedAt": datetime.utcnow(),
                "status": "published",
                "deleted": False,
            }

        else:
            flash("Invalid street type!", "error")
            return redirect(url_for("upload"))

        streets_collection.insert_one(street_doc)
        flash("Street added successfully!", "success")
        return redirect(url_for("dashboard"))

    return render_template("upload.html")



# --------------------------------------------------------
# Dashboard Page (READ only your streets by default)
# --------------------------------------------------------
@app.route("/dashboard")
def dashboard():
    user = current_user()
    if not user:
        session["next"] = request.path
        return redirect(url_for("login"))

    query = {"deleted": False}
    if not is_admin_user(user):
        query["ownerId"] = ObjectId(user["_id"])

    streets = list_with_str_id(streets_collection.find(query))

    for s in streets:
        s["createdAtFmt"] = format_date(s.get("createdAt"))

    total_streets = len(streets)
    total_likes = sum(int(s.get("likes", 0) or 0) for s in streets)
    walk_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "walk")
    drive_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "drive")
    fly_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "fly")
    sit_count = sum(1 for s in streets if s.get("type") == "video" and s.get("mode") == "sit")

    recent_streets = sorted(streets, key=lambda s: s.get("createdAt") or datetime.min, reverse=True)[:8]

    user_info = {
        "name": user.get("name") or user.get("email", "User"),
        "email": user.get("email", ""),
        "createdAtFmt": format_date(user.get("createdAt")),
        "lastLoginFmt": format_date(user.get("lastLoginAt")),
        "role": user.get("role", "user"),
        "is_admin": bool(is_admin_user(user)),
    }

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
        user_info=user_info,
    )


# --------------------------------------------------------
# ✅ ADMIN PAGES
# --------------------------------------------------------
@app.route("/admin")
@admin_required
def admin_dashboard():
    total_users = users_collection.count_documents({})
    total_streets = streets_collection.count_documents({"deleted": False})
    total_deleted = streets_collection.count_documents({"deleted": True})

    recent_logs = list(activity_logs.find({}).sort("timestamp", -1).limit(25))
    recent_logs = make_json_safe(recent_logs)

    return render_template(
        "admin/dashboard.html",
        total_users=total_users,
        total_streets=total_streets,
        total_deleted=total_deleted,
        recent_logs=recent_logs,
    )


@app.route("/admin/users")
@admin_required
def admin_users():
    users = list(users_collection.find({}).sort("createdAt", -1).limit(200))
    users = make_json_safe(users)
    return render_template("admin/users.html", users=users)


@app.route("/admin/streets")
@admin_required
def admin_streets():
    streets = list(streets_collection.find({}).sort("createdAt", -1).limit(300))
    streets = make_json_safe(streets)
    return render_template("admin/streets.html", streets=streets)


@app.post("/admin/user/<user_id>/make-admin")
@admin_required
def admin_make_user_admin(user_id):
    try:
        oid = ObjectId(user_id)
    except InvalidId:
        abort(404)

    users_collection.update_one({"_id": oid}, {"$set": {"role": "admin"}})
    flash("User promoted to admin.", "success")
    return redirect(url_for("admin_users"))


@app.post("/admin/user/<user_id>/make-user")
@admin_required
def admin_make_admin_user_normal(user_id):
    try:
        oid = ObjectId(user_id)
    except InvalidId:
        abort(404)

    users_collection.update_one({"_id": oid}, {"$set": {"role": "user"}})
    flash("Admin changed to user.", "info")
    return redirect(url_for("admin_users"))


# --------------------------------------------------------
# Edit Street (UPDATE – only owner or admin)
# --------------------------------------------------------
@app.route("/street/<street_id>/edit", methods=["GET", "POST"])
def edit_street(street_id):
    user = current_user()
    if not user:
        session["next"] = request.path
        return redirect(url_for("login"))

    try:
        oid = ObjectId(street_id)
    except InvalidId:
        abort(404)

    query = {"_id": oid, "deleted": False}
    if not is_admin_user(user):
        query["ownerId"] = ObjectId(user["_id"])

    street = streets_collection.find_one(query)
    if not street:
        abort(404)

    if request.method == "POST":
        updated_fields = {
            "name": clean_text(request.form.get("name"), 100),
            "city": clean_text(request.form.get("city"), 50),
            "country": clean_text(request.form.get("country"), 50),
            "category": clean_text(request.form.get("category"), 80),
            "description": clean_text(request.form.get("description"), 500),
        }

        if "is_tour" in request.form:
            is_tour_flag = request.form.get("is_tour")
            updated_fields["is_tour"] = True if is_tour_flag in ("on", "true", "1") else False
        if "tour_category" in request.form:
            updated_fields["tour_category"] = clean_text(request.form.get("tour_category"), 80)
        if "tour_best_time" in request.form:
            updated_fields["tour_best_time"] = clean_text(request.form.get("tour_best_time"), 80)

        try:
            lat = float(request.form.get("lat"))
            lng = float(request.form.get("lng"))
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise ValueError
            updated_fields["lat"] = lat
            updated_fields["lng"] = lng
        except Exception:
            flash("Invalid latitude/longitude", "error")
            return redirect(url_for("edit_street", street_id=street_id))

        updated_fields["updatedAt"] = datetime.utcnow()
        streets_collection.update_one({"_id": oid}, {"$set": updated_fields})
        flash("Street updated successfully.", "success")
        return redirect(url_for("dashboard"))

    street["_id"] = str(street["_id"])
    return render_template("edit_street.html", street=street)


# --------------------------------------------------------
# Delete Street (DELETE – only owner or admin, soft delete)
# --------------------------------------------------------
@app.route("/street/<street_id>/delete", methods=["POST"])
def delete_street(street_id):
    user = current_user()
    if not user:
        session["next"] = request.path
        return redirect(url_for("login"))

    try:
        oid = ObjectId(street_id)
    except InvalidId:
        abort(404)

    query = {"_id": oid, "deleted": False}
    if not is_admin_user(user):
        query["ownerId"] = ObjectId(user["_id"])

    result = streets_collection.update_one(
        query,
        {"$set": {"deleted": True, "deletedAt": datetime.utcnow(), "updatedAt": datetime.utcnow()}}
    )

    if result.matched_count == 0:
        abort(404)

    flash("Street deleted.", "info")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------
# Trip Price API  (/api/price)
# --------------------------------------------------------
@app.route("/api/price", methods=["POST"])
def api_price():
    payload = request.get_json(silent=True) or {}

    origin = clean_text(payload.get("origin") or payload.get("from"), 100)
    destination = clean_text(payload.get("destination") or payload.get("to"), 100)

    if not origin or not destination:
        return {"error": "Please enter both origin and destination."}, 400

    try:
        geo_from = geocode_place(origin)
        geo_to = geocode_place(destination)
    except Exception:
        logger.exception("api_price: geocoding failed")
        return {"error": "Could not reach the distance service. Please try again in a moment."}, 502

    if not geo_from or not geo_to:
        return {"error": "We couldn't find one of those places. Try using a nearby big city or airport."}, 404

    distance_km = haversine_km(geo_from["lat"], geo_from["lng"], geo_to["lat"], geo_to["lng"])

    price_info = estimate_price_aed(distance_km)
    distance_text, price_text = build_price_texts(distance_km, price_info)

    return {
        "origin_formatted": geo_from.get("display_name", origin),
        "destination_formatted": geo_to.get("display_name", destination),
        "distance_km": round(distance_km, 1),
        "distance_text": distance_text,
        "price_text": price_text,
    }


# --------------------------------------------------------
# Activity Logging API
# --------------------------------------------------------
@app.route("/api/activity", methods=["POST"])
def log_activity():
    user = current_user()
    if not user:
        return ("", 204)

    data = request.get_json(silent=True) or {}
    event_type = clean_text(data.get("event_type"), 50)
    street_id = data.get("street_id")
    mode = clean_text(data.get("mode"), 10)
    extra = data.get("extra") if isinstance(data.get("extra"), dict) else {}

    try:
        street_oid = ObjectId(street_id) if street_id else None
    except InvalidId:
        street_oid = None

    log_doc = {
        "userId": ObjectId(user["_id"]),
        "streetId": street_oid,
        "eventType": event_type,
        "mode": mode,
        "timestamp": datetime.utcnow(),
        "userAgent": request.headers.get("User-Agent", "")[:200],
        "extra": extra,
    }
    activity_logs.insert_one(log_doc)
    return ("", 204)


@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    user = current_user()
    if not user:
        return {"error": "Unauthorized"}, 401

    days = int(request.args.get("days", "30"))
    since = start_date_for_range(days)

    query_streets = {"deleted": False}
    if not is_admin_user(user):
        query_streets["ownerId"] = ObjectId(user["_id"])

    streets = list(streets_collection.find(query_streets, {
        "name": 1, "city": 1, "country": 1, "type": 1, "mode": 1, "likes": 1,
        "createdAt": 1, "lat": 1, "lng": 1, "status": 1
    }))

    def is_video_mode(doc, m):
        return doc.get("type") == "video" and doc.get("mode") == m

    totals = {
        "total_streets": len(streets),
        "total_likes": sum(int(s.get("likes", 0) or 0) for s in streets),
        "walk_count": sum(1 for s in streets if is_video_mode(s, "walk")),
        "drive_count": sum(1 for s in streets if is_video_mode(s, "drive")),
        "fly_count": sum(1 for s in streets if is_video_mode(s, "fly")),
        "sit_count": sum(1 for s in streets if is_video_mode(s, "sit")),
        "is_admin": bool(is_admin_user(user)),
    }

    logs_query = {}
    if not is_admin_user(user):
        logs_query["userId"] = ObjectId(user["_id"])
    if since:
        logs_query["timestamp"] = {"$gte": since}

    events = list(activity_logs.find(logs_query, {
        "eventType": 1, "streetId": 1, "mode": 1, "timestamp": 1
    }).sort("timestamp", -1).limit(500))

    view_types = set(["view_world", "open_world", "view_street", "open_street"])
    def is_view_event(e):
        et = (e.get("eventType") or "").lower()
        return et in view_types or et.startswith("view") or et.startswith("open")

    day_counts = {}
    for e in events:
        if not is_view_event(e):
            continue
        ts = e.get("timestamp")
        if not ts:
            continue
        day = ts.strftime("%Y-%m-%d")
        day_counts[day] = day_counts.get(day, 0) + 1

    street_map = {}
    streets_safe = []
    for s in streets:
        sid = str(s["_id"])
        street_map[sid] = s
        streets_safe.append({
            "_id": sid,
            "name": s.get("name") or "Untitled",
            "city": s.get("city") or "",
            "country": s.get("country") or "",
            "type": s.get("type"),
            "mode": s.get("mode"),
            "likes": int(s.get("likes", 0) or 0),
            "createdAt": to_iso(s.get("createdAt")),
            "lat": s.get("lat"),
            "lng": s.get("lng"),
            "status": s.get("status", ""),
        })

    view_by_street = {}
    for e in events:
        if not is_view_event(e):
            continue
        sid = e.get("streetId")
        if not sid:
            continue
        key = str(sid)
        view_by_street[key] = view_by_street.get(key, 0) + 1

    top_views = sorted(view_by_street.items(), key=lambda x: x[1], reverse=True)[:8]
    top_views_list = []
    for sid, c in top_views:
        st = street_map.get(sid, {})
        top_views_list.append({
            "streetId": sid,
            "views": c,
            "name": st.get("name", "Unknown"),
            "mode": st.get("mode"),
            "city": st.get("city"),
            "country": st.get("country"),
        })

    top_likes_list = sorted(streets_safe, key=lambda x: x.get("likes", 0), reverse=True)[:8]

    recent = []
    for e in events[:25]:
        sid = str(e.get("streetId")) if e.get("streetId") else None
        st = street_map.get(sid, {})
        recent.append({
            "eventType": e.get("eventType"),
            "mode": e.get("mode") or st.get("mode"),
            "timestamp": to_iso(e.get("timestamp")),
            "streetId": sid,
            "streetName": st.get("name"),
            "city": st.get("city"),
            "country": st.get("country"),
        })

    labels, data = [], []
    if since:
        for i in range(days - 1, -1, -1):
            d = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
            labels.append(d)
            data.append(day_counts.get(d, 0))
    else:
        for d in sorted(day_counts.keys()):
            labels.append(d)
            data.append(day_counts[d])

    return {
        "totals": totals,
        "views_chart": {"labels": labels, "data": data},
        "recent": recent,
        "top_views": top_views_list,
        "top_likes": top_likes_list,
        "streets": streets_safe,
        "user": {
            "name": user.get("name") or user.get("email", "User"),
            "email": user.get("email", ""),
            "role": user.get("role", "user"),
            "is_admin": bool(is_admin_user(user)),
            "createdAt": to_iso(user.get("createdAt")),
            "lastLoginAt": to_iso(user.get("lastLoginAt")),
        }
    }


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
# Start Server (production ready)
# --------------------------------------------------------
if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=debug)
