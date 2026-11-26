import copy
import os
import re
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

import yt_dlp
from flask import Flask, jsonify, render_template, request
from yt_dlp.utils import DownloadCancelled, sanitize_filename

app = Flask(__name__)

DOWNLOAD_DIR = os.path.abspath("downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Konfigurasi
JOB_RETENTION_SECONDS = 3600  # 1 jam
MAX_PLAYLIST_ITEMS = 30      # batas video yang dimuat dari satu playlist
MP3_BITRATES = ["128", "192", "320"]

jobs = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def clean_error(error):
    """Merapikan pesan error yt-dlp (hapus kode warna & awalan 'ERROR:')."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(error)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    return text or "Terjadi kesalahan yang tidak diketahui"


def valid_resolution(value):
    """'best' atau angka tinggi video (mis. 1080)."""
    if value == "best":
        return True
    return bool(value) and value.isdigit() and 100 <= int(value) <= 4320


def url_scope(url):
    """Mendeteksi apakah URL menunjuk ke video, playlist, atau keduanya."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    has_list = "list" in query
    has_video = "v" in query or (
        parsed.netloc.endswith("youtu.be") and parsed.path.strip("/") != ""
    )
    list_id = query.get("list", [""])[0]
    return has_video, has_list, list_id.startswith("RD")


def pick_thumbnail(entry):
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    thumbs = entry.get("thumbnails") or []
    return thumbs[-1].get("url") if thumbs else None


def estimate_size(fmt, duration):
    size = fmt.get("filesize") or fmt.get("filesize_approx")
    if not size and fmt.get("tbr") and duration:
        size = fmt["tbr"] * 1000 / 8 * duration
    return int(size) if size else None


def build_heights(info):
    """Daftar resolusi yang benar-benar tersedia + perkiraan ukuran file."""
    duration = info.get("duration")
    formats = info.get("formats") or []

    audio_sizes = [
        estimate_size(f, duration)
        for f in formats
        if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")
    ]
    audio_size = max([s for s in audio_sizes if s], default=0)

    by_height = {}
    for f in formats:
        height = f.get("height")
        if not height or f.get("vcodec") in (None, "none"):
            continue
        size = estimate_size(f, duration)
        current = by_height.get(height)
        if current is None or (size or 0) > (current or 0):
            by_height[height] = size

    return [
        {
            "height": height,
            "size": (size + audio_size) if size else None,
        }
        for height, size in sorted(by_height.items(), reverse=True)
    ]


def cleanup_jobs():
    """Menghapus job selesai/gagal yang sudah lebih dari 1 jam."""
    now = time.time()

    with jobs_lock:
        expired_jobs = [
            job_id
            for job_id, job in jobs.items()
            if job.get("status") in ["completed", "error", "cancelled"]
            and now - job.get("finished_at", now) > JOB_RETENTION_SECONDS
        ]

        for job_id in expired_jobs:
            jobs.pop(job_id, None)


# ---------------------------------------------------------------------------
# Ambil info (dipanggil lazy saat user menekan "Lanjut")
# ---------------------------------------------------------------------------
def fetch_info(url, scope):
    has_video, has_list, is_mix = url_scope(url)

    # scope: auto | video | playlist
    only_video = scope == "video" or (scope == "auto" and not has_list)

    options = {
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "skip_download": True,
        "noplaylist": only_video,
        "extract_flat": "in_playlist",
        "playlistend": MAX_PLAYLIST_ITEMS,
        "socket_timeout": 15,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("Tidak ada informasi yang bisa dibaca dari link ini")

    # ---- Playlist / Mix ----
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        entries = []
        skipped = 0

        for index, entry in enumerate(info.get("entries") or [], start=1):
            if not entry:
                skipped += 1
                continue

            title = entry.get("title") or ""
            video_id = entry.get("id")
            video_url = entry.get("url") or entry.get("webpage_url")

            if not video_url and video_id:
                video_url = f"https://www.youtube.com/watch?v={video_id}"

            unavailable = (
                not video_url
                or title in ("[Private video]", "[Deleted video]")
            )
            if unavailable:
                skipped += 1
                continue

            entries.append({
                "index": index,
                "id": video_id,
                "title": title or "(Tanpa judul)",
                "url": video_url,
                "duration": entry.get("duration"),
                "uploader": entry.get("uploader") or entry.get("channel"),
                "thumbnail": pick_thumbnail(entry),
            })

        if not entries:
            raise ValueError("Playlist kosong atau semua videonya tidak tersedia")

        return {
            "type": "playlist",
            "title": info.get("title") or "Playlist",
            "uploader": info.get("uploader") or info.get("channel"),
            "is_mix": is_mix,
            "has_single": has_video,
            "truncated": len(entries) + skipped >= MAX_PLAYLIST_ITEMS,
            "skipped": skipped,
            "entries": entries,
        }

    # ---- Satu video ----
    if info.get("is_live"):
        raise ValueError("Live stream belum bisa diunduh")

    return {
        "type": "video",
        "title": info.get("title") or "(Tanpa judul)",
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "thumbnail": pick_thumbnail(info),
        "url": info.get("webpage_url") or url,
        "heights": build_heights(info),
        "has_playlist": has_list,
    }


# ---------------------------------------------------------------------------
# Proses konversi (berjalan di thread)
# ---------------------------------------------------------------------------
def run_conversion(job_id, items, format_type, resolution, bitrate, folder_name):
    """items: list of {"index", "title", "url"}."""
    total = len(items)
    is_playlist = folder_name is not None
    state = {"item": 0, "files_done": 0, "fraction": 0.0}

    # Satu file video = (video + audio) => dua unduhan; MP3 hanya satu.
    expected_files = 2 if format_type == "mp4" else 1

    def update(message=None, item_fraction=None):
        with jobs_lock:
            job = jobs[job_id]
            if item_fraction is not None:
                state["fraction"] = max(state["fraction"], item_fraction)

            done = state["item"]
            overall = (done + state["fraction"]) / total * 100

            job["progress"] = round(min(overall, 99), 1)
            job["current"] = min(state["item"] + 1, total)
            if message:
                job["message"] = message

    def check_cancel():
        with jobs_lock:
            if jobs[job_id].get("cancel_requested"):
                raise DownloadCancelled("Dibatalkan oleh pengguna")

    def progress_hook(data):
        check_cancel()

        if data["status"] == "downloading":
            size = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes", 0)
            file_fraction = (downloaded / size) if size else 0

            fraction = (state["files_done"] + file_fraction) / expected_files
            update("Mengunduh media...", min(fraction, 0.95))

        elif data["status"] == "finished":
            state["files_done"] += 1
            fraction = min(state["files_done"] / expected_files, 0.95)
            update("Memproses hasil...", fraction)

    def postprocessor_hook(data):
        if data["status"] != "started":
            return
        name = data.get("postprocessor", "")
        if "Merger" in name:
            update("Menggabungkan video dan audio...", 0.97)
        elif "ExtractAudio" in name:
            update("Mengonversi ke MP3...", 0.97)

    if format_type == "mp3":
        quality_label = f"MP3-{bitrate}kbps"
    else:
        quality_label = "MP4-Best" if resolution == "best" else f"MP4-{resolution}p"

    if is_playlist:
        target_dir = os.path.join(DOWNLOAD_DIR, folder_name)
    else:
        target_dir = DOWNLOAD_DIR
    os.makedirs(target_dir, exist_ok=True)

    with jobs_lock:
        jobs[job_id]["folder"] = target_dir
        jobs[job_id]["status"] = "processing"

    unique_id = job_id[:8]
    cancelled = False

    try:
        for position, item in enumerate(items):
            state["item"] = position
            state["files_done"] = 0
            state["fraction"] = 0.0

            check_cancel()

            with jobs_lock:
                jobs[job_id]["items"][position]["status"] = "downloading"
                jobs[job_id]["current_title"] = item["title"]
            update("Menyiapkan unduhan...")

            if is_playlist:
                prefix = f"{item['index']:02d} - "
                filename = f"{prefix}%(title)s [{quality_label}].%(ext)s"
            else:
                filename = f"%(title)s [{quality_label}] [{unique_id}].%(ext)s"

            options = {
                # '%' di path harus di-escape karena outtmpl adalah template
                "outtmpl": os.path.join(target_dir.replace("%", "%%"), filename),
                "progress_hooks": [progress_hook],
                "postprocessor_hooks": [postprocessor_hook],
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "no_color": True,
            }

            if format_type == "mp3":
                options.update({
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": bitrate,
                    }],
                })
            else:
                if resolution == "best":
                    video_format = "bestvideo+bestaudio/best"
                else:
                    video_format = (
                        f"bestvideo[height<={resolution}]"
                        f"+bestaudio/best[height<={resolution}]"
                    )

                options.update({
                    "format": video_format,
                    "merge_output_format": "mp4",
                })

            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    ydl.download([item["url"]])

                with jobs_lock:
                    jobs[job_id]["items"][position]["status"] = "done"
                    jobs[job_id]["succeeded"] += 1

            except DownloadCancelled:
                raise

            except Exception as e:
                with jobs_lock:
                    jobs[job_id]["items"][position].update({
                        "status": "failed",
                        "error": clean_error(e),
                    })
                    jobs[job_id]["failed"] += 1

            # Satu item selesai (berhasil/gagal) -> geser progres keseluruhan
            state["item"] = position + 1
            state["fraction"] = 0.0
            update()

    except DownloadCancelled:
        cancelled = True

    with jobs_lock:
        job = jobs[job_id]
        job["finished_at"] = time.time()
        job["current_title"] = None

        if cancelled:
            # Item yang sedang berjalan ditandai batal
            for entry in job["items"]:
                if entry["status"] in ("downloading", "pending"):
                    entry["status"] = "cancelled"
            job["status"] = "cancelled"
            job["message"] = "Dibatalkan"
        elif job["succeeded"] == 0:
            job["status"] = "error"
            first_error = next(
                (e["error"] for e in job["items"] if e.get("error")),
                "Semua unduhan gagal",
            )
            job["message"] = first_error
        else:
            job["status"] = "completed"
            job["progress"] = 100
            job["message"] = "Konversi berhasil!"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/info", methods=["POST"])
def info():
    """Dipanggil saat user menekan 'Lanjut' (lazy load pilihan kualitas)."""
    url = request.form.get("url", "").strip()
    scope = request.form.get("scope", "auto")

    if not url:
        return jsonify({"error": "URL tidak boleh kosong"}), 400

    if scope not in ["auto", "video", "playlist"]:
        return jsonify({"error": "Pilihan cakupan tidak valid"}), 400

    try:
        return jsonify(fetch_info(url, scope))
    except Exception as e:
        return jsonify({"error": clean_error(e)}), 422


@app.route("/convert", methods=["POST"])
def convert():

    cleanup_jobs()

    url = request.form.get("url", "").strip()
    format_type = request.form.get("format")
    resolution = request.form.get("resolution", "best")
    bitrate = request.form.get("bitrate", "192")
    playlist_title = request.form.get("playlist_title", "").strip()

    # Playlist: daftar video terpilih dikirim berulang sebagai "video"
    # dengan format "index|url|judul".
    selected = request.form.getlist("video")

    if format_type not in ["mp3", "mp4"]:
        return jsonify({"error": "Format tidak valid"}), 400

    if not valid_resolution(resolution):
        return jsonify({"error": "Resolusi tidak valid"}), 400

    if bitrate not in MP3_BITRATES:
        return jsonify({"error": "Bitrate tidak valid"}), 400

    items = []
    folder_name = None

    if selected:
        for raw in selected[:MAX_PLAYLIST_ITEMS]:
            parts = raw.split("|", 2)
            if len(parts) != 3 or not parts[0].isdigit() or not parts[1].startswith("http"):
                return jsonify({"error": "Daftar video tidak valid"}), 400
            items.append({
                "index": int(parts[0]),
                "url": parts[1],
                "title": parts[2] or "(Tanpa judul)",
            })

        folder_name = (
            sanitize_filename(playlist_title or "Playlist").strip(". ")
            or "Playlist"
        )
    elif url:
        items.append({"index": 1, "url": url, "title": url})
    else:
        return jsonify({"error": "URL tidak boleh kosong"}), 400

    job_id = str(uuid.uuid4())

    with jobs_lock:
        jobs[job_id] = {
            "status": "starting",
            "progress": 0,
            "message": "Menyiapkan konversi...",
            "total": len(items),
            "current": 1,
            "current_title": None,
            "succeeded": 0,
            "failed": 0,
            "is_playlist": folder_name is not None,
            "folder": None,
            "cancel_requested": False,
            "items": [
                {"title": item["title"], "status": "pending"}
                for item in items
            ],
        }

    thread = threading.Thread(
        target=run_conversion,
        args=(job_id, items, format_type, resolution, bitrate, folder_name),
        daemon=True,
    )
    thread.start()

    return jsonify({
        "job_id": job_id
    })


@app.route("/progress/<job_id>")
def progress(job_id):

    cleanup_jobs()

    with jobs_lock:
        job = jobs.get(job_id)

        if not job:
            return jsonify({
                "error": "Job tidak ditemukan atau sudah kedaluwarsa"
            }), 404

        return jsonify(copy.deepcopy(job))


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id):

    with jobs_lock:
        job = jobs.get(job_id)

        if not job:
            return jsonify({"error": "Job tidak ditemukan"}), 404

        if job["status"] in ["starting", "processing"]:
            job["cancel_requested"] = True
            job["message"] = "Membatalkan..."

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, threaded=True)
