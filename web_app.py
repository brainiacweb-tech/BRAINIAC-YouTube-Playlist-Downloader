import os, sys, uuid, threading, queue, json, time, zipfile, shutil, re, ipaddress, subprocess
from datetime import timedelta
from functools import lru_cache
import requests as _requests
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
    resp.headers["X-Frame-Options"]        = "DENY"
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
        "frame-ancestors 'none';"
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

@app.route("/images/<path:filename>")
def serve_image(filename):
    resp = send_from_directory(IMAGES_DIR, filename)
    resp.headers["Cache-Control"] = "public, max-age=86400"  # cache images 24h
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

    def debug(self, msg: str):
        if msg.startswith("[debug]"):
            return  # too noisy
        _push(self._tid, {"type": "log", "msg": msg, "level": "info"})

    def warning(self, msg: str):
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
        "retries":          3,
        "fragment_retries": 3,
        "ignoreerrors":     mode in ("playlist",),  # only skip errors in playlists
        "no_color":         True,

        # ── Anti-block: look like a real browser + YouTube OAuth if available ─
        "http_headers": _headers,

        # ── Geo / age-gate bypass ─────────────────────────────────────────────
        "geo_bypass":              True,
        "geo_bypass_country":      "US",
        "age_limit":               99,    # bypass all age gates
        "allow_unplayable_formats": True,  # don't skip unplayable/restricted formats

        # ── TLS: ignore cert errors (some CDNs have odd certs) ────────────────
        "nocheckcertificate":      True,

        # tv_embedded: embedded player — never prompts for sign-in, most bot-resistant.
        # web_creator: YouTube Studio client — no PO tokens, handles restricted/age-gated.
        # android/ios/mweb/web_embedded removed: trigger GVS PO tokens or bot checks.
        # player_skip: skip webpage+JS extraction — suppresses n-challenge/signature solver
        #              warnings (no Node.js on Railway) and avoids bot-detection probes.
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "web_creator"],
                "player_skip":   ["webpage", "configs", "js"],
            },
            "twitter": {"api": ["syndication"]},
        },

        # ── Socket patience ───────────────────────────────────────────────────
        "socket_timeout": 60,

        # ── Rate-limit avoidance: pause between consecutive requests ──────────
        "sleep_interval_requests": 2,
        "sleep_interval":          1,

        # ── Let yt-dlp pick the best available format even if DASH fails ──────
        "compat_opts": {"no-youtube-unavailable-videos"},
    }
    if _FFMPEG_LOCATION:
        opts["ffmpeg_location"] = os.path.dirname(_FFMPEG_LOCATION)

    if mode in ("music", "music_search") or quality == "Audio Only (MP3)":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        opts["postprocessors"] = [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "256",
        }]
    elif quality == "Best Quality":
        if mode == "direct":
            # Fully universal — no codec/container restrictions, works on any site
            opts["format"] = "bestvideo+bestaudio/bestvideo/best"
        else:
            # Prefer DASH splits — combined formats (e.g. format 18) are 403'd on server IPs
            opts["format"] = (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                "/bestvideo+bestaudio"
                "/best[ext=mp4]"
                "/best"
            )
    elif quality in ("1080p", "720p", "480p", "360p"):
        h = quality.replace("p", "")
        if mode == "direct":
            opts["format"] = (
                f"bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]"
                f"/best"
            )
        else:
            opts["format"] = (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}][ext=mp4]"
                f"/best[height<={h}]"
                f"/best"
            )
    else:
        opts["format"] = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo+bestaudio"
            "/best[ext=mp4]/best"
        )

    # Merge DASH splits to mp4
    if quality != "Audio Only (MP3)":
        opts["merge_output_format"] = "mp4"
    if mode == "direct":
        opts["allow_unplayable_formats"] = True

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

    session = _requests.Session()
    session.max_redirects = 15
    resp = session.get(url, headers=headers, stream=True, timeout=90,
                       allow_redirects=True, verify=False)
    resp.raise_for_status()

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



def _normalize_direct_url(url: str) -> str:
    """
    Rewrite common sharing URLs into direct-download URLs before attempting
    any download. Handles Dropbox, Google Drive, and GitHub.
    """
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    host   = parsed.netloc.lower()

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

    return url


# Extensions that strongly indicate a plain file — skip yt-dlp entirely
_DIRECT_EXTS = {
    # archives
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst",
    # executables / installers
    ".exe", ".msi", ".pkg", ".deb", ".rpm", ".appimage", ".dmg",
    ".apk", ".ipa", ".msix",
    # documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp", ".epub", ".mobi",
    # images (not likely video-platform)
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".avif",
    ".tiff", ".tif", ".ico", ".svg",
    # raw media files NOT on a media platform (will still try yt-dlp first
    # for recognised hosts; this only shortcuts UNRECOGNISED direct links)
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".wmv", ".m4v",
    ".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".wma", ".opus",
    # data / code
    ".csv", ".json", ".xml", ".sqlite", ".db",
    ".iso", ".img", ".bin", ".torrent",
    # fonts
    ".ttf", ".otf", ".woff", ".woff2",
}


def _looks_like_direct_file(url: str) -> bool:
    """Return True if the URL path ends with a known file extension."""
    import urllib.parse
    path = urllib.parse.urlparse(url).path.lower().rstrip("/")
    _, ext = os.path.splitext(path)
    return ext in _DIRECT_EXTS


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

            # 2. If the URL clearly points to a plain file, skip yt-dlp
            #    and go straight to HTTP — faster and avoids false errors.
            use_http_first = _looks_like_direct_file(url)

            http_succeeded = False
            ytdlp_tried    = False

            if use_http_first:
                _push(task_id, {"type": "log",
                                "msg": "📥  Direct file link detected — downloading via HTTP…",
                                "level": "info"})
                try:
                    _http_fallback_download(task_id, url, task_dir)
                    http_succeeded = True
                except Exception as http_ex:
                    # HTTP failed for a plain-file URL → still try yt-dlp as last resort
                    _push(task_id, {"type": "log",
                                    "msg": f"⚠️  HTTP attempt failed ({http_ex}), trying yt-dlp…",
                                    "level": "warn"})

            # 3. yt-dlp path (for media pages, or as fallback after HTTP failed)
            if not http_succeeded:
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
                    _push(task_id, {"type": "log",
                                    "msg": "ℹ️  yt-dlp hit a recursion loop on this page — falling back to HTTP…",
                                    "level": "info"})
                except Exception as ex:
                    # In direct mode: treat ALL yt-dlp failures as "try HTTP"
                    ytdlp_failed = True
                    _push(task_id, {"type": "log",
                                    "msg": f"ℹ️  yt-dlp could not handle URL ({ex}), falling back to HTTP…",
                                    "level": "info"})
                finally:
                    sys.setrecursionlimit(_old_rlimit)

                # 4. If yt-dlp failed OR produced no files → HTTP fallback
                files_so_far = [
                    f for f in os.listdir(task_dir)
                    if os.path.isfile(os.path.join(task_dir, f)) and not f.endswith(".part")
                ]
                if ytdlp_failed or not files_so_far:
                    try:
                        _http_fallback_download(task_id, url, task_dir)
                        http_succeeded = True
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
                if "Sign in to confirm" in error_msg or "not a bot" in error_msg or "cookies" in error_msg.lower():
                    # ── Auto-retry with longer sleep before giving up ──────────────────
                    _retried = False
                    for _retry_delay in (8, 20):
                        _push(task_id, {"type": "log",
                                        "msg": f"⏳  YouTube bot-check detected — retrying in {_retry_delay}s…",
                                        "level": "warn"})
                        time.sleep(_retry_delay)
                        try:
                            _retry_opts, _ = _build_opts(task_id, task_dir, quality, mode,
                                                         yt_token=yt_token,
                                                         playlist_cap=playlist_cap)
                            _retry_opts["sleep_interval_requests"] = _retry_delay
                            _retry_opts["sleep_interval"]          = _retry_delay // 2
                            with yt_dlp.YoutubeDL(_retry_opts) as _rydl:
                                _rydl.download([url])
                            _ytdlp_failed = False   # success
                            _retried = True
                            break
                        except Exception:
                            pass
                    if not _retried or _ytdlp_failed:
                        _ytdlp_user_msg = ("Download failed: YouTube is blocking this server\u2019s IP address. "
                                           "Try again in a few minutes, or paste the video URL in the Direct tab \u2014 "
                                           "it may work via a different extraction path.")
                elif ("Error code: 152" in error_msg or "Error code: 183" in error_msg
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

    if source not in ("YouTube", "SoundCloud", "Dailymotion"):
        source = "YouTube"

    prefix = {"YouTube": "ytsearch200:", "SoundCloud": "scsearch200:",
              "Dailymotion": "dmsearch200:"}.get(source, "ytsearch200:")

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
                           "player_client": ["tv_embedded", "web_creator"],
                           "player_skip":   ["webpage", "configs", "js"],
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
                "player_client": ["tv_embedded", "web_creator"],
                "player_skip":   ["webpage", "configs", "js"],
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
    data = request.get_json(force=True) or {}
    url  = (data.get("url") or "").strip()
    err = _validate_url(url)
    if err:
        return jsonify({"error": err}), 400

    # ── YouTube Data API v3 fast-path ─────────────────────────────────────────
    if YOUTUBE_API_KEY and ("youtube.com" in url or "youtu.be" in url):
        api_info = _yt_api_prefetch(url)
        if api_info:
            return jsonify(api_info)

    try:
        prefetch_opts = {"quiet": True, "no_warnings": True,
                         "extract_flat": True, "skip_download": True,
                         "nocheckcertificate": True,
                         "geo_bypass": True,
                         "socket_timeout": 10,
                         "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"},
                         "extractor_args": {"youtube": {
                             "player_client": ["tv_embedded", "web_creator"],
                             "player_skip":   ["webpage", "configs", "js"],
                         }}}
        with yt_dlp.YoutubeDL(prefetch_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        entries = info.get("entries") or []
        total_dur  = sum(e.get("duration") or 0 for e in entries if e)
        total_size = sum((e.get("filesize") or e.get("filesize_approx") or 0) for e in entries if e)
        # For single videos (not playlists)
        if not entries:
            total_dur  = info.get("duration") or 0
            total_size = info.get("filesize") or info.get("filesize_approx") or 0
        return jsonify({
            "title":    info.get("title", url),
            "count":    len(entries) if entries else 1,
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": total_dur,
            "filesize": total_size,
        })
    except Exception as ex:
        return jsonify({"error": str(ex)}), 500


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
    return send_file(fpath, as_attachment=True, download_name=os.path.basename(fpath))


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
