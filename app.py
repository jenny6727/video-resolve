"""Video link resolver for John's iOS Shortcut.

POST /resolve  {"url": "<video page url>", "audio": false}
  -> {"title": ..., "url": "<downloadable media url>", "ext": "mp4",
      "duration": ..., "merged": false}

The Shortcut downloads the returned URL and saves it to Photos/Files.

Two paths:
- Sites with a single combined file (TikTok, Douyin, X, ...): the direct
  media URL is returned, nothing is stored on the server.
- Sites that only offer separate video/audio streams (e.g. YouTube):
  the server downloads both parts, merges them with ffmpeg into one mp4
  in /tmp, and returns a /file/<id> URL for the Shortcut to download.
  Merged files are deleted after 2 hours. Only public, freely
  accessible links are resolved; no login/cookies are used.
"""
import glob
import json
import os
import plistlib
import re
import subprocess
import sys
import time
import uuid

from flask import Flask, Response, jsonify, request, send_file

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_shortcut import build as build_shortcut  # noqa: E402

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
# Run yt-dlp as a module so it works regardless of PATH (venv, Docker, etc.)
YT_DLP_CMD = [sys.executable, "-m", "yt_dlp"]
TIMEOUT = int(os.environ.get("RESOLVE_TIMEOUT", "90"))
MERGE_TIMEOUT = int(os.environ.get("MERGE_TIMEOUT", "280"))
TMP_DIR = os.environ.get("TMP_DIR", "/tmp")

# The signed .shortcut file, built once per worker and cached.
# The key lives only in the server env var; the repo holds no secret.
_signed_shortcut = None
HUBSIGN_URL = "https://hubsign.routinehub.services/sign"


def _check_key():
    if not API_KEY:
        return None
    if request.headers.get("X-API-Key") == API_KEY:
        return None
    if request.args.get("key") == API_KEY:
        return None
    return jsonify({"error": "unauthorized"}), 401


def _base_url():
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return f"{proto}://{request.host}"


def _cleanup_old_files():
    now = time.time()
    for path in glob.glob(os.path.join(TMP_DIR, "vr-*.mp4")):
        try:
            if now - os.path.getmtime(path) > 2 * 3600:
                os.remove(path)
        except OSError:
            pass


# In-memory log of recent /resolve attempts (per worker, last 20).
# Only the link's domain is recorded, never the full URL.
RESOLVE_LOG = []


def _log_resolve(url, status, error=""):
    domain = ""
    m = re.match(r"https?://([^/]+)", url or "")
    if m:
        domain = m.group(1)
    RESOLVE_LOG.append({
        "t": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "domain": domain or "(empty)",
        "status": status,
        "error": error,
    })
    del RESOLVE_LOG[:-20]


@app.get("/debug")
def debug_log():
    """Recent /resolve attempts (domain + status only). Key-protected."""
    err = _check_key()
    if err is not None:
        return err
    return jsonify({"log": RESOLVE_LOG})


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "video-resolve", "version": "1.3"})


@app.get("/shortcut")
def serve_shortcut():
    """Serve the signed iOS Shortcut file for one-tap install.

    The file is built here from the API_KEY env var and signed on the fly
    via RoutineHub's free HubSign service, then cached per worker.
    Install from the phone with:
      shortcuts://import-shortcut?url=<this url, url-encoded>
    """
    global _signed_shortcut
    if _signed_shortcut is not None:
        data = _signed_shortcut
    else:
        if not API_KEY:
            return jsonify({"error": "service not configured"}), 500
        workflow = build_shortcut(_base_url(), API_KEY)
        xml = plistlib.dumps(workflow, fmt=plistlib.FMT_XML).decode("utf-8")
        payload = json.dumps({"shortcutName": "下载视频", "shortcut": xml}).encode("utf-8")
        # Call HubSign with curl, not urllib: the service fingerprints
        # Python's TLS handshake and closes the connection on it.
        try:
            proc = subprocess.run(
                ["curl", "-s", "-m", "60", "-X", "POST", HUBSIGN_URL,
                 "-H", "Origin: https://routinehub.co",
                 "-H", "Referer: https://routinehub.co/",
                 "-H", "Content-Type: application/json",
                 "--data-binary", "@-"],
                input=payload, capture_output=True, timeout=70)
            data = proc.stdout
        except Exception as exc:  # noqa: BLE001 - report as 502
            return jsonify({"error": "signing service unavailable",
                            "detail": str(exc)}), 502
        if not data.startswith(b"AEA1"):
            return jsonify({"error": "signing failed"}), 502
        _signed_shortcut = data
    return Response(
        data,
        mimetype="application/octet-stream",
        headers={"Content-Disposition": 'attachment; filename="download-video.shortcut"'},
    )


@app.get("/file/<job_id>")
def serve_file(job_id):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id or ""):
        return jsonify({"error": "not found"}), 404
    path = os.path.join(TMP_DIR, f"vr-{job_id}.mp4")
    if not os.path.isfile(path):
        return jsonify({"error": "file expired or not found"}), 404
    return send_file(path, mimetype="video/mp4", as_attachment=False,
                     download_name=f"video-{job_id}.mp4")


@app.get("/resolve")
@app.post("/resolve")
def resolve():
    err = _check_key()
    if err is not None:
        return err

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or request.args.get("url") or "").strip()
    audio_only = bool(data.get("audio") or request.args.get("audio"))
    if not url or not url.startswith(("http://", "https://")):
        _log_resolve(url, 400, "need a http(s) video url")
        return jsonify({"error": "need a http(s) video url"}), 400

    _cleanup_old_files()

    # One combined file when the site offers it; otherwise separate
    # video (<=720p) + audio streams that we merge server-side.
    fmt = "ba/bestaudio" if audio_only else \
        "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b"
    cmd = YT_DLP_CMD + [
        "--no-playlist",
        "--no-warnings",
        "--get-url",
        "--get-title",
        "--get-duration",
        "--get-filename",
        "-f", fmt,
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        _log_resolve(url, 504, "resolve timed out")
        return jsonify({"error": "resolve timed out, try again"}), 504

    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        detail = (proc.stderr or "").strip().splitlines()
        _log_resolve(url, 422, detail[-1] if detail else "could not resolve")
        return jsonify({"error": "could not resolve this link",
                        "detail": detail[-1] if detail else "unknown error"}), 422

    # yt-dlp prints title, direct url(s), filename, duration -- order varies,
    # so classify each line instead of assuming positions.
    media_urls, title, filename, duration = [], None, "", None
    for ln in lines:
        if ln.startswith(("http://", "https://")):
            media_urls.append(ln)
        elif re.fullmatch(r"\d+:\d{2}(:\d{2})?", ln):
            duration = ln
        elif re.search(r"\.(mp4|m4a|webm|mkv|mov|mp3|opus|aac)$", ln, re.I):
            filename = ln
        elif title is None:
            title = ln
    if not media_urls:
        _log_resolve(url, 422, "no direct media url found")
        return jsonify({"error": "no direct media url found"}), 422

    ext = filename.rsplit(".", 1)[-1] if "." in filename else ("m4a" if audio_only else "mp4")

    # Single combined file: hand the direct URL straight to the Shortcut.
    if audio_only or len(media_urls) == 1:
        _log_resolve(url, 200, "direct")
        return jsonify({
            "title": title,
            "url": media_urls[0],
            "ext": ext,
            "duration": duration,
            "audio_only": audio_only,
            "merged": False,
        })

    # Separate video + audio: download both and merge into one mp4.
    job_id = uuid.uuid4().hex[:12]
    out_tmpl = os.path.join(TMP_DIR, f"vr-{job_id}.%(ext)s")
    merge_cmd = YT_DLP_CMD + [
        "--no-playlist",
        "--no-warnings",
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", out_tmpl,
        url,
    ]
    try:
        mproc = subprocess.run(merge_cmd, capture_output=True, text=True,
                               timeout=MERGE_TIMEOUT)
    except subprocess.TimeoutExpired:
        _log_resolve(url, 504, "download took too long")
        return jsonify({"error": "download took too long, try a shorter video"}), 504
    merged_path = os.path.join(TMP_DIR, f"vr-{job_id}.mp4")
    if mproc.returncode != 0 or not os.path.isfile(merged_path):
        detail = (mproc.stderr or "").strip().splitlines()
        _log_resolve(url, 422, detail[-1] if detail else "could not download/merge")
        return jsonify({"error": "could not download/merge this video",
                        "detail": detail[-1] if detail else "unknown error"}), 422

    _log_resolve(url, 200, "merged")
    return jsonify({
        "title": title,
        "url": f"{_base_url()}/file/{job_id}",
        "ext": "mp4",
        "duration": duration,
        "audio_only": False,
        "merged": True,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port)
