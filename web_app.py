import os, uuid, threading, queue, json, time, zipfile, shutil, base64
import static_ffmpeg
static_ffmpeg.add_paths()   # registers ffmpeg/ffprobe on PATH at startup
import yt_dlp
from flask import Flask, render_template, request, jsonify, Response, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.urandom(24)

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
        "quiet":           True,
        "no_warnings":     True,
        "progress_hooks":  [_make_hook(task_id)],
        "outtmpl":         os.path.join(task_dir, "%(title)s.%(ext)s"),
        "noplaylist":      False,
        # Use iOS/Android client — bypasses YouTube bot-detection on cloud IPs
        "extractor_args":  {"youtube": {"player_client": ["ios", "android"]}},
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
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    elif quality in ("1080p", "720p", "480p", "360p"):
        h = quality.replace("p", "")
        opts["format"] = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/best[height<={h}][ext=mp4]/best"
        )
    else:
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
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
def index():
    return render_template("index.html")


@app.route("/api/search", methods=["POST"])
def search():
    data   = request.get_json(force=True)
    query  = data.get("query", "").strip()
    source = data.get("source", "YouTube")

    if not query:
        return jsonify({"error": "No query provided"}), 400

    prefix = {"YouTube": "ytsearch10:", "SoundCloud": "scsearch10:",
              "Dailymotion": "dmsearch10:"}.get(source, "ytsearch10:")

    try:
        search_opts = {"quiet": True, "no_warnings": True,
                       "extract_flat": True, "skip_download": True,
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
def prefetch():
    data = request.get_json(force=True)
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL"}), 400
    try:
        prefetch_opts = {"quiet": True, "no_warnings": True,
                         "extract_flat": True, "skip_download": True,
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
def start_download():
    data    = request.get_json(force=True)
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
