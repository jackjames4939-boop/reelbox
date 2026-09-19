import io
import logging
import os
import re
import tempfile
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__, static_folder="static", static_url_path="")
# Trust one proxy hop (Render, Railway, etc.) so request.remote_addr is the real client IP.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reelbox")

ALLOWED_HOSTS = {"instagram.com", "www.instagram.com"}
PATH_RE = re.compile(r"^/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?$")

MAX_BYTES = 100 * 1024 * 1024  # refuse videos larger than 100 MB
RATE_LIMIT = 5                 # downloads per IP...
RATE_WINDOW = 60               # ...per this many seconds

_hits = defaultdict(deque)


def too_many_requests(ip: str) -> bool:
    now = time.time()
    q = _hits[ip]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return True
    q.append(now)
    return False


def clean_url(raw: str):
    """Return a normalized Instagram post/reel URL, or None if it isn't one."""
    try:
        parsed = urlparse(raw.strip())
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        return None
    if not PATH_RE.match(parsed.path):
        return None
    return f"https://www.instagram.com{parsed.path}"


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/download")
def download():
    if too_many_requests(request.remote_addr or "unknown"):
        return jsonify(error="Too many downloads in a short time. Wait a minute and try again."), 429

    data = request.get_json(silent=True) or {}
    url = clean_url(str(data.get("url", "")))
    if not url:
        return jsonify(
            error="That doesn't look like an Instagram video link. Copy the link from Instagram's Share menu and try again."
        ), 400

    with tempfile.TemporaryDirectory() as tmp:
        opts = {
            "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s"),
            "format": "best[ext=mp4]/best",
            "playlist_items": "1",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": MAX_BYTES,
            "socket_timeout": 20,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info and info.get("_type") == "playlist":
                    entries = [e for e in (info.get("entries") or []) if e]
                    info = entries[0] if entries else None
                path = ydl.prepare_filename(info) if info else None
        except yt_dlp.utils.DownloadError as exc:
            log.warning("Download failed for %s: %s", url, exc)
            return jsonify(
                error="Couldn't get that video. Make sure the account is public and the link is correct, then try again."
            ), 422
        except Exception:
            log.exception("Unexpected error for %s", url)
            return jsonify(error="Something went wrong on our side. Try again in a moment."), 500

        if not path or not os.path.exists(path):
            return jsonify(error="No downloadable video was found at that link."), 404

        with open(path, "rb") as f:
            buffer = io.BytesIO(f.read())
        video_id = info.get("id", "video")

    return send_file(
        buffer,
        mimetype="video/mp4",
        as_attachment=True,
        download_name=f"reelbox-{video_id}.mp4",
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
