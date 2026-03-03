import os, uuid, threading, queue, json, time, zipfile, shutil, base64, re, ipaddress
from urllib.parse import urlparse
import static_ffmpeg
static_ffmpeg.add_paths()   # registers ffmpeg/ffprobe on PATH at startup
import yt_dlp
from flask import Flask, render_template, request, jsonify, Response, send_file, send_from_directory
from werkzeug.utils import secure_filename
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)
app.secret_key = os.urandom(24)

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
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
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
        "extractor_args": {"youtube": {"player_client": ["ios", "android"]}},

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


# ── Routes ────────────────────────────────────────────────────────────────────
@app.route("/")
@limiter.limit("120 per minute")
def index():
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
@limiter.limit("30 per minute")
def search():
    data   = request.get_json(force=True) or {}
    query  = (data.get("query") or "").strip()
    source = data.get("source", "YouTube")

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
            results.append({
                "url":       e.get("url") or e.get("webpage_url", ""),
                "title":     e.get("title", "Unknown"),
                "duration":  e.get("duration_string") or "",
                "uploader":  e.get("uploader") or e.get("channel") or "",
                "thumbnail": e.get("thumbnail") or "",
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
        return jsonify({
            "title":    info.get("title", url),
            "count":    len(entries) if entries else 1,
            "uploader": info.get("uploader") or info.get("channel") or "",
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


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
