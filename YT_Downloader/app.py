"""
YouTube / YouTube Music Downloader
Backend: Flask + yt-dlp  (subprocess mode — no shell=True)
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

# ── Output path ───────────────────────────────────────────────────────────────
# Playlist-aware template: organises tracks under their playlist folder.
MUSIC_ROOT = Path(r"D:\Music")
MUSIC_ROOT.mkdir(parents=True, exist_ok=True)
OUTPUT_TEMPLATE = str(MUSIC_ROOT / r"%(playlist_title,NA)s\%(title)s.%(ext)s")

# ── yt-dlp binary check ────────────────────────────────────────────────────────
YTDLP_BIN = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
if YTDLP_BIN is None:
    sys.exit(
        "[ERROR] yt-dlp binary not found on PATH.\n"
        "Install it with:  pip install yt-dlp   (then restart your terminal)"
    )

# Substring that appears in yt-dlp stderr when AtomicParsley is missing
_ATOMICPARSLEY_HINT = "atomicparsley"

# ── SSE progress queues (keyed by session token) ───────────────────────────────
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


# ── CLI argument builder ───────────────────────────────────────────────────────

def _build_args(fmt: str, embed_metadata: bool) -> list[str]:
    """
    Build a yt-dlp argument list for subprocess.run / Popen.

    Every element is a discrete string — never joined into a shell command —
    so user-supplied URLs cannot inject additional shell tokens.

    Args:
        fmt:            "mp3" or "mp4"
        embed_metadata: When True, appends --embed-metadata, --embed-thumbnail,
                        and two --parse-metadata rules for album / album-artist
                        tags.  When False these flags are omitted entirely.
    """
    args: list[str] = [
        YTDLP_BIN,
        "--no-playlist",          # treat bare watch URLs as single tracks
        "--ignore-errors",        # skip unavailable items in playlists
        "--newline",              # one progress line per stdout line (parseable)
        "--progress",
        "--output", OUTPUT_TEMPLATE,
    ]

    # ── Format-specific flags ──────────────────────────────────────────────────
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

    # ── Optional metadata / thumbnail embedding ────────────────────────────────
    if embed_metadata:
        args += [
            "--embed-metadata",
            "--embed-thumbnail",
            # Map playlist title (fallback: video title) → MP3/MP4 album tag
            "--parse-metadata", "%(playlist_title,title)s:%(meta_album)s",
            # Map uploader → album artist tag
            "--parse-metadata", "%(uploader,uploader)s:%(meta_album_artist)s",
        ]

    return args



# ── Progress line parser ───────────────────────────────────────────────────────
# Matches lines like:  [download]  45.3% of 5.23MiB at 1.23MiB/s ETA 00:03
_PROGRESS_RE = re.compile(
    r"\[download\]\s+(?P<pct>[\d.]+)%\s+of\s+[\d.]+\S+\s+at\s+(?P<speed>\S+)\s+ETA\s+(?P<eta>\S+)"
)
# Matches:  [download] Destination: some/path/file.mp3
_DEST_RE = re.compile(r"\[download\] Destination:\s+(?P<path>.+)")
# Matches post-processor lines marking a file as finished
_FINISHED_RE = re.compile(r"\[(?:ffmpeg|EmbedThumbnail|MetadataFromField)\]")


def _download_worker(urls: list[str], fmt: str, embed_metadata: bool, token: str) -> None:
    """
    Run yt-dlp as a subprocess for each URL, streaming progress to the SSE queue.

    Uses subprocess.Popen with a pre-built argument *list* (shell=False by default),
    so no URL content can be interpreted as shell syntax.
    """
    q = _get_queue(token)
    base_args = _build_args(fmt, embed_metadata)

    for url in urls:
        url = url.strip()
        if not url:
            continue

        q.put({"type": "start_url", "url": url})
        current_file: str = ""
        stderr_lines: list[str] = []

        try:
            # ── Launch yt-dlp — shell=False (list args, never a shell string) ──
            # Merge stderr into stdout to prevent OS pipe buffer deadlocks on large playlists.
            proc = subprocess.Popen(
                [*base_args, url],          # URL appended as a discrete element
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,   # Merge stderr to prevent deadlocks
                stdin=subprocess.DEVNULL,   # Prevent hanging on input prompts
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,               # explicit for clarity / linting
            )

            has_atomic_error = False
            last_error = ""

            # ── Stream stdout line by line ─────────────────────────────────────
            for line in proc.stdout:
                line = line.rstrip()
                if not line:
                    continue

                # Check for errors in real-time
                if "atomicparsley" in line.lower():
                    has_atomic_error = True
                elif line.startswith("ERROR:"):
                    last_error = line
                    # Emit non-blocking error to UI
                    q.put({"type": "error", "url": url, "message": line})

                dest_m = _DEST_RE.search(line)
                if dest_m:
                    current_file = Path(dest_m.group("path")).name

                prog_m = _PROGRESS_RE.search(line)
                if prog_m:
                    q.put({
                        "type": "progress",
                        "file": current_file,
                        "percent": int(float(prog_m.group("pct"))),
                        "speed": prog_m.group("speed"),
                        "eta": prog_m.group("eta"),
                    })

                if _FINISHED_RE.search(line) and current_file:
                    q.put({"type": "finished_file", "file": current_file})

            # ── Wait for exit ──────────────────────────────
            proc.wait()

            if proc.returncode != 0:
                # ── AtomicParsley check ────────────────────────────────────────
                if has_atomic_error:
                    q.put({
                        "type": "error",
                        "url": url,
                        "message": (
                            "AtomicParsley is required to embed thumbnails into MP3 files "
                            "but was not found on your PATH. "
                            "Download it from https://github.com/wez/atomicparsley/releases "
                            "and place it next to ffmpeg (or anywhere on PATH), then retry."
                        ),
                        "dependency": "AtomicParsley",
                    })
                elif not last_error:
                    # Generic non-zero exit without a specific ERROR: line
                    q.put({"type": "error", "url": url, "message": f"yt-dlp exited with code {proc.returncode}"})

        except FileNotFoundError:
            # yt-dlp binary disappeared between startup check and this call
            q.put({
                "type": "error",
                "url": url,
                "message": "yt-dlp binary not found. Ensure it is installed and on PATH.",
            })
        except OSError as exc:
            q.put({"type": "error", "url": url, "message": f"OS error launching yt-dlp: {exc}"})

    q.put({"type": "done"})




# ── Routes ─────────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html", downloads_dir=str(MUSIC_ROOT))


@app.route("/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True)
    raw_urls: str = data.get("urls", "")
    fmt: str = data.get("format", "mp3").lower()
    token: str = data.get("token", "default")

    # embed_metadata: optional boolean (default False for a faster, lightweight
    # download).  Accept both JSON booleans and the strings "true"/"false".
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
    if sys.platform == "win32":
        os.startfile(folder)          # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", folder])
    else:
        subprocess.Popen(["xdg-open", folder])
    return jsonify({"status": "opened", "path": folder})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n  YT-Downloader running at  http://127.0.0.1:5000")
    print(f"  Files saved to:           {MUSIC_ROOT}")
    print(f"  yt-dlp binary:            {YTDLP_BIN}")
    print(f"  Metadata embedding:       pass embed_metadata=true in POST body\n")
    app.run(debug=False, threaded=True, port=5000)
