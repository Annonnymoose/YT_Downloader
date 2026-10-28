"""
YouTube / YouTube Music Downloader
Backend: Flask + yt-dlp (subprocess mode — no shell=True)
"""

import os
import re
import sys
import json
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

BASE_DIR = Path(__file__).resolve().parent

# Support index.html in either ./templates/index.html or ./index.html
TEMPLATE_DIR = BASE_DIR / "templates" if (BASE_DIR / "templates" / "index.html").exists() else BASE_DIR

# ── Output path ───────────────────────────────────────────────────────────────
# Use D:\Music if D: drive exists; otherwise fall back to ~/Music
DEFAULT_DRIVE = Path(r"D:\\")
MUSIC_ROOT = Path(r"D:\Music") if DEFAULT_DRIVE.exists() else (Path.home() / "Music")
MUSIC_ROOT.mkdir(parents=True, exist_ok=True)

# Organizes playlist items into their playlist folder, and single tracks into "Singles"
OUTPUT_TEMPLATE = str(MUSIC_ROOT / r"%(playlist_title|Singles)s" / r"%(title)s.%(ext)s")

# ── yt-dlp command resolution ─────────────────────────────────────────────────
_ytdlp_exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
if _ytdlp_exe:
    YTDLP_CMD = [_ytdlp_exe]
else:
    # Fall back to running yt_dlp as a module with the active Python interpreter
    try:
        import yt_dlp  # noqa: F401
        YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        sys.exit(
            "[ERROR] yt-dlp not found.\n"
            "Install it with:  pip install -r requirements.txt"
        )

# ── SSE progress queues (keyed by session token) ──────────────────────────────
_progress_queues: dict[str, queue.Queue] = {}
_queue_lock = threading.Lock()


def _get_queue(token: str) -> queue.Queue:
    with _queue_lock:
        if token not in _progress_queues:
            _progress_queues[token] = queue.Queue()
        return _progress_queues[token]


def _remove_queue(token: str) -> None:
    with _queue_lock:
        _progress_queues.pop(token, None)


# ── CLI argument builder ──────────────────────────────────────────────────────
def _build_args(fmt: str, embed_metadata: bool) -> list[str]:
    """
    Build a yt-dlp argument list for subprocess.Popen.
    Every element is a discrete string (never shell=True).
    """
    args: list[str] = [
        *YTDLP_CMD,
        "--no-playlist",          # treat watch URLs with &list= as single tracks unless pure playlist URL
        "--ignore-errors",        # skip unavailable items in playlists
        "--newline",              # one progress line per stdout line
        "--progress",
        "--output", OUTPUT_TEMPLATE,
    ]

    if fmt == "mp3":
        args += [
            "--format", "bestaudio/best",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",       # 0 = best VBR / 320 kbps CBR
        ]
    else:  # mp4
        args += [
            "--format",
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
        ]

    if embed_metadata:
        args += [
            "--embed-metadata",
            "--embed-thumbnail",
            "--parse-metadata", "%(playlist_title,title)s:%(meta_album)s",
            "--parse-metadata", "%(uploader, artist)s:%(meta_album_artist)s",
        ]

    return args


# ── Progress line parsers ─────────────────────────────────────────────────────
# Handles standard and fragmented (~) size estimates:
# e.g. [download]  45.3% of ~ 5.23MiB at 1.23MiB/s ETA 00:03
_PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<pct>[\d.]+)%\s+of\s+~?\s*[\d.]+\S+\s+at\s+(?P<speed>\S+)\s+ETA\s+(?P<eta>\S+)"
)
_DEST_RE = re.compile(
    r"\[(?:download|ExtractAudio)\]\s+Destination:\s+(?P<path>.+)"
)
_MERGER_RE = re.compile(
    r'\[Merger\]\s+Merging formats into\s+"?(?P<path>[^"]+)"?'
)
_ALREADY_DL_RE = re.compile(
    r"\[download\]\s+(?P<path>.+?)\s+has already been downloaded"
)
_FINISHED_RE = re.compile(
    r"\[(?:ExtractAudio|Merger|ffmpeg|EmbedThumbnail|Metadata|MoveFiles)\]"
)


def _download_worker(urls: list[str], fmt: str, embed_metadata: bool, token: str) -> None:
    """Run yt-dlp as a subprocess for each URL, streaming progress to the SSE queue."""
    q = _get_queue(token)
    base_args = _build_args(fmt, embed_metadata)

    for url in urls:
        url = url.strip()
        if not url:
            continue

        q.put({"type": "start_url", "url": url})
        current_file: str = ""
        notified_finished: bool = False

        try:
            proc = subprocess.Popen(
                [*base_args, url],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )

            has_atomic_error = False
            has_ffmpeg_error = False
            last_error = ""

            if proc.stdout is not None:
                for line in proc.stdout:
                    line = line.rstrip()
                    if not line:
                        continue

                    lower_line = line.lower()
                    if "atomicparsley" in lower_line:
                        has_atomic_error = True
                    if "ffmpeg not found" in lower_line or "ffprobe and ffmpeg not found" in lower_line:
                        has_ffmpeg_error = True

                    if line.startswith("ERROR:"):
                        last_error = line
                        q.put({"type": "error", "url": url, "message": line})

                    # Track current filename across download, audio extraction, or merging
                    dest_m = _DEST_RE.search(line) or _MERGER_RE.search(line) or _ALREADY_DL_RE.search(line)
                    if dest_m:
                        new_file = Path(dest_m.group("path").strip()).name
                        if new_file != current_file:
                            current_file = new_file
                            notified_finished = False

                    prog_m = _PROGRESS_RE.search(line)
                    if prog_m:
                        pct = min(100, int(float(prog_m.group("pct"))))
                        q.put({
                            "type": "progress",
                            "file": current_file,
                            "percent": pct,
                            "speed": prog_m.group("speed"),
                            "eta": prog_m.group("eta"),
                        })

                    if _FINISHED_RE.search(line) and current_file and not notified_finished:
                        q.put({"type": "finished_file", "file": current_file})
                        notified_finished = True

            proc.wait()

            if proc.returncode == 0 and current_file and not notified_finished:
                q.put({"type": "finished_file", "file": current_file})

            if proc.returncode != 0:
                if has_ffmpeg_error:
                    q.put({
                        "type": "error",
                        "url": url,
                        "message": (
                            "FFmpeg is required for MP3 conversion and MP4 merging, "
                            "but was not found on your PATH. Install it via 'winget install Gyan.FFmpeg' "
                            "in Command Prompt and restart."
                        ),
                        "dependency": "FFmpeg",
                    })
                elif has_atomic_error:
                    q.put({
                        "type": "error",
                        "url": url,
                        "message": (
                            "AtomicParsley is required to embed thumbnails "
                            "but was not found on your PATH. "
                            "Download it from https://github.com/wez/atomicparsley/releases "
                            "and place it on PATH, then retry."
                        ),
                        "dependency": "AtomicParsley",
                    })
                elif not last_error:
                    q.put({
                        "type": "error",
                        "url": url,
                        "message": f"yt-dlp exited with code {proc.returncode}",
                    })

        except FileNotFoundError:
            q.put({
                "type": "error",
                "url": url,
                "message": "yt-dlp executable not found. Ensure dependencies are installed.",
            })
        except OSError as exc:
            q.put({"type": "error", "url": url, "message": f"OS error launching yt-dlp: {exc}"})

    q.put({"type": "done"})


# ── Routes ────────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder=str(TEMPLATE_DIR))


@app.route("/")
def index():
    return render_template("index.html", downloads_dir=str(MUSIC_ROOT))


@app.route("/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True, silent=True) or {}
    raw_urls: str = data.get("urls", "")
    fmt: str = str(data.get("format", "mp3")).lower()
    token: str = str(data.get("token", "default"))

    raw_meta = data.get("embed_metadata", False)
    if isinstance(raw_meta, str):
        embed_metadata: bool = raw_meta.lower() == "true"
    else:
        embed_metadata = bool(raw_meta)

    if fmt not in ("mp3", "mp4"):
        return jsonify({"error": "Invalid format. Choose 'mp3' or 'mp4'."}), 400

    urls = [u.strip() for u in raw_urls.splitlines() if u.strip()]
    if not urls:
        return jsonify({"error": "No URLs provided."}), 400

    t = threading.Thread(
        target=_download_worker,
        args=(urls, fmt, embed_metadata, token),
        daemon=True,
    )
    t.start()

    return jsonify({
        "status": "started",
        "count": len(urls),
        "format": fmt,
        "embed_metadata": embed_metadata,
    })


@app.route("/progress/<token>")
def progress_stream(token: str):
    """Server-Sent Events for real-time download progress."""
    def generate():
        q = _get_queue(token)
        try:
            while True:
                try:
                    msg = q.get(timeout=60)
                except queue.Empty:
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue

                payload = json.dumps(msg)
                yield f"data: {payload}\n\n"

                if msg.get("type") == "done":
                    break
        finally:
            _remove_queue(token)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/open-folder")
def open_folder():
    """Open the music root in the OS file explorer."""
    folder = str(MUSIC_ROOT)
    try:
        if sys.platform == "win32":
            os.startfile(folder)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return jsonify({"status": "opened", "path": folder})
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n  YT-Downloader running at  http://127.0.0.1:5000")
    print(f"  Files saved to:           {MUSIC_ROOT}")
    print(f"  yt-dlp command:           {' '.join(YTDLP_CMD)}")
    print(f"  Template directory:       {TEMPLATE_DIR}\n")
    app.run(host="127.0.0.1", debug=False, threaded=True, port=5000)