import os, uuid, threading, queue, json, time, zipfile, shutil, base64, re, ipaddress
import requests as _requests
from urllib.parse import urlparse
import static_ffmpeg
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from authlib.integrations.flask_client import OAuth
static_ffmpeg.add_paths()   # registers ffmpeg/ffprobe on PATH at startup
import yt_dlp
from flask import Flask, render_template, request, jsonify, Response, send_file, send_from_directory, redirect, session, url_for
from werkzeug.utils import secure_filename
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "brainiac-yt-dl-secret-key-2026"

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
    "pool_recycle": 280,   # keep-alive: recycle before MySQL's wait_timeout (usually 300s)
    "pool_pre_ping": True, # test connection health before each use
}
db = SQLAlchemy(app)

# ── Auth ──────────────────────────────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = ""

class User(UserMixin, db.Model):
    id        = db.Column(db.Integer, primary_key=True)
    username  = db.Column(db.String(80), unique=True, nullable=False)
    email     = db.Column(db.String(120), unique=True, nullable=False)
    password  = db.Column(db.String(256), nullable=False)
    google_id = db.Column(db.String(128), unique=True, nullable=True)
    avatar    = db.Column(db.String(512), nullable=True)

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
        ("google_id", "ALTER TABLE user ADD COLUMN google_id VARCHAR(128) UNIQUE"),
        ("avatar",    "ALTER TABLE user ADD COLUMN avatar VARCHAR(512)"),
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
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)

# ── Security config ───────────────────────────────────────────────────────────
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024   # 5 MB max upload / request body

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
    return send_from_directory(IMAGES_DIR, filename)

DOWNLOAD_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_downloads")
os.makedirs(DOWNLOAD_BASE, exist_ok=True)

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
                "Chrome/122.0.0.0 Safari/537.36"
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

        # ── YouTube specifically: use iOS/Android client ──────────────────────
        # ── Twitter/X: use syndication API (avoids auth requirement) ────────
        "extractor_args": {
            "youtube": {"player_client": ["ios", "android"]},
            "twitter": {"api": ["syndication"]},
        },

        # ── Socket patience ───────────────────────────────────────────────────
        "socket_timeout": 30,
    }
    _inject_cookies(opts)

    if mode == "music" or quality == "Audio Only (MP3)":
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

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        files = [f for f in os.listdir(task_dir) if os.path.isfile(os.path.join(task_dir, f))]
        if not files:
            raise RuntimeError("No files were downloaded.")

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
    if current_user.is_authenticated:
        return redirect("/")
    cb = url_for("google_callback", _external=True)
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

    # 1. Find by google_id
    user = User.query.filter_by(google_id=google_id).first()

    # 2. Find by email and link
    if not user:
        user = User.query.filter_by(email=email).first()
        if user:
            user.google_id = google_id
            user.avatar    = picture or user.avatar
            db.session.commit()

    # 3. Create new account
    if not user:
        base = re.sub(r"[^a-zA-Z0-9]", "", name or email.split("@")[0])[:20] or "user"
        username, n = base, 1
        while User.query.filter_by(username=username).first():
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

    login_user(user, remember=True)
    return redirect("/")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect("/")
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
            return redirect("/")
    return render_template("signup.html", error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect("/")
    error = None
    if request.method == "POST":
        identifier = request.form.get("identifier", "").strip()
        password   = request.form.get("password", "")
        user = User.query.filter(
            (User.username == identifier) | (User.email == identifier.lower())
        ).first()
        if user and check_password_hash(user.password, password):
            login_user(user, remember=request.form.get("remember") == "on")
            return redirect("/")
        error = "Invalid username/email or password."
    return render_template("login.html", error=error)


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect("/login")


@app.route("/")
@login_required
@limiter.limit("120 per minute")
def index():
    return render_template("index.html", username=current_user.username, avatar=current_user.avatar)


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

    prefix = {"YouTube": "ytsearch10:", "SoundCloud": "scsearch10:",
              "Dailymotion": "dmsearch10:"}.get(source, "ytsearch10:")

    try:
        search_opts = {"quiet": True, "no_warnings": True,
                       "extract_flat": True, "skip_download": True,
                       "nocheckcertificate": True,
                       "geo_bypass": True,
                       "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"},
                       "extractor_args": {"youtube": {"player_client": ["ios", "android"]}}}
        _inject_cookies(search_opts)
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            info = ydl.extract_info(f"{prefix}{query}", download=False)

        results = []
        for e in (info.get("entries") or [])[:10]:
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
                         "extractor_args": {"youtube": {"player_client": ["ios", "android"]}}}
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

    return jsonify({"ok": True, "size": len(content)})


@app.route("/api/cookies", methods=["DELETE"])
def clear_cookies():
    try:
        os.remove(COOKIES_FILE)
    except FileNotFoundError:
        pass
    return jsonify({"ok": True})


# ── Google Drive Integration ──────────────────────────────────────────────────
_GDRIVE_SCOPES = "https://www.googleapis.com/auth/drive.file"
_GDRIVE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _gdrive_redirect_uri():
    base = os.environ.get("APP_URL", "").rstrip("/")
    return f"{base}/api/gdrive/callback" if base else None


@app.route("/api/gdrive/status")
def gdrive_status():
    configured = bool(os.environ.get("GDRIVE_CLIENT_ID") and os.environ.get("GDRIVE_CLIENT_SECRET"))
    authed = bool(session.get("gdrive_token"))
    return jsonify({"configured": configured, "authed": authed})


@app.route("/api/gdrive/auth")
def gdrive_auth():
    client_id = os.environ.get("GDRIVE_CLIENT_ID", "")
    if not client_id:
        return jsonify({"error": "Google Drive is not configured on this server."}), 503
    task_id = request.args.get("task_id", "")
    redir = _gdrive_redirect_uri()
    if not redir:
        return jsonify({"error": "APP_URL env var not set — cannot build redirect URI."}), 503
    auth_url = (
        f"{_GDRIVE_AUTH_URL}?client_id={client_id}"
        f"&redirect_uri={redir}"
        "&response_type=code"
        f"&scope={_GDRIVE_SCOPES}"
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
        return f"<script>window.opener&&window.opener.postMessage({{type:'gdrive_error',msg:'{error}'}}, '*');window.close();</script>"

    client_id     = os.environ.get("GDRIVE_CLIENT_ID", "")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET", "")
    redir         = _gdrive_redirect_uri()

    resp = _requests.post(_GDRIVE_TOKEN_URL, data={
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redir, "grant_type": "authorization_code",
    }, timeout=15)
    token_data = resp.json()
    access_token = token_data.get("access_token", "")
    if not access_token:
        err_msg = token_data.get("error_description", "Token exchange failed")
        return f"<script>window.opener&&window.opener.postMessage({{type:'gdrive_error',msg:'{err_msg}'}}, '*');window.close();</script>"

    session["gdrive_token"] = access_token
    return f"<script>window.opener&&window.opener.postMessage({{type:'gdrive_authed',taskId:'{task_id}'}}, '*');window.close();</script>"


@app.route("/api/gdrive/upload/<task_id>", methods=["POST"])
@limiter.limit("20 per hour")
def gdrive_upload(task_id):
    token = session.get("gdrive_token", "")
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


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
