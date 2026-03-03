import os, uuid, threading, queue, json, time, zipfile, shutil, base64, re, ipaddress, subprocess
from functools import lru_cache
import requests as _requests
from urllib.parse import urlparse
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
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle":  280,   # keep-alive: recycle before MySQL's wait_timeout (usually 300s)
    "pool_pre_ping": True,  # test connection health before each use
    "pool_size":     10,    # max persistent connections per worker
    "max_overflow":  20,    # extra connections allowed under load
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
    gdrive_token        = db.Column(db.String(512), nullable=True)   # access token
    gdrive_refresh      = db.Column(db.Text, nullable=True)          # refresh token (longer)

class AppSetting(db.Model):
    """Key-value store for app-wide settings that must survive restarts."""
    key   = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

with app.app_context():
    try:
        db.create_all()
    except Exception:
        # Ignore "table already exists" race between gunicorn workers on first boot
        db.session.rollback()
    # Add google_id / avatar columns if this is an existing DB without them
    for _col, _ddl in [
        ("google_id",      "ALTER TABLE user ADD COLUMN google_id VARCHAR(128) UNIQUE"),
        ("avatar",         "ALTER TABLE user ADD COLUMN avatar VARCHAR(512)"),
        ("gdrive_token",   "ALTER TABLE user ADD COLUMN gdrive_token VARCHAR(512)"),
        ("gdrive_refresh", "ALTER TABLE user ADD COLUMN gdrive_refresh TEXT"),
    ]:
        try:
            with db.engine.connect() as _conn:
                _conn.execute(db.text(_ddl))
                _conn.commit()
        except Exception:
            pass  # column already exists

# ── Google OAuth ──────────────────────────────────────────────────────────────
oauth = OAuth(app)
google_oauth = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    # Hardcoded endpoints — avoids an extra HTTP round-trip to fetch discovery doc
    access_token_url="https://oauth2.googleapis.com/token",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
    client_kwargs={"scope": "openid email profile"},
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
    if len(url) > 2048:
        return "URL too long."
    try:
        p = urlparse(url)
    except Exception:
        return "Malformed URL."
    if p.scheme not in ("http", "https"):
        return "Only http/https URLs are allowed."
    hostname = p.hostname or ""
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
    if len(q) > 200:
        return "Search query too long (max 200 chars)."
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
SEARCH_CACHE_TTL = 300        # seconds — cache search results for 5 minutes

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

# ── Cookie file ───────────────────────────────────────────────────────────────
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt_cookies.txt")

# Bootstrap from environment variable on startup (Railway-friendly):
# Set YT_COOKIES in Railway env vars to the base64-encoded content of cookies.txt
_env_cookies = os.environ.get("YT_COOKIES", "")
if _env_cookies and not os.path.exists(COOKIES_FILE):
    try:
        with open(COOKIES_FILE, "w", encoding="utf-8") as _f:
            _f.write(base64.b64decode(_env_cookies).decode("utf-8"))
        print("[cookies] Loaded cookies from YT_COOKIES environment variable")
    except Exception as _e:
        print(f"[cookies] Failed to load YT_COOKIES env var: {_e}")

# Restore cookies from DB (persists across Railway restarts/redeployments)
if not os.path.exists(COOKIES_FILE) or os.path.getsize(COOKIES_FILE) == 0:
    try:
        with app.app_context():
            _db_cookies = AppSetting.query.get("yt_cookies")
            if _db_cookies and _db_cookies.value:
                with open(COOKIES_FILE, "w", encoding="utf-8") as _f:
                    _f.write(base64.b64decode(_db_cookies.value).decode("utf-8"))
                print("[cookies] Restored cookies from database")
    except Exception as _e:
        print(f"[cookies] Could not restore cookies from DB: {_e}")


def _cookies_active() -> bool:
    return os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 0


def _inject_cookies(opts: dict) -> dict:
    """Add cookiefile to yt-dlp opts if a cookie file exists."""
    if _cookies_active():
        opts["cookiefile"] = COOKIES_FILE
    return opts

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


def _build_opts(task_id: str, task_dir: str, quality: str, mode: str) -> dict:
    opts = {
        "quiet":            True,
        "no_warnings":      True,
        "progress_hooks":   [_make_hook(task_id)],
        "outtmpl":          os.path.join(task_dir, "%(title)s.%(ext)s"),
        "noplaylist":       False,
        "retries":          10,
        "fragment_retries": 10,
        "ignoreerrors":     True,         # skip bad items in playlists
        "no_color":         True,

        # ── Anti-block: look like a real browser ──────────────────────────────
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "*/*",
        },

        # ── Geo / age-gate bypass ─────────────────────────────────────────────
        "geo_bypass":              True,
        "geo_bypass_country":      "US",
        "age_limit":               99,    # don't block age-gated content

        # ── TLS: ignore cert errors (some CDNs have odd certs) ────────────────
        "nocheckcertificate":      True,

        # ── YouTube client + bgutil PO token provider ──────────────────────
        # bgutil server runs on port 4416 (started by start.sh).
        # It generates YouTube PO tokens so any datacenter IP can download.
        "extractor_args": {
            "youtube": {
                "player_client":         ["web", "tv_embedded", "mweb"],
                "player_skip":           ["configs"],
                "getpot_bgutil_baseurl": ["http://127.0.0.1:4416"],
            },
            "twitter": {"api": ["syndication"]},
        },

        # ── Socket patience ───────────────────────────────────────────────────
        "socket_timeout": 60,

        # ── Let yt-dlp pick the best available format even if DASH fails ──────
        "compat_opts": {"no-youtube-unavailable-videos"},
    }
    _inject_cookies(opts)

    if mode in ("music", "music_search") or quality == "Audio Only (MP3)":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key":              "FFmpegExtractAudio",
            "preferredcodec":   "mp3",
            "preferredquality": "256",
        }]
    elif quality == "Best Quality":
        # Wide fallback chain: tries merged mp4, then any best single-file
        opts["format"] = (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo+bestaudio"
            "/best[ext=mp4]"
            "/best"
        )
    elif quality in ("1080p", "720p", "480p", "360p"):
        h = quality.replace("p", "")
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
            "/best[ext=mp4]"
            "/best"
        )

    # For direct mode merge video+audio and re-encode into a clean mp4 if needed
    if mode == "direct" and quality != "Audio Only (MP3)":
        opts["merge_output_format"] = "mp4"

    return opts


def _run_download(task_id: str, data: dict):
    url     = data.get("url", "").strip()
    quality = data.get("quality", "Best Quality")
    mode    = data.get("mode", "playlist")

    task_dir = os.path.join(DOWNLOAD_BASE, task_id)
    os.makedirs(task_dir, exist_ok=True)

    try:
        _push(task_id, {"type": "log", "msg": "⏳  Starting download…", "level": "info"})
        opts = _build_opts(task_id, task_dir, quality, mode)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
        except yt_dlp.utils.DownloadError as ex:
            error_msg = str(ex)
            if "age-restricted" in error_msg or "This video is age restricted" in error_msg:
                user_msg = "Download failed: The video is age-restricted. Try uploading your YouTube cookies."
            elif "region-locked" in error_msg or "This video is not available in your country" in error_msg:
                user_msg = "Download failed: The video is region-locked. Try using cookies from an allowed region."
            elif "HTTP Error 429" in error_msg or "Access Denied" in error_msg:
                user_msg = "Download failed: Your server IP may be blocked by YouTube. Try uploading cookies or using a different network."
            else:
                user_msg = f"Download failed: {error_msg}"
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["error"] = user_msg
            _push(task_id, {"type": "error", "msg": user_msg})
            return

        files = [f for f in os.listdir(task_dir) if os.path.isfile(os.path.join(task_dir, f))]
        if not files:
            user_msg = (
                "No files were downloaded. This is usually caused by:\n"
                "1) YouTube blocking server IPs — try uploading cookies (Settings → Cookies).\n"
                "2) The video is age-restricted or region-locked.\n"
                "3) The URL is invalid or the video was removed."
            )
            with _tasks_lock:
                _tasks[task_id]["status"] = "error"
                _tasks[task_id]["error"] = user_msg
            _push(task_id, {"type": "error", "msg": user_msg})
            return

        if len(files) == 1:
            with _tasks_lock:
                _tasks[task_id]["files"]  = files
                _tasks[task_id]["status"] = "done"
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
            _push(task_id, {"type": "done", "filename": zip_name, "count": len(files)})

    except Exception as ex:
        with _tasks_lock:
            _tasks[task_id]["status"] = "error"
            _tasks[task_id]["error"]  = str(ex)
        _push(task_id, {"type": "error", "msg": str(ex)})


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
    # Don't block if a different user is already logged in — let Google OAuth proceed
    # so the correct account gets signed in.
    cb = os.environ.get("GOOGLE_REDIRECT_URI") or url_for("google_callback", _external=True)
    return google_oauth.authorize_redirect(cb)


@app.route("/auth/google/callback")
def google_callback():
    try:
        token = google_oauth.authorize_access_token()
    except Exception:
        return redirect("/login")

    userinfo = token.get("userinfo") or {}
    google_id = str(userinfo.get("sub", ""))
    email     = userinfo.get("email", "").lower().strip()
    name      = userinfo.get("name", "")
    picture   = userinfo.get("picture", "")

    if not google_id or not email:
        return redirect("/login")

    # Single query: find by google_id OR email
    from sqlalchemy import or_
    user = User.query.filter(
        or_(User.google_id == google_id, User.email == email)
    ).first()

    if user:
        # Link google_id if signed up via email before; always refresh avatar
        changed = False
        if not user.google_id:
            user.google_id = google_id; changed = True
        if picture and user.avatar != picture:
            user.avatar = picture; changed = True
        if changed:
            db.session.commit()
    else:
        # Create new account
        base = re.sub(r"[^a-zA-Z0-9]", "", name or email.split("@")[0])[:20] or "user"
        # Generate unique username with one query using LIKE
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

    # Clear any previous session (different account) before logging in
    logout_user()
    session.clear()
    login_user(user, remember=True)
    # Restore persisted GDrive token into session so it's immediately available
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
            login_user(user)
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
            login_user(user, remember=request.form.get("remember") == "on")
            if user.gdrive_token:
                session["gdrive_token"] = user.gdrive_token
            return redirect("/app")
        error = "Invalid username/email or password."
    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    session.clear()
    resp = make_response(redirect("/login"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
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


@app.route("/privacy")
def privacy():
    resp = render_template("privacy.html")
    return resp, 200, {"Cache-Control": "public, max-age=3600"}

@app.route("/terms")
def terms():
    resp = render_template("terms.html")
    return resp, 200, {"Cache-Control": "public, max-age=3600"}

@app.route("/")
def landing():
    if current_user.is_authenticated:
        return redirect("/app")
    resp = render_template("landing.html")
    return resp, 200, {"Cache-Control": "public, max-age=300"}

# Google Search Console domain verification — set GOOGLE_SITE_VERIFY env var to your token
@app.route("/google<token>.html")
def google_site_verify(token):
    expected = os.environ.get("GOOGLE_SITE_VERIFY", "")
    if token == expected:
        return f"google-site-verification: google{token}.html", 200, {"Content-Type": "text/plain"}
    return "", 404

@app.route("/app")
@login_required
@limiter.limit("120 per minute")
def index():
    resp = make_response(render_template("index.html", username=current_user.username, avatar=current_user.avatar, email=current_user.email))
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

    prefix = {"YouTube": "ytsearch50:", "SoundCloud": "scsearch50:",
              "Dailymotion": "dmsearch50:"}.get(source, "ytsearch50:")

    # ── Cache check ───────────────────────────────────────────────────────────
    cache_key = (query, source, mode)
    cached = _cache_get(cache_key)
    if cached is not None:
        return jsonify({"results": cached, "cached": True})

    try:
        search_opts = {"quiet": True, "no_warnings": True,
                       "extract_flat": True, "skip_download": True,
                       "nocheckcertificate": True,
                       "geo_bypass": True,
                       "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"},
                       "extractor_args": {"youtube": {
                           "player_client": ["web", "tv_embedded", "mweb"],
                           "getpot_bgutil_baseurl": ["http://127.0.0.1:4416"],
                       }}}
        _inject_cookies(search_opts)
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


@app.route("/api/prefetch", methods=["POST"])
@limiter.limit("30 per minute")
def prefetch():
    data = request.get_json(force=True) or {}
    url  = (data.get("url") or "").strip()
    err = _validate_url(url)
    if err:
        return jsonify({"error": err}), 400
    try:
        prefetch_opts = {"quiet": True, "no_warnings": True,
                         "extract_flat": True, "skip_download": True,
                         "nocheckcertificate": True,
                         "geo_bypass": True,
                         "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"},
                         "extractor_args": {"youtube": {
                             "player_client": ["web", "tv_embedded", "mweb"],
                             "getpot_bgutil_baseurl": ["http://127.0.0.1:4416"],
                         }}}
        _inject_cookies(prefetch_opts)
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
def start_download():
    data = request.get_json(force=True) or {}
    url  = (data.get("url") or "").strip()
    mode = (data.get("mode") or "playlist").strip()

    # Validate URL for non-search modes
    if mode != "music_search" and mode != "movies_search":
        err = _validate_url(url)
        if err:
            return jsonify({"error": err}), 400

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
    if not t or t["status"] != "done":
        return jsonify({"error": "File not ready"}), 404

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


# ── Cookie management routes ─────────────────────────────────────────────────
@app.route("/api/cookies", methods=["GET"])
def cookies_status():
    if _cookies_active():
        size = os.path.getsize(COOKIES_FILE)
        return jsonify({"active": True, "size": size})
    return jsonify({"active": False})


@app.route("/api/cookies", methods=["POST"])
def upload_cookies():
    """Accept either a file upload (multipart) or a plain-text body."""
    # Multipart file upload
    if "file" in request.files:
        f = request.files["file"]
        if not f.filename:
            return jsonify({"error": "No file selected"}), 400
        content = f.read().decode("utf-8", errors="replace")
    else:
        # Raw text body (paste)
        content = request.get_data(as_text=True).strip()

    if not content:
        return jsonify({"error": "Empty cookies content"}), 400

    with open(COOKIES_FILE, "w", encoding="utf-8") as fh:
        fh.write(content)

    # Persist to DB so cookies survive Railway restarts / redeployments
    try:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        setting = AppSetting.query.get("yt_cookies")
        if setting:
            setting.value = encoded
        else:
            db.session.add(AppSetting(key="yt_cookies", value=encoded))
        db.session.commit()
    except Exception as _e:
        print(f"[cookies] Could not persist cookies to DB: {_e}")

    return jsonify({"ok": True, "size": len(content)})


@app.route("/api/cookies", methods=["DELETE"])
def clear_cookies():
    try:
        os.remove(COOKIES_FILE)
    except FileNotFoundError:
        pass
    # Remove from DB too
    try:
        setting = AppSetting.query.get("yt_cookies")
        if setting:
            db.session.delete(setting)
            db.session.commit()
    except Exception:
        pass
    return jsonify({"ok": True})


# ── Google Drive Integration ──────────────────────────────────────────────────
_GDRIVE_SCOPES = "https://www.googleapis.com/auth/drive.file"
_GDRIVE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _gdrive_redirect_uri():
    # Always use the custom domain — Railway's APP_URL points to the internal
    # domain which doesn't match the registered OAuth redirect URI.
    return "https://franciskusi.dev/api/gdrive/callback"


@app.route("/api/gdrive/status")
def gdrive_status():
    gdrive_id  = os.environ.get("GDRIVE_CLIENT_ID", "")
    google_id  = os.environ.get("GOOGLE_CLIENT_ID", "")
    eff_id     = gdrive_id or google_id
    configured = bool(eff_id and (os.environ.get("GDRIVE_CLIENT_SECRET") or os.environ.get("GOOGLE_CLIENT_SECRET")))
    authed     = bool(session.get("gdrive_token") or (current_user.is_authenticated and current_user.gdrive_token))
    return jsonify({
        "configured": configured,
        "authed": authed,
        "has_gdrive_creds": bool(gdrive_id),
        "has_google_creds": bool(google_id),
        "redirect_uri": _gdrive_redirect_uri(),
    })


@app.route("/api/gdrive/auth")
def gdrive_auth():
    # Accept GDRIVE_CLIENT_ID or fall back to the Google login client (same app)
    client_id = os.environ.get("GDRIVE_CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_ID", "")
    if not client_id:
        _err = "Google OAuth client ID is not configured on this server."
        return f"<script>window.opener&&window.opener.postMessage({{type:'gdrive_error',msg:{repr(_err)}}}, '*');window.close();</script>", 503
    task_id = request.args.get("task_id", "")
    redir = _gdrive_redirect_uri()
    from urllib.parse import quote
    auth_url = (
        f"{_GDRIVE_AUTH_URL}?client_id={client_id}"
        f"&redirect_uri={quote(redir, safe='')}"
        "&response_type=code"
        f"&scope={quote(_GDRIVE_SCOPES, safe='')}"
        f"&state={task_id}"
        "&access_type=offline&prompt=consent"
    )
    return redirect(auth_url)


@app.route("/api/gdrive/callback")
def gdrive_callback():
    code    = request.args.get("code", "")
    task_id = request.args.get("state", "")
    error   = request.args.get("error", "")
    if error:
        return f"<script>window.opener&&window.opener.postMessage({{type:'gdrive_error',msg:{repr(error)}}}, '*');window.close();</script>"

    # Fall back to the Google login credentials if GDRIVE-specific ones aren't set
    client_id     = os.environ.get("GDRIVE_CLIENT_ID")     or os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET") or os.environ.get("GOOGLE_CLIENT_SECRET", "")
    redir         = _gdrive_redirect_uri()

    resp = _requests.post(_GDRIVE_TOKEN_URL, data={
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redir, "grant_type": "authorization_code",
    }, timeout=15)
    token_data = resp.json()
    access_token  = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    if not access_token:
        err_msg = token_data.get("error_description", "Token exchange failed")
        return f"<script>window.opener&&window.opener.postMessage({{type:'gdrive_error',msg:'{err_msg}'}}, '*');window.close();</script>"

    # Persist tokens in DB so they survive server restarts / redeploys
    if current_user.is_authenticated:
        current_user.gdrive_token   = access_token
        if refresh_token:  # Google only sends refresh_token on first auth
            current_user.gdrive_refresh = refresh_token
        db.session.commit()

    session["gdrive_token"] = access_token
    return f"<script>window.opener&&window.opener.postMessage({{type:'gdrive_authed',taskId:'{task_id}'}}, '*');window.close();</script>"


@app.route("/api/gdrive/disconnect", methods=["POST"])
@login_required
def gdrive_disconnect():
    session.pop("gdrive_token", None)
    if current_user.is_authenticated:
        current_user.gdrive_token   = None
        current_user.gdrive_refresh = None
        db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/gdrive/upload/<task_id>", methods=["POST"])
@limiter.limit("20 per hour")
def gdrive_upload(task_id):
    # 1. Try session (fastest)
    token = session.get("gdrive_token", "")

    # 2. Fall back to DB-stored token
    if not token and current_user.is_authenticated and current_user.gdrive_token:
        token = current_user.gdrive_token
        session["gdrive_token"] = token  # warm up session cache

    # 3. Try to refresh using the stored refresh token
    if not token and current_user.is_authenticated and current_user.gdrive_refresh:
        client_id     = os.environ.get("GDRIVE_CLIENT_ID")     or os.environ.get("GOOGLE_CLIENT_ID", "")
        client_secret = os.environ.get("GDRIVE_CLIENT_SECRET") or os.environ.get("GOOGLE_CLIENT_SECRET", "")
        r = _requests.post(_GDRIVE_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "refresh_token": current_user.gdrive_refresh,
            "client_id":     client_id,
            "client_secret": client_secret,
        }, timeout=15)
        new_token = r.json().get("access_token", "")
        if new_token:
            token = new_token
            current_user.gdrive_token = new_token
            db.session.commit()
            session["gdrive_token"] = new_token

    if not token:
        return jsonify({"error": "not_authed"}), 401

    t = _get_task(task_id)
    if not t or t["status"] != "done":
        return jsonify({"error": "File not ready"}), 404

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
    file_size = os.path.getsize(fpath)

    # Resumable upload for files > 5 MB, multipart for smaller
    if file_size > 5 * 1024 * 1024:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Upload-Content-Type": "application/octet-stream",
            "X-Upload-Content-Length": str(file_size),
        }
        init_resp = _requests.post(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable",
            headers=headers, json={"name": filename}, timeout=30
        )
        if init_resp.status_code != 200:
            return jsonify({"error": f"Drive init failed: {init_resp.text}"}), 500
        upload_url = init_resp.headers.get("Location")
        with open(fpath, "rb") as f:
            up_resp = _requests.put(upload_url, data=f, headers={
                "Content-Length": str(file_size),
                "Content-Type": "application/octet-stream",
            }, timeout=600)
    else:
        with open(fpath, "rb") as f:
            up_resp = _requests.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                headers={"Authorization": f"Bearer {token}"},
                files={
                    "metadata": ("metadata", json.dumps({"name": filename}), "application/json"),
                    "file": (filename, f, "application/octet-stream"),
                }, timeout=120
            )

    if up_resp.status_code in (200, 201):
        file_id = up_resp.json().get("id", "")
        return jsonify({"ok": True, "url": f"https://drive.google.com/file/d/{file_id}/view"})
    if up_resp.status_code == 401:
        session.pop("gdrive_token", None)
        return jsonify({"error": "not_authed"}), 401
    return jsonify({"error": f"Upload failed ({up_resp.status_code})"}), 500


# ── OneDrive Integration ────────────────────────────────────────────────────────
_OD_AUTH_URL  = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_OD_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_OD_SCOPES    = "Files.ReadWrite offline_access"


def _onedrive_redirect_uri():
    base = os.environ.get("APP_URL", "").rstrip("/")
    return f"{base}/api/onedrive/callback" if base else None


@app.route("/api/onedrive/status")
def onedrive_status():
    configured = bool(os.environ.get("ONEDRIVE_CLIENT_ID"))
    authed = bool(session.get("onedrive_token"))
    return jsonify({"configured": configured, "authed": authed})


@app.route("/api/onedrive/auth")
def onedrive_auth():
    client_id = os.environ.get("ONEDRIVE_CLIENT_ID", "")
    if not client_id:
        return jsonify({"error": "OneDrive is not configured on this server."}), 503
    task_id = request.args.get("task_id", "")
    redir = _onedrive_redirect_uri()
    if not redir:
        return jsonify({"error": "APP_URL env var not set — cannot build redirect URI."}), 503
    auth_url = (
        f"{_OD_AUTH_URL}?client_id={client_id}"
        f"&redirect_uri={redir}"
        "&response_type=code"
        f"&scope={_OD_SCOPES.replace(' ', '%20')}"
        f"&state={task_id}"
    )
    return redirect(auth_url)


@app.route("/api/onedrive/callback")
def onedrive_callback():
    code    = request.args.get("code", "")
    task_id = request.args.get("state", "")
    error   = request.args.get("error", "")
    if error:
        return f"<script>window.opener&&window.opener.postMessage({{type:'od_error',msg:'{error}'}}, '*');window.close();</script>"

    client_id = os.environ.get("ONEDRIVE_CLIENT_ID", "")
    redir     = _onedrive_redirect_uri()

    resp = _requests.post(_OD_TOKEN_URL, data={
        "code": code, "client_id": client_id,
        "redirect_uri": redir, "grant_type": "authorization_code",
        "scope": _OD_SCOPES,
    }, timeout=15)
    token_data = resp.json()
    access_token = token_data.get("access_token", "")
    if not access_token:
        err_msg = token_data.get("error_description", "Token exchange failed")
        return f"<script>window.opener&&window.opener.postMessage({{type:'od_error',msg:'{err_msg}'}}, '*');window.close();</script>"

    session["onedrive_token"] = access_token
    return f"<script>window.opener&&window.opener.postMessage({{type:'od_authed',taskId:'{task_id}'}}, '*');window.close();</script>"


@app.route("/api/onedrive/upload/<task_id>", methods=["POST"])
@limiter.limit("20 per hour")
def onedrive_upload(task_id):
    token = session.get("onedrive_token", "")
    if not token:
        return jsonify({"error": "not_authed"}), 401

    t = _get_task(task_id)
    if not t or t["status"] != "done":
        return jsonify({"error": "File not ready"}), 404

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
    file_size = os.path.getsize(fpath)

    headers = {"Authorization": f"Bearer {token}"}

    if file_size <= 4 * 1024 * 1024:
        # Simple upload (≤4 MB)
        with open(fpath, "rb") as f:
            up_resp = _requests.put(
                f"https://graph.microsoft.com/v1.0/me/drive/root:/{filename}:/content",
                headers={**headers, "Content-Type": "application/octet-stream"},
                data=f, timeout=120
            )
        if up_resp.status_code in (200, 201):
            web_url = up_resp.json().get("webUrl", "")
            return jsonify({"ok": True, "url": web_url})
        if up_resp.status_code == 401:
            session.pop("onedrive_token", None)
            return jsonify({"error": "not_authed"}), 401
        return jsonify({"error": f"Upload failed ({up_resp.status_code})"}), 500
    else:
        # Create upload session for large files
        sess_resp = _requests.post(
            f"https://graph.microsoft.com/v1.0/me/drive/root:/{filename}:/createUploadSession",
            headers={**headers, "Content-Type": "application/json"},
            json={"item": {"@microsoft.graph.conflictBehavior": "rename", "name": filename}},
            timeout=30
        )
        if sess_resp.status_code not in (200, 201):
            return jsonify({"error": f"Session creation failed: {sess_resp.text}"}), 500
        upload_url = sess_resp.json().get("uploadUrl")
        # Upload in 10 MB chunks
        chunk_size = 10 * 1024 * 1024
        with open(fpath, "rb") as f:
            offset = 0
            web_url = ""
            while offset < file_size:
                chunk = f.read(chunk_size)
                end = offset + len(chunk) - 1
                chunk_resp = _requests.put(
                    upload_url,
                    headers={"Content-Range": f"bytes {offset}-{end}/{file_size}",
                             "Content-Length": str(len(chunk))},
                    data=chunk, timeout=300
                )
                if chunk_resp.status_code in (200, 201):
                    web_url = chunk_resp.json().get("webUrl", "")
                elif chunk_resp.status_code == 202:
                    pass  # Continue uploading
                else:
                    return jsonify({"error": f"Chunk upload failed ({chunk_resp.status_code})"}), 500
                offset += len(chunk)
        return jsonify({"ok": True, "url": web_url})


@app.route("/api/onedrive/disconnect", methods=["POST"])
@login_required
def onedrive_disconnect():
    session.pop("onedrive_token", None)
    return jsonify({"ok": True})


# ── Change Password ───────────────────────────────────────────────────────────
@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    from werkzeug.security import check_password_hash, generate_password_hash
    error = success = None
    if request.method == "POST":
        current_pw  = request.form.get("current_password", "")
        new_pw      = request.form.get("new_password", "")
        confirm_pw  = request.form.get("confirm_password", "")
        if not check_password_hash(current_user.password, current_pw):
            error = "Current password is incorrect."
        elif len(new_pw) < 6:
            error = "New password must be at least 6 characters."
        elif new_pw != confirm_pw:
            error = "New passwords do not match."
        else:
            current_user.password = generate_password_hash(new_pw)
            db.session.commit()
            success = "Password changed successfully!"
    return render_template("change_password.html", error=error, success=success,
                           username=current_user.username, avatar=current_user.avatar)


# ── Change Email ──────────────────────────────────────────────────────────────
@app.route("/change-email", methods=["GET", "POST"])
@login_required
def change_email():
    from werkzeug.security import check_password_hash
    error = success = None
    if request.method == "POST":
        new_email   = request.form.get("new_email", "").strip().lower()
        password    = request.form.get("password", "")
        if not new_email or "@" not in new_email:
            error = "Please enter a valid email address."
        elif not check_password_hash(current_user.password, password):
            error = "Password is incorrect."
        elif User.query.filter_by(email=new_email).first():
            error = "That email is already in use."
        else:
            current_user.email = new_email
            db.session.commit()
            success = "Email updated successfully!"
    return render_template("change_email.html", error=error, success=success,
                           username=current_user.username, avatar=current_user.avatar,
                           current_email=current_user.email)


# ── List downloaded files ─────────────────────────────────────────────────────
@app.route("/api/files")
@login_required
def list_files():
    import datetime
    rows = []
    dl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
    if os.path.isdir(dl_dir):
        for root, dirs, files in os.walk(dl_dir):
            for fname in files:
                if fname.endswith(".part"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    stat  = os.stat(fpath)
                    rows.append({
                        "name":     fname,
                        "folder":   os.path.relpath(root, dl_dir),
                        "size":     stat.st_size,
                        "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
                except OSError:
                    pass
    rows.sort(key=lambda x: x["modified"], reverse=True)
    return jsonify(rows)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
