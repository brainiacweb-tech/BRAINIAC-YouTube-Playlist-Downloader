import os, sys, uuid, threading, queue, json, time, zipfile, shutil, re, ipaddress, subprocess
from datetime import timedelta
from functools import lru_cache
import requests as _requests
from bs4 import BeautifulSoup as _BS
# Suppress InsecureRequestWarning from verify=False in direct HTTP downloads
import urllib3 as _urllib3
_urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)
from urllib.parse import urlparse

# ╔══════════════════════════════════════════════════════════════════╗
# ║  ⚠  LOCKED — DO NOT REMOVE static-ffmpeg  ⚠                     ║
# ║  Required for MP3 extraction, DASH merging, MP4 remux on Railway ║
# ║  Removing this breaks ALL audio/video postprocessing.            ║
# ╚══════════════════════════════════════════════════════════════════╝
_FFMPEG_LOCATION = None
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()          # adds bundled ffmpeg/ffprobe to PATH
    import shutil as _shutil
    _FFMPEG_LOCATION = _shutil.which("ffmpeg") or None
except Exception:
    pass  # will use system ffmpeg if available
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth
import yt_dlp
from flask import Flask, render_template, request, jsonify, Response, send_file, send_from_directory, redirect, session, url_for, make_response
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_compress import Compress

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "brainiac-yt-dl-secret-key-2026"
# ── Session persistence: keep users logged in across browser closes / server restarts ─
app.config["SESSION_PERMANENT"]            = True
app.config["PERMANENT_SESSION_LIFETIME"]   = timedelta(days=30)
app.config["SESSION_COOKIE_SAMESITE"]      = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"]      = True
# ── Remember-me cookie (stored in browser, survives server restarts) ──────────────────
app.config["REMEMBER_COOKIE_DURATION"]     = timedelta(days=30)
app.config["REMEMBER_COOKIE_HTTPONLY"]     = True
app.config["REMEMBER_COOKIE_SAMESITE"]     = "Lax"
app.config["REMEMBER_COOKIE_SECURE"]       = True   # Railway is always HTTPS
# Trust Cloudflare / Railway proxy headers so url_for(_external=True) uses https + real hostname
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ── Database ──────────────────────────────────────────────────────────────────
# On Railway: add the MySQL plugin and it will set MYSQL_URL automatically.
# Locally: falls back to a SQLite file.
_raw_db_url = os.environ.get("MYSQL_URL") or os.environ.get("DATABASE_URL")
if _raw_db_url:
    # Railway provides mysql:// — SQLAlchemy needs mysql+pymysql://
    _db_uri = _raw_db_url.replace("mysql://", "mysql+pymysql://", 1)
else:
    _DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.db")
    _db_uri = f"sqlite:///{_DB_PATH}"
app.config["SQLALCHEMY_DATABASE_URI"] = _db_uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# SQLite doesn't support connection-pool options; only apply them for MySQL
if _raw_db_url:
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_recycle":  280,   # recycle before MySQL's wait_timeout (usually 300 s)
        "pool_pre_ping": True,  # test connection health before each use
        "pool_size":     10,    # max persistent connections per worker
        "max_overflow":  20,    # extra connections allowed under load
    }
else:
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,  # still useful for SQLite file-locking detection
    }
db = SQLAlchemy(app)

# ── Auth ──────────────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = ""

class User(UserMixin, db.Model):
    id                  = db.Column(db.Integer, primary_key=True)
    username            = db.Column(db.String(80), unique=True, nullable=False)
    email               = db.Column(db.String(120), unique=True, nullable=False)
    password            = db.Column(db.String(256), nullable=False)
    google_id           = db.Column(db.String(128), unique=True, nullable=True)
    avatar              = db.Column(db.String(512), nullable=True)
    gdrive_token        = db.Column(db.String(512), nullable=True)
    gdrive_refresh      = db.Column(db.Text, nullable=True)
    plan                = db.Column(db.String(20), nullable=False, default="free")
    plan_expires        = db.Column(db.String(20), nullable=True)

class DailyDownload(db.Model):
    """Tracks how many downloads a user has done today."""
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    date_str   = db.Column(db.String(10), nullable=False)
    count      = db.Column(db.Integer, nullable=False, default=0)
    __table_args__ = (db.UniqueConstraint("user_id", "date_str", name="uq_user_date"),)

class AppSetting(db.Model):
    """Key-value store for app-wide settings that must survive restarts."""
    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)

# ── Plan definitions ──────────────────────────────────────────────────────────
PLAN_LIMITS = {
    #         daily_downloads  max_quality  batch_playlist
    "free":  {"daily": 999999, "quality": "Best Quality", "batch": True},
    "plus":  {"daily": 999999, "quality": "Best Quality", "batch": True},
    "pro":   {"daily": 999999, "quality": "Best Quality", "batch": True},
}

def _get_user_plan(user) -> str:
    """Return active plan, auto-downgrade to free if expired."""
    if not user or not user.is_authenticated:
        return "free"
    plan = user.plan or "free"
    if plan != "free" and user.plan_expires:
        from datetime import datetime as _dt
        try:
            exp = _dt.strptime(user.plan_expires, "%Y-%m-%d").date()
            if _dt.utcnow().date() > exp:
                return "free"
        except Exception:
            pass
    return plan

def _check_daily_limit(user) -> tuple[bool, int, int]:
    """Returns (allowed, used_today, daily_limit)."""
    from datetime import datetime as _dt
    plan  = _get_user_plan(user)
    limit = PLAN_LIMITS[plan]["daily"]
    if not user or not user.is_authenticated:
        return True, 0, limit
    today = _dt.utcnow().strftime("%Y-%m-%d")
    row = DailyDownload.query.filter_by(user_id=user.id, date_str=today).first()
    used = row.count if row else 0
    return (used < limit), used, limit

def _increment_daily(user):
    from datetime import datetime as _dt
    if not user or not user.is_authenticated:
        return
    today = _dt.utcnow().strftime("%Y-%m-%d")
    row = DailyDownload.query.filter_by(user_id=user.id, date_str=today).first()
    if row:
        row.count += 1
    else:
        row = DailyDownload(user_id=user.id, date_str=today, count=1)
        db.session.add(row)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

def _quality_allowed(user, quality: str) -> bool:
    """Check if the requested quality is within the user's plan."""
    plan     = _get_user_plan(user)
    max_qual = PLAN_LIMITS[plan]["quality"]
    order    = ["360p", "480p", "720p", "1080p", "4K", "Best Quality"]
    def _rank(q):
        q = q.strip()
        if q in order:
            return order.index(q)
        if q in ("Audio Only (MP3)",):
            return -1
        return len(order)
    return _rank(quality) <= _rank(max_qual)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

with app.app_context():
    try:
        db.create_all()
    except Exception:
        db.session.rollback()
    for _col, _ddl in [
        ("google_id",      "ALTER TABLE user ADD COLUMN google_id VARCHAR(128) UNIQUE"),
        ("avatar",         "ALTER TABLE user ADD COLUMN avatar VARCHAR(512)"),
        ("gdrive_token",   "ALTER TABLE user ADD COLUMN gdrive_token VARCHAR(512)"),
        ("gdrive_refresh", "ALTER TABLE user ADD COLUMN gdrive_refresh TEXT"),
        ("plan",           "ALTER TABLE user ADD COLUMN plan VARCHAR(20) NOT NULL DEFAULT 'free'"),
        ("plan_expires",   "ALTER TABLE user ADD COLUMN plan_expires VARCHAR(20)"),
    ]:
        try:
            with db.engine.connect() as _conn:
                _conn.execute(db.text(_ddl))
                _conn.commit()
        except Exception:
            pass

# ── Google OAuth ──────────────────────────────────────────────────────────────
oauth = OAuth(app)
google_oauth = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    access_token_url="https://oauth2.googleapis.com/token",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
    client_kwargs={
        "scope": "openid email profile https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/youtube",
    },
)

# ── Security config ───────────────────────────────────────────────────────────
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024   # 5 MB max upload / request body

# ── Compression ──────────────────────────────────────────────────────────────
app.config["COMPRESS_MIMETYPES"] = [
    "text/html", "text/css", "text/javascript", "application/javascript",
    "application/json", "text/plain", "text/xml",
]
app.config["COMPRESS_LEVEL"] = 6   # gzip level 6 — good balance of speed vs size
app.config["COMPRESS_MIN_SIZE"] = 500  # don't compress tiny responses
Compress(app)

# Rate limiter — keyed by real client IP (Railway passes X-Forwarded-For)
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "60 per hour"],
    headers_enabled=True,
)

# ── Security headers on every response ───────────────────────────────────────
@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"]        = "SAMEORIGIN"
    resp.headers["X-XSS-Protection"]       = "1; mode=block"
    resp.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"]     = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
        "font-src 'self' https://fonts.gstatic.com https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://accounts.google.com; "
        "frame-src https://www.youtube.com https://www.youtube-nocookie.com https://w.soundcloud.com; "
        "frame-ancestors 'self' http://127.0.0.1:* http://localhost:*;"
    )
    # Remove fingerprinting headers
    resp.headers.pop("Server", None)
    resp.headers.pop("X-Powered-By", None)
    return resp

# ── SSRF / URL firewall ───────────────────────────────────────────────────────
_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

def _validate_url(url: str) -> str | None:
    """Return an error string if URL is invalid/dangerous, else None."""
    if not url:
        return "URL is required."
    if len(url) > 8192:
        return "URL too long."
    try:
        p = urlparse(url)
    except Exception:
        return "Malformed URL."
    if p.scheme not in ("http", "https"):
        return "Only http/https URLs are allowed."
    hostname = p.hostname or ""
    # Block non-media pages (search results, image viewers, etc.)
    _non_media_patterns = [
        (r"google\.com", r"/(search|imgres|imghp)"),
        (r"bing\.com",   r"/search"),
        (r"yahoo\.com",  r"/search"),
    ]
    for host_pat, path_pat in _non_media_patterns:
        if re.search(host_pat, hostname, re.I) and re.match(path_pat, p.path or "/", re.I):
            return "This looks like a search results page, not a media URL. Please paste a direct link to the video or audio."
    # Block localhost names
    if re.match(r"^(localhost|.*\.local)$", hostname, re.I):
        return "Access to local addresses is not allowed."
    # Block internal IPs
    try:
        addr = ipaddress.ip_address(hostname)
        if any(addr in net for net in _BLOCKED_NETS):
            return "Access to internal/private addresses is not allowed."
    except ValueError:
        pass   # hostname — not an IP literal, fine
    return None


def _validate_query(q: str) -> str | None:
    if not q:
        return "Query is required."
    if len(q) > 1000:
        return "Search query too long (max 1000 chars)."
    return None

# ── YouTube Data API v3 ───────────────────────────────────────────────────────
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
_YT_API_BASE    = "https://www.googleapis.com/youtube/v3"

def _iso_duration(iso: str) -> tuple[int, str]:
    """Convert ISO 8601 duration (PT1H2M3S) → (total_seconds, 'H:MM:SS')."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0, ""
    h, mn, s = (int(m.group(i) or 0) for i in (1, 2, 3))
    secs = h * 3600 + mn * 60 + s
    dur_str = f"{h}:{mn:02d}:{s:02d}" if h else f"{mn}:{s:02d}"
    return secs, dur_str


def _yt_api_search(query: str, mode: str) -> list | None:
    """Search YouTube via Data API v3. Returns up to 200 results (4 pages) or None on failure."""
    if not YOUTUBE_API_KEY:
        return None
    try:
        # 1. Paginate search to collect up to 200 video IDs (4 pages × 50)
        items      = []
        page_token = None
        for _ in range(4):   # max 4 pages = 200 results
            params = {
                "part": "snippet", "q": query, "type": "video",
                "maxResults": 50, "key": YOUTUBE_API_KEY,
            }
            if page_token:
                params["pageToken"] = page_token
            r    = _requests.get(f"{_YT_API_BASE}/search", params=params, timeout=10)
            resp = r.json()
            items.extend(resp.get("items") or [])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if not items:
            return []

        # 2. Fetch duration for all IDs in batches of 50
        vid_ids_all = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
        details: dict = {}
        for i in range(0, len(vid_ids_all), 50):
            batch = ",".join(vid_ids_all[i:i + 50])
            vr = _requests.get(f"{_YT_API_BASE}/videos", params={
                "part": "contentDetails", "id": batch, "key": YOUTUBE_API_KEY,
            }, timeout=10)
            details.update({v["id"]: v for v in (vr.json().get("items") or [])})

        results = []
        bps = 32_000 if mode == "music" else 250_000
        for it in items:
            vid_id = (it.get("id") or {}).get("videoId", "")
            if not vid_id:
                continue
            snip    = it.get("snippet") or {}
            iso_dur = ((details.get(vid_id) or {}).get("contentDetails") or {}).get("duration", "")
            dur_sec, dur_str = _iso_duration(iso_dur)
            thumbs  = snip.get("thumbnails") or {}
            thumb   = (thumbs.get("maxres") or thumbs.get("high") or
                       thumbs.get("medium") or thumbs.get("default") or {}).get("url") or \
                      f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"
            results.append({
                "url":       f"https://www.youtube.com/watch?v={vid_id}",
                "title":     snip.get("title") or "Unknown",
                "duration":  dur_str,
                "uploader":  snip.get("channelTitle") or "",
                "thumbnail": thumb,
                "filesize":  int(dur_sec * bps) if dur_sec else 0,
            })
        return results
    except Exception:
        return None


# ── Third-party site scrapers ────────────────────────────────────────────────
_SCRAPE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/122.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

def _trendybeatz_search(query: str, limit: int = 20) -> list:
    """Search TrendyBeatz and return track results."""
    import re as _re
    try:
        url = f"https://trendybeatz.com/?s={_requests.utils.quote(query)}"
        r   = _requests.get(url, headers=_SCRAPE_HEADERS, timeout=15)
        soup = _BS(r.text, "html.parser")
        results = []
        _skip_kw = ['song-of-the-day', 'songs-of-the-week', 'musics', 'djmix',
                    'albums', 'artists', 'category', 'page/', '#']
        seen = set()
        for a in soup.find_all('a', href=True):
            href = a.get('href', '')
            if '/download-mp3/' not in href:
                continue
            if href in seen or any(k in href for k in _skip_kw):
                continue
            seen.add(href)
            txt = a.get_text(separator=' ', strip=True)
            for junk in ['Rating:', 'Download', 'Stream', 'Featuring:']:
                txt = txt.replace(junk, '')
            txt = _re.sub(r'\s{2,}', ' ', txt).strip()
            if not txt:
                continue
            parent = a.find_parent(['div', 'li', 'article'])
            img = parent.find('img') if parent else None
            thumb = ''
            if img:
                thumb = img.get('src') or img.get('data-src') or img.get('data-lazy-src') or ''
            results.append({
                "url":       href,
                "title":     txt,
                "duration":  "",
                "uploader":  "TrendyBeatz",
                "thumbnail": thumb,
                "filesize":  0,
            })
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []


def _mdundo_search(query: str, limit: int = 20) -> list:
    """Search Mdundo via Bing site-search (page is JS-rendered, no public API)."""
    try:
        bing_url = ("https://www.bing.com/search?q=site%3Amdundo.com+"
                    + _requests.utils.quote(query))
        hdrs = dict(_SCRAPE_HEADERS)
        hdrs['Accept'] = 'text/html,application/xhtml+xml'
        r = _requests.get(bing_url, headers=hdrs, timeout=15)
        soup = _BS(r.text, "html.parser")
        results = []
        seen = set()
        for a in soup.find_all('a', href=True):
            href = a.get('href', '')
            # Bing wraps results in /ck/a?... redirects — also catch direct links
            target = href
            if 'mdundo.com' not in target:
                continue
            # Skip Bing UI noise
            if any(x in target for x in ['bing.com', 'microsoft.com', 'msn.com']):
                continue
            # Only song pages
            if not any(p in target for p in ['/song/', '/songs/', '/a/', '/music/']):
                continue
            if target in seen:
                continue
            seen.add(target)
            title = a.get_text(separator=' ', strip=True)
            if not title or len(title) < 2:
                continue
            results.append({
                "url":       target,
                "title":     title,
                "duration":  "",
                "uploader":  "Mdundo",
                "thumbnail": "",
                "filesize":  0,
            })
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []


def _moviebox_search(query: str, limit: int = 20) -> list:
    """Search MovieBox via the h5-api.aoneroom.com JSON API (POST)."""
    try:
        api_url = 'https://h5-api.aoneroom.com/wefeed-h5api-bff/subject/search'
        hdrs = {
            'User-Agent': _SCRAPE_HEADERS['User-Agent'],
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
            'Origin': 'https://moviebox.ph',
            'Referer': 'https://moviebox.ph/',
        }
        r = _requests.post(
            api_url,
            json={'keyword': query, 'pageNo': 1, 'pageSize': limit},
            headers=hdrs,
            timeout=15,
        )
        data = r.json()
        items = data.get('data', {}).get('items') or []
        results = []
        for item in items:
            detail_path = item.get('detailPath') or f"/movies/detail/{item.get('subjectId', '')}"
            page_url = 'https://moviebox.ph' + detail_path
            dur_sec = item.get('duration') or 0
            if dur_sec:
                m, s = divmod(int(dur_sec) // 60, 60)
                dur_str = f"{m}:{s:02d}"
            else:
                dur_str = ''
            results.append({
                "url":       page_url,
                "title":     item.get('title', ''),
                "duration":  dur_str,
                "uploader":  "MovieBox",
                "thumbnail": item.get('cover', ''),
                "filesize":  0,
            })
            if len(results) >= limit:
                break
        return results
    except Exception:
        return []


def _yt_api_prefetch(url: str) -> dict | None:
    """Fetch playlist/video metadata via YouTube Data API v3."""
    if not YOUTUBE_API_KEY:
        return None
    try:
        from urllib.parse import parse_qs, urlparse as _up
        qs          = parse_qs(_up(url).query)
        playlist_id = (qs.get("list") or [""])[0]
        video_id    = (qs.get("v") or [""])[0]
        # youtu.be/VIDEO_ID short links
        if not video_id and "youtu.be" in url:
            video_id = _up(url).path.lstrip("/")

        if playlist_id:
            pr = _requests.get(f"{_YT_API_BASE}/playlists", params={
                "part": "snippet,contentDetails", "id": playlist_id, "key": YOUTUBE_API_KEY,
            }, timeout=10)
            pl_items = pr.json().get("items") or []
            if not pl_items:
                return None
            pl    = pl_items[0]
            count = (pl.get("contentDetails") or {}).get("itemCount") or 0
            snip  = pl.get("snippet") or {}
            thumbs = snip.get("thumbnails") or {}
            thumb  = (thumbs.get("maxres") or thumbs.get("high") or
                      thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
            return {"title": snip.get("title") or url, "count": count,
                    "uploader": snip.get("channelTitle") or "", "duration": 0, "filesize": 0,
                    "thumbnail": thumb}

        if video_id:
            vr = _requests.get(f"{_YT_API_BASE}/videos", params={
                "part": "snippet,contentDetails", "id": video_id, "key": YOUTUBE_API_KEY,
            }, timeout=10)
            v_items = vr.json().get("items") or []
            if not v_items:
                return None
            v       = v_items[0]
            snip    = v.get("snippet") or {}
            dur_sec, _ = _iso_duration((v.get("contentDetails") or {}).get("duration", ""))
            thumbs = snip.get("thumbnails") or {}
            thumb  = (thumbs.get("maxres") or thumbs.get("high") or
                      thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
            return {"title": snip.get("title") or url, "count": 1,
                    "uploader": snip.get("channelTitle") or "", "duration": dur_sec, "filesize": 0,
                    "thumbnail": thumb}
    except Exception:
        return None

IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
STATIC_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

@app.route("/images/<path:filename>")
def serve_image(filename):
    resp = send_from_directory(IMAGES_DIR, filename)
    resp.headers["Cache-Control"] = "public, max-age=86400"  # cache images 24h
    return resp

# ── PWA: serve manifest + service worker from root scope ─────────────────────
@app.route("/manifest.json")
def serve_manifest():
    resp = send_from_directory(STATIC_DIR, "manifest.json")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp

@app.route("/sw.js")
def serve_sw():
    resp = send_from_directory(STATIC_DIR, "sw.js")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Content-Type"]  = "application/javascript"
    return resp

DOWNLOAD_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_downloads")
os.makedirs(DOWNLOAD_BASE, exist_ok=True)

# ── Verify ffmpeg is available ────────────────────────────────────────────────
def _find_ffmpeg():
    import shutil as _shutil
    ff = _shutil.which("ffmpeg")
    if ff:
        print(f"[startup] ffmpeg found: {ff}")
    else:
        print("[startup] WARNING: ffmpeg not found in PATH — audio extraction will fail!")
    return ff
_FFMPEG_PATH = _find_ffmpeg()


# ── Search result cache ───────────────────────────────────────────────────────
_search_cache: dict = {}      # key: (query, source, mode) → (timestamp, results)
_search_cache_lock = threading.Lock()
SEARCH_CACHE_TTL = 900        # seconds — cache search results for 15 minutes

def _cache_get(key):
    with _search_cache_lock:
        entry = _search_cache.get(key)
    if entry and (time.time() - entry[0]) < SEARCH_CACHE_TTL:
        return entry[1]
    return None

def _cache_set(key, value):
    with _search_cache_lock:
        _search_cache[key] = (time.time(), value)
        # Evict oldest entries if cache grows too large
        if len(_search_cache) > 200:
            oldest = sorted(_search_cache, key=lambda k: _search_cache[k][0])[:50]
            for k in oldest:
                _search_cache.pop(k, None)

# ── Per-task state ────────────────────────────────────────────────────────────
_tasks: dict = {}
_tasks_lock = threading.Lock()


def _create_task(task_id: str):
    with _tasks_lock:
        _tasks[task_id] = {
            "queue":  queue.Queue(),
            "status": "running",
            "files":  [],
            "zip":    None,
            "error":  None,
        }


def _get_task(task_id: str):
    with _tasks_lock:
        return _tasks.get(task_id)


def _push(task_id: str, data: dict):
    t = _get_task(task_id)
    if t:
        t["queue"].put(data)


# ── yt-dlp helpers ────────────────────────────────────────────────────────────
class _YtLogger:
    """Forward yt-dlp messages to the SSE stream so users see real errors."""
    def __init__(self, task_id: str):
        self._tid = task_id
        self.errors: list[str] = []

    # Developer/internal messages we never want to surface to end users
    _SUPPRESS_WARNINGS = (
        "UNPLAYABLE",          # android_creator/embedded dev-only notice
        "unplayable",
        "Unsupported client",
        "unsupported client",
        "tv_embedded",
        "No title found in player",
        "Sleeping",
        "Skipping",
    )
    _SUPPRESS_DEBUG = (
        "[debug]",
        "Sleeping",
        "retrying in",          # internal retry countdown noise
        "YouTube bot",
        "bot-check",
    )

    def debug(self, msg: str):
        for skip in self._SUPPRESS_DEBUG:
            if skip in msg:
                return
        _push(self._tid, {"type": "log", "msg": msg, "level": "info"})

    def warning(self, msg: str):
        for skip in self._SUPPRESS_WARNINGS:
            if skip in msg:
                return  # hide developer-only / internal notices from users
        _push(self._tid, {"type": "log", "msg": f"⚠  {msg}", "level": "warn"})

    def error(self, msg: str):
        self.errors.append(msg)
        _push(self._tid, {"type": "log", "msg": f"✘  {msg}", "level": "err"})


def _make_hook(task_id: str):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done  = d.get("downloaded_bytes", 0)
            pct   = int(done / total * 100) if total else 0
            _push(task_id, {
                "type":     "progress",
                "pct":      pct,
                "speed":    (d.get("_speed_str") or "").strip(),
                "eta":      (d.get("_eta_str")   or "").strip(),
                "filename": os.path.basename(d.get("filename", "")),
            })
        elif d["status"] == "finished":
            _push(task_id, {
                "type":  "log",
                "msg":   f"✔  Done: {os.path.basename(d.get('filename',''))}",
                "level": "ok",
            })
    return hook


def _build_opts(task_id: str, task_dir: str, quality: str, mode: str, yt_token: str = "", playlist_cap: int = 0) -> dict:
    logger = _YtLogger(task_id)
    _headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "*/*",
    }
    # If the user authenticated with Google, use their OAuth token so YouTube
    # recognises them as a signed-in user — bypasses bot/sign-in prompts.
    if yt_token:
        _headers["Authorization"] = f"Bearer {yt_token}"

    opts = {
        "logger":           logger,
        "progress_hooks":   [_make_hook(task_id)],
        "outtmpl":          os.path.join(task_dir, "%(title)s.%(ext)s"),
        "noplaylist":       mode == "direct",   # for direct tab: single item only
        "retries":          10,
        "fragment_retries": 10,
        "ignoreerrors":     mode in ("playlist",),  # only skip errors in playlists
        "no_color":         True,
        # Don't pre-validate format URLs — android pre-signed URLs can fail HEAD checks
        "check_formats":    False,

        # ── Anti-block: look like a real browser + YouTube OAuth if available ─
        "http_headers": _headers,

        # ── Geo / age-gate bypass ─────────────────────────────────────────────
        "geo_bypass":              True,
        "geo_bypass_country":      "US",
        "age_limit":               99,    # bypass all age gates
        # ── TLS: ignore cert errors (some CDNs have odd certs) ────────────────
        "nocheckcertificate":      True,

        # Best no-cookie clients for yt-dlp 2026:
        # android_vr  — pre-signed URLs, no PO token needed, no DRM
        # web_creator — YT Studio API, JS challenge solved by yt-dlp-ejs
        # web         — standard web, universal fallback
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "web_creator", "web"],
            },
            "twitter": {"api": ["syndication"]},
        },

        # ── Socket patience ───────────────────────────────────────────────────
        "socket_timeout": 60,

        # ── Rate-limit avoidance: pause between consecutive requests ──────────
        "sleep_interval_requests": 1,
        "sleep_interval":          1,

        # ── Extra resilience ─────────────────────────────────────────────────
        "extractor_retries": 3,
    }
    if _FFMPEG_LOCATION:
        opts["ffmpeg_location"] = os.path.dirname(_FFMPEG_LOCATION)

    if quality == "Audio Only (MP3)":
        # m4a first — android_vr pre-signed streams are m4a
        opts["format"] = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best[ext=m4a]/best"
        opts["postprocessors"] = [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "256",
        }]
    elif quality == "Best Quality":
        opts["format"] = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio"
            "/bestvideo[ext=webm]+bestaudio[ext=webm]"
            "/bestvideo+bestaudio"
            "/best[ext=mp4]/best"
        )
    elif quality in ("1080p", "720p", "480p", "360p"):
        h = quality.replace("p", "")
        opts["format"] = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h}][ext=mp4]+bestaudio"
            f"/bestvideo[height<={h}][ext=webm]+bestaudio[ext=webm]"
            f"/bestvideo[height<={h}]+bestaudio"
            f"/best[height<={h}][ext=mp4]"
            f"/best[height<={h}]"
            f"/best"
        )
    else:
        opts["format"] = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio"
            "/bestvideo[ext=webm]+bestaudio[ext=webm]"
            "/bestvideo+bestaudio"
            "/best[ext=mp4]/best"
        )

    # Merge DASH splits to mp4
    if quality != "Audio Only (MP3)":
        opts["merge_output_format"] = "mp4"

    # No playlist cap — all users download full playlists

    return opts, logger


def _http_fallback_download(task_id: str, url: str, task_dir: str, extra_headers: dict | None = None) -> str:
    """
    Universal HTTP downloader — handles ANY file from ANY source.
    Streams with progress, auto-detects filename & extension, follows redirects,
    rotates User-Agent, handles auth challenges gracefully.
    Returns saved filename on success, raises on failure.
    """
    import urllib.parse, mimetypes, time as _time

    _push(task_id, {"type": "log", "msg": "🌐  Downloading via HTTP…", "level": "info"})

    # Rich browser headers — maximise compatibility with CDNs, APKs, game stores, etc.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",   # no gzip so chunk sizes reflect real bytes
        "Connection": "keep-alive",
        "Referer": "/".join(url.split("/")[:3]) + "/",   # origin as referer
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    if extra_headers:
        headers.update(extra_headers)

    # Twitter / X image+video CDN needs specific headers to avoid HTML redirect
    import urllib.parse as _urlp
    _dl_host = _urlp.urlparse(url).netloc.lower()
    if "twimg.com" in _dl_host:
        headers.update({
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://twitter.com/",
            "Sec-Fetch-Dest": "image",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        })

    session = _requests.Session()
    session.max_redirects = 15
    resp = session.get(url, headers=headers, stream=True, timeout=90,
                       allow_redirects=True, verify=False)
    resp.raise_for_status()

    # ── Reject HTML pages — they are web pages, not downloadable files ────────
    _resp_ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if _resp_ct in ("text/html", "text/xhtml", "text/xhtml+xml", "application/xhtml+xml"):
        raise ValueError(
            "This URL points to a web page (HTML), not a downloadable file. "
            "No video or file could be extracted. Try a direct file or CDN link."
        )

    # ── Filename resolution (priority order) ─────────────────────────────────
    filename = None

    # 1. Content-Disposition header (RFC 6266 — handles UTF-8 encoded names)
    cd = resp.headers.get("Content-Disposition", "")
    if cd:
        # RFC 5987: filename*=UTF-8''encoded%20name
        m = re.search(r"filename\*\s*=\s*UTF-8''([^\s;]+)", cd, re.I)
        if m:
            filename = urllib.parse.unquote(m.group(1))
        if not filename:
            m = re.search(r'filename\s*=\s*["\']?([^"\';\r\n]+)', cd, re.I)
            if m:
                filename = m.group(1).strip().strip('"\'')

    # 2. Final URL path after redirects
    if not filename:
        final_url = resp.url
        parsed    = urllib.parse.urlparse(final_url)
        path_part = urllib.parse.unquote(parsed.path)
        # Try query param "file", "filename", "name", "f", "fn"
        qs = urllib.parse.parse_qs(parsed.query)
        for key in ("file", "filename", "name", "fn", "f", "title"):
            if key in qs:
                filename = urllib.parse.unquote(qs[key][0])
                break
        if not filename:
            filename = os.path.basename(path_part.rstrip("/")) or "download"

    # 3. Append extension from Content-Type if filename has none
    base, ext = os.path.splitext(os.path.basename(filename))
    if not ext:
        ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        _ct_map = {
            "video/mp4": ".mp4", "video/webm": ".webm", "video/x-matroska": ".mkv",
            "video/quicktime": ".mov", "video/x-msvideo": ".avi", "video/3gpp": ".3gp",
            "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/ogg": ".ogg",
            "audio/wav": ".wav", "audio/flac": ".flac", "audio/aac": ".aac",
            "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
            "image/webp": ".webp", "image/avif": ".avif", "image/bmp": ".bmp",
            "application/pdf": ".pdf", "application/zip": ".zip",
            "application/x-rar-compressed": ".rar", "application/x-7z-compressed": ".7z",
            "application/x-tar": ".tar", "application/gzip": ".tar.gz",
            "application/vnd.android.package-archive": ".apk",
            "application/octet-stream": "",   # keep as-is; may be anything
            "application/x-msdownload": ".exe",
            "application/x-www-form-urlencoded": "",
            "text/plain": ".txt", "text/csv": ".csv",
        }
        guessed = _ct_map.get(ct) or mimetypes.guess_extension(ct) or ""
        if guessed == ".jpe":
            guessed = ".jpg"
        filename = base + guessed if guessed else base or "download"

    # Sanitise — allow Unicode (international filenames), strip only forbidden chars
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).strip(". ")
    if not filename:
        filename = "download"
    filepath = os.path.join(task_dir, filename)

    # ── Stream with real-time speed & progress ────────────────────────────────
    total      = int(resp.headers.get("Content-Length", 0))
    downloaded = 0
    start_ts   = _time.monotonic()
    last_push  = start_ts

    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(chunk_size=512 * 1024):   # 512 KB chunks
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                now     = _time.monotonic()
                elapsed = now - start_ts or 0.001
                speed   = downloaded / elapsed            # bytes/sec
                pct     = round(downloaded / total * 100, 1) if total else 0

                # Throttle pushes to ~4 per second
                if now - last_push >= 0.25 or (total and pct >= 100):
                    last_push = now
                    def _fmt_speed(bps):
                        if bps > 1_000_000: return f"{bps/1_000_000:.1f} MB/s"
                        if bps > 1_000:     return f"{bps/1_000:.0f} KB/s"
                        return f"{bps:.0f} B/s"
                    eta = ""
                    if total and speed:
                        remaining = (total - downloaded) / speed
                        m, s = divmod(int(remaining), 60)
                        eta = f"{m}:{s:02d}" if m else f"{s}s"
                    _push(task_id, {
                        "type":     "progress",
                        "pct":      pct,
                        "speed":    _fmt_speed(speed),
                        "eta":      eta,
                        "filename": filename,
                    })

    _push(task_id, {"type": "log", "msg": f"✔  Done: {filename}", "level": "ok"})
    return filename


# Domains where yt-dlp is useless — go straight to OG / page scraping
_IMAGE_PLATFORM_HOSTS = {
    "pinterest.com", "www.pinterest.com", "pin.it",
    "pinterest.co.uk", "pinterest.fr", "pinterest.de",
    "pinterest.ca", "pinterest.es", "pinterest.pt",
    "flickr.com", "www.flickr.com",
    "500px.com", "www.500px.com",
    "unsplash.com", "www.unsplash.com",
    "pexels.com", "www.pexels.com",
    "pixabay.com", "www.pixabay.com",
    "imgur.com", "www.imgur.com",
    "giphy.com", "www.giphy.com",
    "tenor.com", "www.tenor.com",
    "gfycat.com", "www.gfycat.com",
}


def _is_image_platform(url: str) -> bool:
    import urllib.parse as _uip
    return _uip.urlparse(url).netloc.lower() in _IMAGE_PLATFORM_HOSTS


def _upgrade_image_url(u: str) -> str:
    """Upgrade CDN thumbnail URLs to highest available resolution."""
    if not u:
        return u
    if "pinimg.com" in u:
        u = re.sub(r'/\d+x\d*/', '/originals/', u)
    if "staticflickr.com" in u or "live.staticflickr.com" in u:
        u = re.sub(r'_(m|n|w|z|c|b)(\.jpg)$', r'_b\2', u)
    if "i.imgur.com" in u:
        u = re.sub(r'(https://i\.imgur\.com/[A-Za-z0-9]+)[shbtlm](\.(?:jpg|jpeg|png|gif|webp))$',
                   r'\1\2', u)
    return u


def _try_og_media_download(task_id: str, page_url: str, task_dir: str) -> bool:
    """
    Comprehensive HTML page media extractor. Finds ALL media via:
    1. OG / Twitter Card meta tags
    2. JSON-LD structured data
    3. Site-specific JSON stores (Pinterest __PWS_DATA__, etc.)
    4. Raw CDN URL scan (pinimg, redd.it, imgur, etc.)
    5. Any https URL ending in a known media extension
    Downloads every found item. Returns True if at least one file was saved.
    """
    import json as _json
    import urllib.parse as _uparse4

    _push(task_id, {"type": "log",
                    "msg": "🔍  Scanning page for all media (images, videos, files)…",
                    "level": "info"})
    try:
        _pg_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Upgrade-Insecure-Requests": "1",
        }
        _pg_resp = _requests.get(page_url, headers=_pg_headers, timeout=25,
                                 allow_redirects=True)
        html = _pg_resp.text
    except Exception as _pg_ex:
        _push(task_id, {"type": "log",
                        "msg": f"⚠️  Could not fetch page: {_pg_ex}", "level": "warn"})
        return False

    found_urls: list[tuple[str, str]] = []   # (url, "image"|"video"|"file")
    seen: set[str] = set()

    def _add(raw: str, label: str):
        u = _upgrade_image_url(raw.strip().split('"')[0].split("'")[0])
        if not u or not u.startswith("http"):
            return
        # Deduplicate by path (ignore query for CDN hosts)
        _key = u.split("?")[0] if any(cdn in u for cdn in
               ("pinimg.com", "staticflickr.com", "redd.it", "imgur.com")) else u
        if _key in seen:
            return
        seen.add(_key)
        found_urls.append((u, label))

    # ── 1. ALL OG / Twitter Card meta tags ───────────────────────────────────
    _IMAGE_PROPS = {
        "og:image", "og:image:url", "og:image:secure_url",
        "twitter:image", "twitter:image:src",
        "twitter:image0", "twitter:image1", "twitter:image2", "twitter:image3",
    }
    _VIDEO_PROPS = {
        "og:video", "og:video:url", "og:video:secure_url",
        "og:video:stream", "twitter:player:stream",
    }
    _meta_p1 = re.compile(
        r'<meta[^>]+(?:property|name)\s*=\s*["\']([^"\']+)["\'][^>]+content\s*=\s*["\']([^"\']*)["\']',
        re.I | re.S)
    _meta_p2 = re.compile(
        r'<meta[^>]+content\s*=\s*["\']([^"\']*)["\'][^>]+(?:property|name)\s*=\s*["\']([^"\']+)["\']',
        re.I | re.S)
    for _m in _meta_p1.finditer(html):
        _prop, _ct = _m.group(1).strip().lower(), _m.group(2)
        if _prop in _IMAGE_PROPS:   _add(_ct, "image")
        elif _prop in _VIDEO_PROPS: _add(_ct, "video")
    for _m in _meta_p2.finditer(html):
        _ct, _prop = _m.group(1), _m.group(2).strip().lower()
        if _prop in _IMAGE_PROPS:   _add(_ct, "image")
        elif _prop in _VIDEO_PROPS: _add(_ct, "video")

    # ── 2. JSON-LD structured data ────────────────────────────────────────────
    for _jld_raw in re.findall(
            r'<script[^>]+type\s*=\s*["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
            html, re.I):
        try:
            _jld = _json.loads(_jld_raw)
            _items = _jld if isinstance(_jld, list) else [_jld]
            for _item in _items:
                if not isinstance(_item, dict):
                    continue
                for _key in ("image", "thumbnailUrl", "contentUrl",
                             "thumbnail", "video", "videoUrl", "url"):
                    _val = _item.get(_key)
                    if isinstance(_val, str) and _val.startswith("http"):
                        _lbl = "video" if "video" in _key.lower() else "image"
                        _add(_val, _lbl)
                    elif isinstance(_val, (list, dict)):
                        _lst = _val if isinstance(_val, list) else [_val]
                        for _v in _lst:
                            _u2 = (_v if isinstance(_v, str) else
                                   (_v.get("url") or _v.get("contentUrl") or ""))
                            if isinstance(_u2, str) and _u2.startswith("http"):
                                _add(_u2, "image")
        except Exception:
            pass

    # ── 3. Pinterest: extract from __PWS_DATA__ / __PWS_INITIAL_STORE__ ──────
    if "pinimg.com" in html or "pinterest" in page_url.lower():
        # Scan raw HTML for all pinimg.com media URLs
        for _pu in re.findall(r'https://i\.pinimg\.com/[^\s"\'<>\\]+', html):
            _add(_pu, "image")
        for _vu in re.findall(r'https://v\.pinimg\.com/[^\s"\'<>\\]+', html):
            _add(_vu, "video")
        # Also parse the embedded JSON store if present
        for _pws_raw in re.findall(
                r'(?:__PWS_DATA__|__PWS_INITIAL_STORE__|__REDUX_STATE__)\s*=\s*(\{.+?\});\s*</script>',
                html, re.S):
            try:
                _pws_str = _json.dumps(_json.loads(_pws_raw))
                for _pu2 in re.findall(r'https://i\\.pinimg\\.com/[^"\\\\]+', _pws_str):
                    _add(_pu2, "image")
                for _vu2 in re.findall(r'https://v\\.pinimg\\.com/[^"\\\\]+', _pws_str):
                    _add(_vu2, "video")
            except Exception:
                pass

    # ── 4. Reddit CDN ─────────────────────────────────────────────────────────
    for _ru in re.findall(r'https://(?:preview|i)\.redd\.it/[^\s"\'<>\\]+', html):
        _add(_ru, "image")

    # ── 5. Imgur CDN ──────────────────────────────────────────────────────────
    for _iu in re.findall(r'https://i\.imgur\.com/[A-Za-z0-9]+\.[a-z]{2,4}', html):
        _add(_iu, "image")

    # ── 6. Any https URL with a known media extension ─────────────────────────
    for _gu in re.findall(
            r'https://[^\s"\'<>\\]+\.(?:jpg|jpeg|png|gif|webp|avif|mp4|webm|mov|mkv|mp3|m4a)'
            r'(?:\?[^\s"\'<>\\]*)?',
            html):
        _lbl = "video" if re.search(r'\.(mp4|webm|mov|mkv)(\?|$)', _gu, re.I) else "image"
        _add(_gu, _lbl)

    if not found_urls:
        _push(task_id, {"type": "log",
                        "msg": "ℹ️  No media found on this page.", "level": "warn"})
        return False

    _push(task_id, {"type": "log",
                    "msg": f"✅  Found {len(found_urls)} media item(s) — downloading all…",
                    "level": "info"})
    saved = 0
    for idx, (media_url, label) in enumerate(found_urls, 1):
        short = media_url[:80] + ("…" if len(media_url) > 80 else "")
        _push(task_id, {"type": "log",
                        "msg": f"[{idx}/{len(found_urls)}] 📥  {label}: {short}",
                        "level": "info"})
        try:
            _http_fallback_download(task_id, media_url, task_dir)
            saved += 1
        except Exception as _dl_ex:
            _push(task_id, {"type": "log",
                            "msg": f"⚠️  Skipped ({_dl_ex})", "level": "warn"})
    return saved > 0


def _try_twitter_media_download(task_id: str, tweet_url: str, task_dir: str) -> bool:
    """
    Use the fxtwitter JSON API to get all media (images + videos) from a tweet
    and download each one directly.  Returns True if at least one file was saved.
    Works for image tweets, video tweets, and mixed-media threads.
    """
    import re as _re2

    # Extract tweet ID from any Twitter/X/fxtwitter URL variant
    m = _re2.search(r'(?:status|i/status)[/\\]+(\d+)', tweet_url)
    if not m:
        return False
    tweet_id = m.group(1)

    api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
    _push(task_id, {"type": "log",
                    "msg": "🐦  Checking tweet media via fxtwitter API…",
                    "level": "info"})
    try:
        api_resp = _requests.get(api_url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (compatible; BRAINIAC/1.0)"
        })
        api_resp.raise_for_status()
        data = api_resp.json()
    except Exception as e:
        _push(task_id, {"type": "log",
                        "msg": f"⚠️  fxtwitter API failed: {e}",
                        "level": "warn"})
        return False

    tweet = data.get("tweet") or {}
    media = tweet.get("media") or {}
    all_urls = []

    # Photos — upgrade to original quality
    for photo in (media.get("photos") or []):
        img_url = photo.get("url") or ""
        if img_url:
            if "name=" in img_url:
                img_url = _re2.sub(r'([?&])name=[^&]*', r'\1name=orig', img_url)
            elif "?" in img_url:
                img_url += "&name=orig"
            else:
                img_url += "?name=orig"
            all_urls.append(img_url)

    # Videos / GIFs — pick highest bitrate variant
    for video in (media.get("videos") or []):
        # fxtwitter may give a "url" directly or a "variants" list
        variants = video.get("variants") or []
        if variants:
            best = max(variants, key=lambda v: v.get("bitrate", 0))
            vid_url = best.get("url") or ""
        else:
            vid_url = video.get("url") or ""
        if vid_url:
            all_urls.append(vid_url)

    if not all_urls:
        _push(task_id, {"type": "log",
                        "msg": "⚠️  No media found in this tweet (text-only, private, or deleted).",
                        "level": "warn"})
        return False

    _push(task_id, {"type": "log",
                    "msg": f"📸  Found {len(all_urls)} media item(s) — downloading…",
                    "level": "info"})
    saved = 0
    for media_url in all_urls:
        try:
            _http_fallback_download(task_id, media_url, task_dir)
            saved += 1
        except Exception as dl_err:
            _push(task_id, {"type": "log",
                            "msg": f"⚠️  Could not download {media_url}: {dl_err}",
                            "level": "warn"})
    return saved > 0


def _normalize_direct_url(url: str) -> str:
    """
    Rewrite common sharing URLs into direct-download URLs before attempting
    any download. Handles Dropbox, Google Drive, GitHub, and Twitter/X.
    """
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    host   = parsed.netloc.lower()

    # ── Twitter / X ───────────────────────────────────────────────────────
    # x.com/i/status/<ID>  →  fxtwitter.com/i/status/<ID>
    # (fxtwitter is a proxy that exposes proper video URLs for yt-dlp)
    if host in ("x.com", "www.x.com", "twitter.com", "www.twitter.com"):
        url = url.replace("https://x.com", "https://fxtwitter.com", 1)\
                 .replace("https://www.x.com", "https://fxtwitter.com", 1)\
                 .replace("https://twitter.com", "https://fxtwitter.com", 1)\
                 .replace("https://www.twitter.com", "https://fxtwitter.com", 1)
        return url

    # ── Dropbox ───────────────────────────────────────────────────────────
    # ?dl=0  → ?dl=1   (force download rather than preview)
    if "dropbox.com" in host:
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        qs["dl"] = ["1"]
        new_query = urllib.parse.urlencode({k: v[0] for k, v in qs.items()})
        url = urllib.parse.urlunparse(parsed._replace(query=new_query))

    # ── Google Drive ──────────────────────────────────────────────────────
    # /file/d/<ID>/view  →  /uc?export=download&id=<ID>&confirm=t
    elif "drive.google.com" in host:
        m = re.search(r"/(?:file/d|open\?id=)([A-Za-z0-9_-]{25,})", url)
        if m:
            fid = m.group(1)
            url = f"https://drive.google.com/uc?export=download&id={fid}&confirm=t&authuser=0"

    # ── GitHub release / raw ──────────────────────────────────────────────
    # github.com/<owner>/<repo>/blob/<branch>/<path>
    # → raw.githubusercontent.com/<owner>/<repo>/<branch>/<path>
    elif host in ("github.com", "www.github.com"):
        m = re.match(r"/([^/]+)/([^/]+)/blob/(.+)", parsed.path)
        if m:
            url = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"

    # ── Twitter image/video CDN ───────────────────────────────────────────
    # pbs.twimg.com/media/<id>?format=jpg&name=large  (image CDN)
    # Use ?name=orig to get highest quality
    elif "twimg.com" in host:
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        qs["name"] = ["orig"]
        new_query = urllib.parse.urlencode({k: v[0] for k, v in qs.items()})
        url = urllib.parse.urlunparse(parsed._replace(query=new_query))

    return url


# Extensions that strongly indicate a plain file — skip yt-dlp entirely
_DIRECT_EXTS = {
    # archives
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst", ".lz4", ".lzma",
    # executables / installers
    ".exe", ".msi", ".pkg", ".deb", ".rpm", ".appimage", ".dmg",
    ".apk", ".ipa", ".msix", ".xapk",
    # documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".epub", ".mobi", ".azw", ".azw3",
    ".txt", ".rtf", ".md",
    # images
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".avif",
    ".tiff", ".tif", ".ico", ".svg", ".heic", ".heif", ".raw",
    ".psd", ".ai", ".xcf",
    # raw media files NOT on a media platform
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v",
    ".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".wma", ".opus",
    ".ts", ".mts", ".m2ts",
    # data / code
    ".csv", ".json", ".xml", ".sqlite", ".db", ".sql",
    ".iso", ".img", ".bin", ".torrent",
    # fonts
    ".ttf", ".otf", ".woff", ".woff2",
    # 3D / game
    ".fbx", ".obj", ".glb", ".gltf", ".stl",
}


def _looks_like_direct_file(url: str) -> bool:
    """Return True if the URL path ends with a known file extension,
    OR if the query string contains format=<image/media> (e.g. Twitter CDN)."""
    import urllib.parse
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower().rstrip("/")
    _, ext = os.path.splitext(path)
    if ext in _DIRECT_EXTS:
        return True
    # Twitter CDN: ?format=jpg  / ?format=png / ?format=gif / ?format=webp
    qs = urllib.parse.parse_qs(parsed.query)
    fmt = (qs.get("format") or qs.get("fmt") or [""])[0].lower()
    if fmt in ("jpg", "jpeg", "png", "gif", "webp", "avif", "bmp",
               "mp4", "webm", "mkv", "mov", "m4v"):
        return True
    # twimg.com CDN is always a direct media file
    if "twimg.com" in parsed.netloc.lower():
        return True
    return False


# Domains that only serve HTML pages — HTTP fallback will never yield a media file
_SOCIAL_MEDIA_HOSTS = {
    "fxtwitter.com", "x.com", "twitter.com",
    "instagram.com", "www.instagram.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
    "facebook.com", "www.facebook.com", "fb.watch",
    "reddit.com", "www.reddit.com", "old.reddit.com",
    "youtube.com", "www.youtube.com", "youtu.be",
    "vimeo.com", "www.vimeo.com",
    "twitch.tv", "www.twitch.tv", "clips.twitch.tv",
    "bilibili.com", "www.bilibili.com",
    "dailymotion.com", "www.dailymotion.com",
}

def _is_social_media_page(url: str) -> bool:
    """Return True if URL is a known social platform page (HTTP fallback useless)."""
    import urllib.parse
    host = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
    return any(host == h or host.endswith("." + h) for h in _SOCIAL_MEDIA_HOSTS)


def _run_download(task_id: str, data: dict):
    url          = data.get("url", "").strip()
    quality      = data.get("quality", "Best Quality")
    mode         = data.get("mode", "playlist")
    yt_token     = data.get("yt_token", "")
    playlist_cap = data.get("_playlist_cap", 0)

    task_dir = os.path.join(DOWNLOAD_BASE, task_id)
    os.makedirs(task_dir, exist_ok=True)
    logger = None   # may be set later in yt-dlp paths

    try:
        _push(task_id, {"type": "log", "msg": "⏳  Starting download…", "level": "info"})

        # ── Direct mode: smart dual-strategy ─────────────────────────────────
        if mode == "direct":
            # 1. Normalise URL (Dropbox, Google Drive, GitHub raw)
            norm_url = _normalize_direct_url(url)
            if norm_url != url:
                _push(task_id, {"type": "log",
                                "msg": f"🔗  Rewritten URL: {norm_url}", "level": "info"})
                url = norm_url

            # 2. Decide strategy:
            #    a) Known file extension  → HTTP immediately
            #    b) Quick HEAD says non-HTML content-type → HTTP immediately
            #    c) Looks like a media page (HTML / known platform) → yt-dlp first
            use_http_first = _looks_like_direct_file(url)

            if not use_http_first:
                # Probe with HEAD to check Content-Type — fast, no body downloaded
                try:
                    _head_headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                        "Accept": "*/*",
                    }
                    _head_sess = _requests.Session()
                    _head_sess.max_redirects = 10
                    _hresp = _head_sess.head(url, headers=_head_headers, timeout=10,
                                             allow_redirects=True, verify=False)
                    _ct = _hresp.headers.get("Content-Type", "").lower()
                    # If the server returns a real file (not an HTML page), use HTTP
                    if _ct and "text/html" not in _ct and "text/xml" not in _ct:
                        use_http_first = True
                        _push(task_id, {"type": "log",
                                        "msg": f"📄  Detected file type: {_ct.split(';')[0].strip()} — downloading directly…",
                                        "level": "info"})
                except Exception:
                    pass  # HEAD failed — proceed with normal logic

            http_succeeded = False
            ytdlp_tried    = False

            # ── Platform shortcuts ────────────────────────────────────────────
            _orig_url = data.get("url", "").strip()
            _is_tweet = any(h in _orig_url.lower() for h in
                            ("x.com/", "twitter.com/", "fxtwitter.com/"))
            _is_img_platform = _is_image_platform(_orig_url)

            # Twitter/X → fxtwitter API (all photos + videos)
            if _is_tweet:
                _push(task_id, {"type": "log",
                                "msg": "🐦  Twitter/X link detected — fetching all media…",
                                "level": "info"})
                _tw_success = _try_twitter_media_download(task_id, _orig_url, task_dir)
                if _tw_success:
                    http_succeeded = True
                else:
                    # Nothing found (private/deleted tweet)
                    user_msg = ("No media found in this tweet. "
                                "It may be text-only, private, or deleted.")
                    with _tasks_lock:
                        _tasks[task_id]["status"] = "error"
                        _tasks[task_id]["error"]  = user_msg
                    _push(task_id, {"type": "error", "msg": user_msg})
                    return

            # Pinterest / Flickr / Imgur / image platforms → OG scraper directly
            if not http_succeeded and _is_img_platform and not use_http_first:
                _push(task_id, {"type": "log",
                                "msg": "🖼️  Image platform detected — scanning for all media…",
                                "level": "info"})
                _og_img = _try_og_media_download(task_id, _orig_url, task_dir)
                if _og_img:
                    http_succeeded = True
                else:
                    user_msg = ("No downloadable media found on this page. "
                                "The content may require login or be private.")
                    with _tasks_lock:
                        _tasks[task_id]["status"] = "error"
                        _tasks[task_id]["error"]  = user_msg
                    _push(task_id, {"type": "error", "msg": user_msg})
                    return

            if not http_succeeded and use_http_first:
                _push(task_id, {"type": "log",
                                "msg": "📥  Direct file detected — downloading via HTTP…",
                                "level": "info"})
                try:
                    _http_fallback_download(task_id, url, task_dir)
                    http_succeeded = True
                except Exception as http_ex:
                    _push(task_id, {"type": "log",
                                    "msg": f"⚠️  HTTP attempt failed ({http_ex}), trying yt-dlp…",
                                    "level": "warn"})

            # 3. yt-dlp path (for media pages, or as fallback after HTTP failed)
            if not http_succeeded and not _is_tweet and not _is_img_platform:
                ytdlp_tried = True
                opts, logger = _build_opts(task_id, task_dir, quality, mode,
                                           yt_token=yt_token, playlist_cap=playlist_cap)
                ytdlp_failed = False
                _old_rlimit = sys.getrecursionlimit()
                sys.setrecursionlimit(500)
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        ydl.download([url])
                except RecursionError:
                    ytdlp_failed = True
                except Exception as ex:
                    ytdlp_failed = True
                finally:
                    sys.setrecursionlimit(_old_rlimit)

                # 4. If yt-dlp failed OR produced no files → OG scraper → HTTP fallback
                files_so_far = [
                    f for f in os.listdir(task_dir)
                    if os.path.isfile(os.path.join(task_dir, f)) and not f.endswith(".part")
                ]
                if ytdlp_failed or not files_so_far:
                    # Step A: try Open Graph / meta-tag scraping (images + videos from
                    # Pinterest, Instagram, Reddit, Facebook, etc.)
                    og_success = _try_og_media_download(task_id, url, task_dir)
                    if og_success:
                        http_succeeded = True
                    else:
                        # Step B: direct HTTP download (works for raw file URLs)
                        try:
                            _http_fallback_download(task_id, url, task_dir)
                            http_succeeded = True
                        except ValueError as html_ex:
                            user_msg = str(html_ex)
                            with _tasks_lock:
                                _tasks[task_id]["status"] = "error"
                                _tasks[task_id]["error"]  = user_msg
                            _push(task_id, {"type": "error", "msg": user_msg})
                            return
                        except Exception as http_ex2:
                            user_msg = (
                                f"Download failed: could not retrieve this URL via "
                                f"yt-dlp or direct HTTP ({http_ex2}). "
                                "If this is a protected file, try pasting a direct download link."
                            )
                            with _tasks_lock:
                                _tasks[task_id]["status"] = "error"
                                _tasks[task_id]["error"]  = user_msg
                            _push(task_id, {"type": "error", "msg": user_msg})
                            return

        # ── Playlist / Song / Video modes ─────────────────────────────────────
        else:
            opts, logger = _build_opts(task_id, task_dir, quality, mode,
                                       yt_token=yt_token, playlist_cap=playlist_cap)
            _old_rlimit2 = sys.getrecursionlimit()
            sys.setrecursionlimit(500)
            _ytdlp_failed   = False
            _ytdlp_user_msg = ""
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except RecursionError:
                _ytdlp_failed   = True
                _ytdlp_user_msg = ("Download failed: this page is not a supported media source "
                                   "(yt-dlp recursion loop). Please paste a direct link to the video or audio.")
            except (yt_dlp.utils.DownloadError, yt_dlp.utils.UnsupportedError) as ex:
                _ytdlp_failed = True
                error_msg = str(ex)
                if ("Error code: 152" in error_msg or "Error code: 183" in error_msg
                      or ("unavailable" in error_msg.lower() and "Watch video on YouTube" in error_msg)):
                    _ytdlp_user_msg = ("Download failed: This video has embedding disabled and cannot be "
                                       "downloaded from the server. Open the video on YouTube and try "
                                       "downloading it from there, or use the Direct tab with the video URL.")
                elif "region-locked" in error_msg or "not available in your country" in error_msg:
                    _ytdlp_user_msg = "Download failed: This video is not available in the server's region."
                elif "HTTP Error 429" in error_msg or "Access Denied" in error_msg:
                    _ytdlp_user_msg = "Download failed: YouTube is rate-limiting this server. Please try again in a few minutes."
                else:
                    _ytdlp_user_msg = f"Download failed: {error_msg}"
            finally:
                sys.setrecursionlimit(_old_rlimit2)

            if _ytdlp_failed:
                with _tasks_lock:
                    _tasks[task_id]["status"] = "error"
                    _tasks[task_id]["error"]  = _ytdlp_user_msg
                _push(task_id, {"type": "error", "msg": _ytdlp_user_msg})
                return

        files = [f for f in os.listdir(task_dir)
                 if os.path.isfile(os.path.join(task_dir, f)) and not f.endswith(".part")]
        if not files:
            captured = "; ".join(logger.errors[-3:]) if (logger and logger.errors) else "no details captured"
            user_msg = f"No files were downloaded. Error details: {captured}"
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["error"] = user_msg
            _push(task_id, {"type": "error", "msg": user_msg})
            return

        if len(files) == 1:
            with _tasks_lock:
                _tasks[task_id]["files"]  = files
                _tasks[task_id]["status"] = "done"
            with open(os.path.join(task_dir, "task.json"), "w") as f:
                json.dump({"status": "done", "files": files, "zip": None}, f)
            # ── Auto-save to user's persistent folder ──────────────────────
            _autosave_file(data, os.path.join(task_dir, files[0]))
            _push(task_id, {"type": "done", "filename": files[0], "count": 1})
        else:
            zip_name = "playlist.zip"
            zip_path = os.path.join(task_dir, zip_name)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in files:
                    zf.write(os.path.join(task_dir, f), f)
            with _tasks_lock:
                _tasks[task_id]["files"]  = files
                _tasks[task_id]["zip"]    = zip_name
                _tasks[task_id]["status"] = "done"
            with open(os.path.join(task_dir, "task.json"), "w") as f:
                json.dump({"status": "done", "files": files, "zip": zip_name}, f)
            # ── Auto-save zip to user's persistent folder ──────────────────
            _autosave_file(data, zip_path)
            _push(task_id, {"type": "done", "filename": zip_name, "count": len(files)})

    except RecursionError:
        user_msg = "Download failed: yt-dlp hit a recursion loop on this URL. This usually means the page is not a supported media source (e.g. a Google Images or search result page). Please paste a direct link to the video or audio."
        with _tasks_lock:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["error"]  = user_msg
        _push(task_id, {"type": "error", "msg": user_msg})
    except Exception as ex:
        with _tasks_lock:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["error"]  = str(ex)
        _push(task_id, {"type": "error", "msg": str(ex)})


def _autosave_file(data: dict, src_path: str):
    """Copy a completed download to the user's persistent saved folder."""
    user_id = data.get("_user_id")
    if not user_id or not os.path.isfile(src_path):
        return
    saved_dir = os.path.join(DOWNLOAD_BASE, "saved", str(user_id))
    os.makedirs(saved_dir, exist_ok=True)
    orig_name = os.path.basename(src_path)
    base, ext = os.path.splitext(orig_name)
    dst = os.path.join(saved_dir, orig_name)
    counter = 1
    while os.path.exists(dst):
        dst = os.path.join(saved_dir, f"{base} ({counter}){ext}")
        counter += 1
    try:
        shutil.copy2(src_path, dst)
    except Exception:
        pass  # never crash the download thread over a save failure


def _schedule_cleanup(task_dir: str, task_id: str, delay: int = 300):
    """Delete task folder and memory entry after `delay` seconds."""
    def _clean():
        time.sleep(delay)
        shutil.rmtree(task_dir, ignore_errors=True)
        with _tasks_lock:
            _tasks.pop(task_id, None)
    threading.Thread(target=_clean, daemon=True).start()


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route("/auth/google")
def google_login():
    cb = os.environ.get("GOOGLE_REDIRECT_URI") or url_for("google_callback", _external=True)
    return google_oauth.authorize_redirect(cb, access_type="offline", prompt="consent")


@app.route("/auth/google/callback")
def google_callback():
    try:
        token = google_oauth.authorize_access_token()
    except Exception:
        return redirect("/login")

    userinfo  = token.get("userinfo") or {}
    google_id = str(userinfo.get("sub", ""))
    email     = userinfo.get("email", "").lower().strip()
    name      = userinfo.get("name", "")
    picture   = userinfo.get("picture", "")

    if not google_id or not email:
        return redirect("/login")

    from sqlalchemy import or_
    user = User.query.filter(
        or_(User.google_id == google_id, User.email == email)
    ).first()

    if user:
        changed = False
        if not user.google_id:
            user.google_id = google_id; changed = True
        if picture and user.avatar != picture:
            user.avatar = picture; changed = True
        if changed:
            db.session.commit()
    else:
        base = re.sub(r"[^a-zA-Z0-9]", "", name or email.split("@")[0])[:20] or "user"
        existing = {u.username for u in User.query.filter(
            User.username.like(f"{base}%")
        ).with_entities(User.username).all()}
        username, n = base, 1
        while username in existing:
            username = f"{base}{n}"; n += 1
        user = User(
            username  = username,
            email     = email,
            password  = generate_password_hash(os.urandom(24).hex()),
            google_id = google_id,
            avatar    = picture or None,
        )
        db.session.add(user)
        db.session.commit()

    _yt_access  = token.get("access_token", "")
    _yt_refresh = token.get("refresh_token", "")
    if _yt_access:
        user.gdrive_token = _yt_access
    if _yt_refresh:
        user.gdrive_refresh = _yt_refresh
    if _yt_access or _yt_refresh:
        db.session.commit()

    logout_user()
    session.clear()
    login_user(user, remember=True)
    if user.gdrive_token:
        session["gdrive_token"] = user.gdrive_token
    return redirect("/app")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect("/app")
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")
        if not username or not email or not password:
            error = "All fields are required."
        elif len(username) < 3:
            error = "Username must be at least 3 characters."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."
        elif User.query.filter_by(username=username).first():
            error = "Username already taken."
        elif User.query.filter_by(email=email).first():
            error = "Email already registered."
        else:
            user = User(
                username=username,
                email=email,
                password=generate_password_hash(password)
            )
            db.session.add(user)
            db.session.commit()
            login_user(user, remember=True)
            return redirect("/app")
    return render_template("signup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect("/app")
    error = None
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password   = request.form.get("password", "")
        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier.lower())
        ).first()
        if user and check_password_hash(user.password, password):
            login_user(user, remember=True)
            if user.gdrive_token:
                session["gdrive_token"] = user.gdrive_token
            return redirect("/app")
        error = "Invalid username/email or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    logout_user()
    session.clear()
    session.permanent = False
    resp = make_response(redirect("/login"))
    # Explicitly expire both cookies — required because REMEMBER_COOKIE_SECURE=True
    # means the browser only accepts the delete directive when Secure is also present.
    resp.delete_cookie("remember_token", path="/", secure=True,  samesite="Lax")
    resp.delete_cookie("session",        path="/", secure=False, samesite="Lax")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/api/me")
@login_required
def api_me():
    resp = make_response(jsonify({
        "username": current_user.username,
        "avatar":   current_user.avatar or "",
        "email":    current_user.email,
    }))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


@app.route("/api/db-status")
def api_db_status():
    """Health endpoint — returns MySQL/SQLite reachability."""
    try:
        user_count = User.query.count()
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        db_type = "mysql" if "mysql" in db_uri else "sqlite"
        resp = make_response(jsonify({
            "db_type":    db_type,
            "reachable":  True,
            "user_count": user_count,
            "error":      None,
            "note":       db_type.upper() + " connected — data is persistent.",
        }))
    except Exception as ex:
        resp = make_response(jsonify({
            "db_type":   "unknown",
            "reachable": False,
            "error":     str(ex),
            "note":      "Database unreachable.",
        }))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/privacy")
def privacy():
    resp = render_template("privacy.html")
    return resp, 200, {"Cache-Control": "public, max-age=3600"}

@app.route("/terms")
def terms():
    resp = render_template("terms.html")
    return resp, 200, {"Cache-Control": "public, max-age=3600"}

@app.route("/pricing")
def pricing():
    resp = render_template("pricing.html")
    return resp, 200, {"Cache-Control": "public, max-age=3600"}

@app.route("/stats")
def stats():
    return render_template("stats.html"), 200, {"Cache-Control": "no-cache"}

# \u2500\u2500 Plan status API \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
@app.route("/api/plan-status")
@login_required
def api_plan_status():
    from datetime import datetime as _dt
    plan    = _get_user_plan(current_user)
    limits  = PLAN_LIMITS[plan]
    today   = _dt.utcnow().strftime("%Y-%m-%d")
    row     = DailyDownload.query.filter_by(user_id=current_user.id, date_str=today).first()
    used    = row.count if row else 0
    return jsonify({
        "plan":          plan,
        "plan_expires":  current_user.plan_expires,
        "daily_limit":   limits["daily"],
        "used_today":    used,
        "remaining":     max(0, limits["daily"] - used),
        "max_quality":   limits["quality"],
        "batch":         limits["batch"],
    })

@app.route("/api/admin/set-plan", methods=["POST"])
@login_required
def api_admin_set_plan():
    """Admin-only: manually upgrade/downgrade a user's plan."""
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    if not admin_secret:
        return jsonify({"error": "Admin not configured"}), 403
    data   = request.get_json(force=True) or {}
    secret = data.get("secret", "")
    if secret != admin_secret:
        return jsonify({"error": "Forbidden"}), 403
    email   = (data.get("email") or "").strip().lower()
    plan    = (data.get("plan") or "free").strip().lower()
    expires = data.get("expires")
    if plan not in PLAN_LIMITS:
        return jsonify({"error": f"Unknown plan: {plan}"}), 400
    u = User.query.filter_by(email=email).first()
    if not u:
        return jsonify({"error": "User not found"}), 404
    u.plan         = plan
    u.plan_expires = expires
    try:
        db.session.commit()
        return jsonify({"ok": True, "email": email, "plan": plan, "expires": expires})
    except Exception as ex:
        db.session.rollback()
        return jsonify({"error": str(ex)}), 500

@app.route("/")
def landing():
    if current_user.is_authenticated:
        return redirect("/app")
    resp = render_template("landing.html")
    return resp, 200, {"Cache-Control": "public, max-age=300"}

# Google Search Console domain verification
_GSITE_TOKEN = os.environ.get("GOOGLE_SITE_VERIFY", "gFXOPMcc20rPxHnQwiIZHtrRALzPJdNO_-em6L0nD2M")
@app.route("/google<token>.html")
def google_site_verify(token):
    if token == _GSITE_TOKEN:
        return f"google-site-verification: google{token}.html", 200, {"Content-Type": "text/plain"}
    return "", 404

@app.route("/mobile-preview")
def mobile_preview():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mobile Preview</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1a1a2e;display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100vh;font-family:sans-serif}
h2{color:#aaa;font-size:12px;letter-spacing:2px;text-transform:uppercase;margin-bottom:18px}
.device-bar{display:flex;gap:12px;margin-bottom:18px}
.device-btn{background:#2a2a3e;color:#aaa;border:1px solid #444;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px;transition:.2s}
.device-btn.active,.device-btn:hover{background:#e53935;color:#fff;border-color:#e53935}
.phone-wrap{position:relative;background:#000;border-radius:44px;padding:14px 12px;box-shadow:0 0 0 2px #333,0 0 0 4px #222,0 20px 60px rgba(0,0,0,.8)}
.phone-wrap::before{content:'';position:absolute;top:14px;left:50%;transform:translateX(-50%);width:80px;height:6px;background:#222;border-radius:4px;z-index:10}
.phone-wrap::after{content:'';position:absolute;bottom:10px;left:50%;transform:translateX(-50%);width:40px;height:5px;background:#333;border-radius:4px}
iframe{display:block;border:none;border-radius:34px;background:#fff}
.tablet-wrap{background:#000;border-radius:20px;padding:20px 14px;box-shadow:0 0 0 2px #333,0 20px 60px rgba(0,0,0,.8)}
.tablet-wrap iframe{border-radius:10px}
</style>
</head>
<body>
<h2>Mobile Preview</h2>
<div class="device-bar">
  <button class="device-btn active" onclick="setDevice('iphone')">iPhone 15</button>
  <button class="device-btn" onclick="setDevice('android')">Android</button>
  <button class="device-btn" onclick="setDevice('tablet')">iPad</button>
</div>
<div id="wrap" class="phone-wrap">
  <iframe id="frame" src="/app" width="390" height="844" scrolling="yes"></iframe>
</div>
<script>
const devices={
  iphone:{w:390,h:844,cls:'phone-wrap'},
  android:{w:360,h:780,cls:'phone-wrap'},
  tablet:{w:768,h:1024,cls:'tablet-wrap'}
};
function setDevice(d){
  const f=document.getElementById('frame');
  const wrap=document.getElementById('wrap');
  const dev=devices[d];
  f.width=dev.w; f.height=dev.h;
  wrap.className=dev.cls;
  document.querySelectorAll('.device-btn').forEach(b=>b.classList.remove('active'));
  event.target.classList.add('active');
}
</script>
</body>
</html>"""
    return html

@app.route("/app")
@login_required
@limiter.limit("120 per minute")
def index():
    from datetime import datetime as _dt
    plan   = _get_user_plan(current_user)
    today  = _dt.utcnow().strftime("%Y-%m-%d")
    row    = DailyDownload.query.filter_by(user_id=current_user.id, date_str=today).first()
    used   = row.count if row else 0
    limits = PLAN_LIMITS[plan]
    resp = make_response(render_template(
        "index.html",
        username=current_user.username,
        avatar=current_user.avatar,
        email=current_user.email,
        user_plan=plan,
        plan_used=used,
        plan_limit=limits["daily"],
        plan_max_quality=limits["quality"],
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/search", methods=["POST"])
@limiter.limit("30 per minute")
def search():
    data   = request.get_json(force=True) or {}
    query  = (data.get("query") or "").strip()
    source = data.get("source", "YouTube")
    mode   = (data.get("mode") or "music").strip()

    err = _validate_query(query)
    if err:
        return jsonify({"error": err}), 400

    if source not in ("YouTube", "SoundCloud", "Dailymotion", "Audiomack",
                      "TrendyBeatz", "Mdundo", "MovieBox"):
        source = "YouTube"

    # ── Scraped sources — no yt-dlp needed for search ────────────────────────
    scraper_map = {
        "TrendyBeatz": _trendybeatz_search,
        "Mdundo":      _mdundo_search,
        "MovieBox":    _moviebox_search,
    }
    if source in scraper_map:
        cache_key = (query, source, mode)
        cached = _cache_get(cache_key)
        if cached is not None:
            return jsonify({"results": cached, "cached": True})
        results = scraper_map[source](query)
        _cache_set(cache_key, results)
        return jsonify({"results": results})

    prefix = {"YouTube": "ytsearch200:", "SoundCloud": "scsearch200:",
              "Dailymotion": "dmsearch200:", "Audiomack": "audiomack:search20:"}.get(source, "ytsearch200:")

    # ── Cache check ───────────────────────────────────────────────────────────
    cache_key = (query, source, mode)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify({"results": cached, "cached": True})

    # ── YouTube Data API v3 fast-path (faster + more reliable than yt-dlp for metadata)
    if source == "YouTube" and YOUTUBE_API_KEY:
        api_results = _yt_api_search(query, mode)
        if api_results is not None:
            _cache_set(cache_key, api_results)
            return jsonify({"results": api_results})

    try:
        search_opts = {"quiet": True, "no_warnings": True,
                       "extract_flat": True, "skip_download": True,
                       "nocheckcertificate": True,
                       "geo_bypass": True,
                       "socket_timeout": 10,
                       "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"},
                       "extractor_args": {"youtube": {
                           "player_client": ["android_vr", "web_creator", "web"],
                       }}}
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            info = ydl.extract_info(f"{prefix}{query}", download=False)

        results = []
        for e in (info.get("entries") or []):
            if not e:
                continue

            # ── Thumbnail: prefer explicit field, then thumbnails list,
            #    then construct YouTube URL from video ID ─────────────
            thumb = e.get("thumbnail") or ""
            if not thumb:
                thumbs = e.get("thumbnails") or []
                if thumbs:
                    best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
                    thumb = best.get("url") or ""
            if not thumb and source == "YouTube":
                vid_id = e.get("id") or ""
                if vid_id and not vid_id.startswith("http"):
                    thumb = f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"

            # ── Filesize: use real value or estimate from duration ────
            filesize = e.get("filesize") or e.get("filesize_approx") or 0
            if filesize == 0:
                dur_sec = e.get("duration") or 0
                if dur_sec:
                    # music mode → ~256 kbps MP3; video → ~2 Mbps
                    bps = 32_000 if (mode == "music" or source == "SoundCloud") else 250_000
                    filesize = int(dur_sec * bps)

            # ── Duration string ───────────────────────────────────────
            dur_str = e.get("duration_string") or ""
            if not dur_str:
                dur_sec = e.get("duration") or 0
                if dur_sec:
                    m, s = divmod(int(dur_sec), 60)
                    h, m = divmod(m, 60)
                    dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

            results.append({
                "url":       e.get("webpage_url") or e.get("url") or "",
                "title":     e.get("title") or "Unknown",
                "duration":  dur_str,
                "uploader":  e.get("uploader") or e.get("channel") or e.get("creator") or "",
                "thumbnail": thumb,
                "filesize":  filesize,
            })
        _cache_set(cache_key, results)
        return jsonify({"results": results})
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@app.route("/api/playlist-items", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def playlist_items_route():
    """Return all video details for a YouTube playlist.
    Uses YouTube Data API v3 if key is available, otherwise falls back to yt-dlp.
    """
    data        = request.get_json(force=True) or {}
    playlist_id = (data.get("playlist_id") or "").strip()
    playlist_url = (data.get("playlist_url") or "").strip()
    if not playlist_id and not playlist_url:
        return jsonify({"error": "No playlist_id provided"}), 400

    # ── YouTube Data API v3 fast-path ─────────────────────────────────────────
    if YOUTUBE_API_KEY and playlist_id:
        try:
            video_ids  = []
            page_token = None
            while True:
                params = {
                    "part": "snippet,contentDetails",
                    "playlistId": playlist_id,
                    "maxResults": 50,
                    "key": YOUTUBE_API_KEY,
                }
                if page_token:
                    params["pageToken"] = page_token
                r    = _requests.get(f"{_YT_API_BASE}/playlistItems", params=params, timeout=15)
                resp = r.json()
                if resp.get("error"):
                    break  # fall through to yt-dlp
                for item in (resp.get("items") or []):
                    vid_id = ((item.get("snippet") or {}).get("resourceId") or {}).get("videoId") or \
                             (item.get("contentDetails") or {}).get("videoId")
                    if vid_id and vid_id not in ("deleted", None):
                        video_ids.append(vid_id)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

            if video_ids:
                videos = []
                for i in range(0, len(video_ids), 50):
                    batch = video_ids[i:i + 50]
                    vr    = _requests.get(f"{_YT_API_BASE}/videos", params={
                        "part": "snippet,contentDetails",
                        "id":   ",".join(batch),
                        "key":  YOUTUBE_API_KEY,
                    }, timeout=15)
                    detail_map = {v["id"]: v for v in (vr.json().get("items") or [])}
                    for vid_id in batch:
                        v = detail_map.get(vid_id)
                        if not v:
                            continue
                        snip   = v.get("snippet") or {}
                        thumbs = snip.get("thumbnails") or {}
                        thumb  = (thumbs.get("maxres") or thumbs.get("high") or
                                  thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
                        dur_sec, _ = _iso_duration((v.get("contentDetails") or {}).get("duration", ""))
                        videos.append({
                            "video_id":  vid_id,
                            "title":     snip.get("title") or vid_id,
                            "thumbnail": thumb,
                            "duration":  dur_sec,
                            "uploader":  snip.get("channelTitle") or "",
                            "url":       f"https://www.youtube.com/watch?v={vid_id}",
                        })
                return jsonify({"videos": videos})
        except Exception:
            pass  # fall through to yt-dlp

    # ── yt-dlp fallback (no API key needed) ───────────────────────────────────
    try:
        target = playlist_url or f"https://www.youtube.com/playlist?list={playlist_id}"
        opts = {
            "quiet": True, "no_warnings": True,
            "extract_flat": True, "skip_download": True,
            "nocheckcertificate": True, "geo_bypass": True,
            "noplaylist": False,
            "socket_timeout": 10,
            "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"},
            "extractor_args": {"youtube": {
                "player_client": ["android_vr", "web_creator", "web"],
            }},
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)

        entries = info.get("entries") or []
        videos  = []
        for e in entries:
            if not e:
                continue
            vid_id = e.get("id") or e.get("video_id") or ""
            if not vid_id:
                continue
            # thumbnail: prefer explicit, then thumbnails list
            thumb = e.get("thumbnail") or ""
            if not thumb:
                for t in reversed(e.get("thumbnails") or []):
                    if t.get("url"):
                        thumb = t["url"]; break
            if not thumb and vid_id:
                thumb = f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"
            dur_sec = e.get("duration") or 0
            videos.append({
                "video_id":  vid_id,
                "title":     e.get("title") or vid_id,
                "thumbnail": thumb,
                "duration":  dur_sec,
                "uploader":  e.get("uploader") or e.get("channel") or "",
                "url":       e.get("url") or e.get("webpage_url") or f"https://www.youtube.com/watch?v={vid_id}",
            })
        return jsonify({"videos": videos})
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@app.route("/api/web-search", methods=["POST"])
@limiter.limit("20 per minute")
@login_required
def web_search_route():
    import urllib.parse
    data  = request.get_json(force=True) or {}
    query = (data.get("query") or "").strip()[:200]
    if not query:
        return jsonify({"error": "No query provided"}), 400
    try:
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        # Use GET — more reliable than POST for DuckDuckGo HTML
        r = _requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query, "kl": "us-en"},
            headers=headers,
            timeout=12,
        )
        r.raise_for_status()
        html = r.text

        results  = []
        seen     = set()

        # Strategy 1: extract result__a anchors directly (most reliable)
        anchors = re.findall(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL | re.IGNORECASE
        )
        # Also try reversed attr order
        anchors += re.findall(
            r'<a[^>]+href="([^"]+)"[^>]+class="[^"]*result__a[^"]*"[^>]*>(.*?)</a>',
            html, re.DOTALL | re.IGNORECASE
        )

        # Extract snippets: collect all result__snippet spans in order
        snippets = re.findall(
            r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|span|div)',
            html, re.DOTALL | re.IGNORECASE
        )
        snippet_idx = 0

        for href_raw, title_raw in anchors:
            # Decode DDG redirect URL
            href = href_raw.strip()
            if 'uddg=' in href:
                params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(href).query))
                href   = urllib.parse.unquote(params.get('uddg', href))
            if href.startswith('//'):
                href = 'https:' + href
            if not href.startswith('http'):
                continue

            title = re.sub(r'<[^>]+>', '', title_raw).strip()
            if not title or href in seen:
                continue
            seen.add(href)

            snippet = ''
            if snippet_idx < len(snippets):
                snippet = re.sub(r'<[^>]+>', '', snippets[snippet_idx]).strip()
                snippet_idx += 1

            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= 30:
                break

        return jsonify({"results": results})
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


@app.route("/api/prefetch", methods=["POST"])
@limiter.limit("30 per minute")
def prefetch():
    import urllib.parse as _uparse, re as _pre
    data = request.get_json(force=True) or {}
    url  = (data.get("url") or "").strip()
    err = _validate_url(url)
    if err:
        return jsonify({"error": err}), 400

    parsed = _uparse.urlparse(url)
    host   = parsed.netloc.lower()
    path   = parsed.path.lower().rstrip("/")
    _, ext = os.path.splitext(path)
    ext    = ext.lower()

    # ── 1. Twitter / X fast-path (fxtwitter JSON API) ────────────────────────
    if any(d in host for d in ("twitter.com", "x.com", "fxtwitter.com")):
        m = _pre.search(r'(?:status|i/status)[/\\]+(\d+)', url)
        if m:
            try:
                tweet_id = m.group(1)
                api_resp = _requests.get(
                    f"https://api.fxtwitter.com/status/{tweet_id}",
                    timeout=10,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; BRAINIAC/1.0)"}
                )
                api_resp.raise_for_status()
                td = api_resp.json()
                tweet   = td.get("tweet") or {}
                media   = tweet.get("media") or {}
                photos  = media.get("photos") or []
                videos  = media.get("videos") or []
                # Best thumbnail: first video's thumbnail, else first photo
                thumb = ""
                if videos:
                    thumb = videos[0].get("thumbnail_url") or ""
                if not thumb and photos:
                    thumb = photos[0].get("url") or ""
                if not thumb:
                    thumb = tweet.get("thumbnail_url") or ""
                author = (tweet.get("author") or {}).get("name") or ""
                return jsonify({
                    "type":        "twitter",
                    "title":       tweet.get("text") or (f"Tweet by {author}" if author else "Tweet"),
                    "uploader":    author,
                    "thumbnail":   thumb,
                    "count":       len(photos) + len(videos),
                    "media_types": {"photos": len(photos), "videos": len(videos)},
                })
            except Exception:
                pass  # fall through to yt-dlp

    # ── 2. Direct image URL fast-path ────────────────────────────────────────
    _IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif",
                   ".bmp", ".tiff", ".tif", ".heic", ".heif", ".svg"}
    qs_map = _uparse.parse_qs(parsed.query)
    fmt    = (qs_map.get("format") or qs_map.get("fmt") or [""])[0].lower()
    is_img = (ext in _IMAGE_EXTS or "twimg.com" in host or
              fmt in ("jpg", "jpeg", "png", "gif", "webp", "avif"))
    if is_img:
        filesize = 0
        try:
            hr = _requests.head(url, timeout=8, allow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            filesize = int(hr.headers.get("Content-Length") or 0)
        except Exception:
            pass
        fname = os.path.basename(_uparse.unquote(path)) or url.split("/")[-1].split("?")[0] or "image"
        return jsonify({
            "type":      "image",
            "title":     fname,
            "thumbnail": url,
            "filesize":  filesize,
            "uploader":  host,
        })

    # ── 3. Known file extension fast-path (HEAD probe for size) ──────────────
    _FILE_ONLY_EXTS = _DIRECT_EXTS - _IMAGE_EXTS
    if ext in _FILE_ONLY_EXTS:
        filesize = 0
        try:
            hr = _requests.head(url, timeout=8, allow_redirects=True,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            filesize = int(hr.headers.get("Content-Length") or 0)
        except Exception:
            pass
        fname = os.path.basename(_uparse.unquote(path)) or "file"
        return jsonify({
            "type":     "file",
            "title":    fname,
            "ext":      ext.lstrip("."),
            "filesize": filesize,
            "uploader": host,
        })

    # ── 4. YouTube Data API v3 fast-path ─────────────────────────────────────
    if YOUTUBE_API_KEY and ("youtube.com" in url or "youtu.be" in url):
        api_info = _yt_api_prefetch(url)
        if api_info:
            return jsonify(api_info)

    # ── 5. yt-dlp generic (Instagram, TikTok, Reddit, SoundCloud, etc.) ──────
    try:
        prefetch_opts = {"quiet": True, "no_warnings": True,
                         "extract_flat": True, "skip_download": True,
                         "nocheckcertificate": True,
                         "geo_bypass": True,
                         "socket_timeout": 10,
                         "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"},
                         "extractor_args": {"youtube": {
                             "player_client": ["android_vr", "web_creator", "web"],
                         }}}
        with yt_dlp.YoutubeDL(prefetch_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = info.get("entries") or []
        total_dur  = sum(e.get("duration") or 0 for e in entries if e)
        total_size = sum((e.get("filesize") or e.get("filesize_approx") or 0) for e in entries if e)
        if not entries:
            total_dur  = info.get("duration") or 0
            total_size = info.get("filesize") or info.get("filesize_approx") or 0
        # Extract best thumbnail
        thumb = info.get("thumbnail") or ""
        if not thumb:
            thumbs = info.get("thumbnails") or []
            if thumbs:
                thumb = thumbs[-1].get("url") or ""
        return jsonify({
            "type":      "generic",
            "title":     info.get("title", url),
            "count":     len(entries) if entries else 1,
            "uploader":  info.get("uploader") or info.get("channel") or "",
            "duration":  total_dur,
            "filesize":  total_size,
            "thumbnail": thumb,
        })
    except Exception:
        pass  # fall through to OG meta scrape

    # ── 6. OG meta scrape fallback (Pinterest images, unsupported sites, etc.) ─
    try:
        import urllib.parse as _upf
        _pg_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,*/*;q=0.9",
            "Accept-Language": "en-US,en;q=0.9",
        }
        _pr = _requests.get(url, headers=_pg_headers, timeout=12, allow_redirects=True)
        _html = _pr.text

        def _og(prop):
            """Extract content of a named meta property (exact match)."""
            import re as _re_og
            _pesc = _re_og.escape(prop)
            for _pat in (
                rf'<meta[^>]+(?:property|name)\s*=\s*["\']{_pesc}["\'][^>]+content\s*=\s*["\']([^"\']+)["\']',
                rf'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]+(?:property|name)\s*=\s*["\']{_pesc}["\']',
            ):
                _m2 = _re_og.search(_pat, _html, _re_og.I | _re_og.S)
                if _m2:
                    return _m2.group(1).strip()
            return ""

        og_title = (_og("og:title") or _og("twitter:title") or
                    _og("title") or url)
        og_image = (_og("og:image") or _og("og:image:secure_url") or
                    _og("twitter:image") or _og("twitter:image:src") or "")
        og_desc  = _og("og:description") or _og("description") or ""

        # Upgrade Pinterest thumbnail to higher resolution
        if og_image and "pinimg.com" in og_image:
            import re as _rpin2
            og_image = _rpin2.sub(r'/\d+x/', '/736x/', og_image)

        if og_image:
            return jsonify({
                "type":        "image",
                "title":       og_title,
                "thumbnail":   og_image,
                "uploader":    host,
                "description": og_desc,
            })
        if og_title and og_title != url:
            return jsonify({
                "type":        "generic",
                "title":       og_title,
                "uploader":    host,
                "thumbnail":   "",
                "description": og_desc,
            })
    except Exception:
        pass

    return jsonify({"error": "Could not fetch metadata for this URL."}), 500


@app.route("/api/download", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour")
@login_required
def start_download():
    data = request.get_json(force=True) or {}
    url     = (data.get("url") or "").strip()
    mode    = (data.get("mode") or "playlist").strip()
    quality = (data.get("quality") or "Best Quality").strip()

    # Validate URL for non-search modes
    if mode != "music_search" and mode != "movies_search":
        err = _validate_url(url)
        if err:
            return jsonify({"error": err}), 400

    # ── Plan enforcement ──────────────────────────────────────────────────────
    allowed, used, limit = _check_daily_limit(current_user)
    if not allowed:
        plan = _get_user_plan(current_user)
        return jsonify({
            "error": (
                f"Daily download limit reached ({used}/{limit}). "
                + ("Upgrade to Plus or Pro for more downloads." if plan == "free"
                   else "Upgrade to Pro for unlimited downloads.")
            ),
            "limit_reached": True,
            "plan": plan,
        }), 429

    if not _quality_allowed(current_user, quality):
        plan      = _get_user_plan(current_user)
        max_qual  = PLAN_LIMITS[plan]["quality"]
        return jsonify({
            "error": f"Quality '{quality}' is not supported. Please select a valid quality option.",
            "quality_blocked": True,
            "plan": plan,
        }), 403

    # ── Batch / playlist: no cap — all users download full playlists ────────

    # Pass the user's Google/YouTube token to the download thread so yt-dlp
    # can authenticate via Authorization header — avoids bot-detection prompts.
    yt_tok = session.get("gdrive_token") or (current_user.gdrive_token or "")
    if yt_tok:
        data["yt_token"] = yt_tok

    # Increment daily counter
    _increment_daily(current_user)

    # Pass user id so the download thread can persist files to the saved folder
    data["_user_id"] = current_user.id

    task_id = str(uuid.uuid4())
    _create_task(task_id)
    threading.Thread(target=_run_download, args=(task_id, data), daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/api/progress/<task_id>")
def progress_stream(task_id):
    def generate():
        t = _get_task(task_id)
        if not t:
            yield f"data: {json.dumps({'type':'error','msg':'Task not found'})}\n\n"
            return
        q = t["queue"]
        while True:
            try:
                item = q.get(timeout=30)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield "data: {\"type\":\"ping\"}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/file/<task_id>")
def download_file(task_id):
    t = _get_task(task_id)
    # Fallback: read task.json from disk (handles cross-worker / restart cases)
    if not t or t["status"] != "done":
        task_dir = os.path.join(DOWNLOAD_BASE, task_id)
        meta_path = os.path.join(task_dir, "task.json")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path) as f:
                    t = json.load(f)
            except Exception:
                t = None
    if not t or t.get("status") != "done":
        return (
            "<!doctype html><html><head>"
            "<meta http-equiv='refresh' content='3;url=/app'>"
            "<style>body{font-family:sans-serif;display:flex;align-items:center;"
            "justify-content:center;height:100vh;margin:0;background:#1a1a1a;color:#eee;}</style>"
            "</head><body><div style='text-align:center'>"
            "<div style='font-size:48px;margin-bottom:16px'>⏳</div>"
            "<h2 style='margin:0 0 8px'>File Not Ready</h2>"
            "<p style='color:#aaa;margin:0'>The download has expired or hasn't finished yet.<br>"
            "Redirecting back to the app in 3 seconds…</p>"
            "<a href='/app' style='display:inline-block;margin-top:20px;padding:10px 24px;"
            "background:#e53935;color:#fff;border-radius:20px;text-decoration:none;"
            "font-weight:700;'>← Back to App</a>"
            "</div></body></html>",
            404,
        )

    task_dir = os.path.join(DOWNLOAD_BASE, task_id)
    zip_name = t.get("zip")

    if zip_name:
        fpath = os.path.join(task_dir, zip_name)
    else:
        files = t.get("files", [])
        if not files:
            return jsonify({"error": "No files"}), 404
        fpath = os.path.join(task_dir, files[0])

    filename = os.path.basename(fpath)
    _schedule_cleanup(task_dir, task_id, delay=120)
    return send_file(fpath, as_attachment=True, download_name=filename)


# ── (Google Drive and OneDrive integration removed) ─────────────────────────







# ── Change Password ───────────────────────────────────────────────────────────
@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    error = success = None
    if request.method == "POST":
        new_pw     = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")
        if len(new_pw) < 6:
            error = "New password must be at least 6 characters."
        elif new_pw != confirm_pw:
            error = "Passwords do not match."
        else:
            try:
                current_user.password = generate_password_hash(new_pw)
                db.session.commit()
                success = "Password changed successfully!"
            except Exception as _e:
                db.session.rollback()
                error = f"Could not update password: {_e}"
    return render_template("change_password.html", error=error, success=success,
                           username=current_user.username, avatar=current_user.avatar)


# ── Change Email ──────────────────────────────────────────────────────────────
@app.route("/change-email", methods=["GET", "POST"])
@login_required
def change_email():
    error = success = None
    if request.method == "POST":
        new_email = request.form.get("new_email", "").strip().lower()
        if not new_email or "@" not in new_email:
            error = "Please enter a valid email address."
        else:
            try:
                current_user.email = new_email
                db.session.commit()
                success = "Email updated successfully!"
            except Exception as _e:
                db.session.rollback()
                error = f"Could not update email: {_e}"
    return render_template("change_email.html", error=error, success=success,
                           username=current_user.username, avatar=current_user.avatar,
                           current_email=current_user.email)


# ── List / serve / delete persistent saved files ───────────────────────────
@app.route("/api/files")
@login_required
def list_files():
    import datetime
    saved_dir = os.path.join(DOWNLOAD_BASE, "saved", str(current_user.id))
    rows = []
    if os.path.isdir(saved_dir):
        for fname in os.listdir(saved_dir):
            fpath = os.path.join(saved_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                stat = os.stat(fpath)
                rows.append({
                    "name":     fname,
                    "size":     stat.st_size,
                    "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
            except OSError:
                pass
    rows.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify(rows)


@app.route("/api/saved-file/<path:filename>")
@login_required
def serve_saved_file(filename):
    saved_dir = os.path.realpath(os.path.join(DOWNLOAD_BASE, "saved", str(current_user.id)))
    fpath     = os.path.realpath(os.path.join(saved_dir, filename))
    if not fpath.startswith(saved_dir + os.sep) and fpath != saved_dir:
        return jsonify({"error": "Forbidden"}), 403
    if not os.path.isfile(fpath):
        return jsonify({"error": "Not found"}), 404
    _img_exts = {'.jpg','.jpeg','.png','.gif','.webp','.avif','.bmp','.svg','.ico','.tiff','.tif','.heic','.heif'}
    _, ext = os.path.splitext(fpath.lower())
    as_att = ext not in _img_exts
    return send_file(fpath, as_attachment=as_att, download_name=os.path.basename(fpath))


@app.route("/api/delete-file", methods=["POST"])
@login_required
def delete_saved_file():
    data     = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename or os.sep in filename or ".." in filename:
        return jsonify({"error": "Invalid filename"}), 400
    saved_dir = os.path.realpath(os.path.join(DOWNLOAD_BASE, "saved", str(current_user.id)))
    fpath     = os.path.realpath(os.path.join(saved_dir, filename))
    if not fpath.startswith(saved_dir + os.sep):
        return jsonify({"error": "Forbidden"}), 403
    if not os.path.isfile(fpath):
        return jsonify({"error": "Not found"}), 404
    os.remove(fpath)
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
