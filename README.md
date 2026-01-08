# Virtual Street Walk (Flask)

A simple Flask + MongoDB app to upload and explore street videos (walk/drive/fly/sit) and optional 3D GLB streets.

## Quick start (local)
1) Create and fill `.env` (see `.env.example` below)
2) Install deps:
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```
3) Run:
```bash
python app.py
```
Open http://127.0.0.1:5000

## Production notes
- Set `FLASK_SECRET_KEY` in your environment (do not hardcode it).
- Consider enabling authentication (admin-only uploads), rate limiting for `/like/<id>`, and file size limits.
- For large videos: consider using HLS (`.m3u8`) delivery and streaming-friendly encoding.

## .env.example
```ini
FLASK_SECRET_KEY=change-me

MONGO_URI=mongodb+srv://...

CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...

SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_BUCKET=streetwalk
SUPABASE_SERVICE_KEY=...

# Upload limit in bytes (default 1GB)
MAX_CONTENT_LENGTH=1073741824
```
