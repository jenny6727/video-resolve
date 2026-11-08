"""Video link resolver for John's iOS Shortcut.

POST /resolve  {"url": "<video page url>", "audio": false}
  -> {"title": ..., "url": "<direct media url>", "ext": "mp4", "duration": ..., "site": ...}

The Shortcut then downloads the direct URL itself and saves it to Photos/Files.
This keeps the server stateless: no video files are stored here.
Only public, freely accessible links are resolved; no login/cookies are used.
"""
import json
import os
import subprocess
import sys

from flask import Flask, jsonify, request

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
# Run yt-dlp as a module so it works regardless of PATH (venv, Docker, etc.)
YT_DLP_CMD = [sys.executable, "-m", "yt_dlp"]
TIMEOUT = int(os.environ.get("RESOLVE_TIMEOUT", "90"))


def _check_key():
    if not API_KEY:
        return None
    if request.headers.get("X-API-Key") == API_KEY:
        return None
    if request.args.get("key") == API_KEY:
        return None
    return jsonify({"error": "unauthorized"}), 401


@app.get("/")
def health():
    return jsonify({"ok": True, "service": "video-resolve", "version": "1.0"})


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
        return jsonify({"error": "need a http(s) video url"}), 400

    fmt = "ba/bestaudio" if audio_only else "mp4"
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
        return jsonify({"error": "resolve timed out, try again"}), 504

    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if proc.returncode != 0 or not lines:
        detail = (proc.stderr or "").strip().splitlines()
        return jsonify({"error": "could not resolve this link",
                        "detail": detail[-1] if detail else "unknown error"}), 422

    # yt-dlp prints title, direct url(s), filename, duration -- order varies,
    # so classify each line instead of assuming positions.
    import re
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
        return jsonify({"error": "no direct media url found"}), 422

    ext = filename.rsplit(".", 1)[-1] if "." in filename else ("m4a" if audio_only else "mp4")
    return jsonify({
        "title": title,
        "url": media_urls[0],
        "all_urls": media_urls,
        "ext": ext,
        "duration": duration,
        "audio_only": audio_only,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "7860"))
    app.run(host="0.0.0.0", port=port)
