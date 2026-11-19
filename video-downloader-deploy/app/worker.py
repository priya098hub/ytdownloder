"""Isolated yt-dlp worker. Communicates only JSON lines over stdin/stdout."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import traceback
from pathlib import Path

# Isolated Python (-I) omits the script directory; add only this trusted app module path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from media import MAX_BYTES, PublicError, select_options, validate_info


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class QuietLogger:
    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        pass

    def error(self, _message: str) -> None:
        pass


def params() -> dict:
    node = shutil.which("node") or "node"
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreconfig": True,
        "proxy": "",
        "noplaylist": True,
        "allowed_extractors": [r"^Youtube$"],
        "js_runtimes": {"node": {"path": node}},
        "remote_components": [],
        "logger": QuietLogger(),
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "file_access_retries": 1,
        "windowsfilenames": True,
        "restrictfilenames": True,
        "nooverwrites": False,
        "continuedl": False,
    }


def run(payload: dict) -> None:
    try:
        from yt_dlp import YoutubeDL
        mode = payload.get("mode")
        url = payload.get("url")
        if mode not in {"analyze", "download"} or not isinstance(url, str) or not url.startswith("https://www.youtube.com/watch?v="):
            emit({"type": "error", "code": "internal"})
            return
        video_id = url.rsplit("=", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            emit({"type": "error", "code": "internal"})
            return

        if mode == "analyze":
            options = params()
            options["skip_download"] = True
            options["extract_flat"] = False
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise PublicError("YouTube did not return a video. Check the link and try again.")
            validate_info(info, video_id)
            choices = select_options(info)
            if not choices:
                raise PublicError("No supported video-and-audio formats under 500 MiB were found. Try a lower-resolution public video.")
            safe_formats = []
            for fmt in info.get("formats", []):
                keys = ("format_id", "ext", "height", "vcodec", "acodec", "filesize", "filesize_approx", "tbr", "abr", "fps", "protocol", "has_drm")
                safe_formats.append({k: fmt.get(k) for k in keys if fmt.get(k) is not None})
            emit({"type": "result", "id": video_id, "title": str(info.get("title") or "YouTube video")[:300],
                  "channel": str(info.get("channel") or info.get("uploader") or "YouTube channel")[:200],
                  "duration": info.get("duration"), "availability": info.get("availability"),
                  "age_limit": info.get("age_limit", 0), "is_private": info.get("is_private", False),
                  "is_unlisted": info.get("is_unlisted", False), "is_live": info.get("is_live", False),
                  "is_upcoming": info.get("is_upcoming", False), "live_status": info.get("live_status"),
                  "has_drm": info.get("has_drm", False), "formats": safe_formats, "choices": choices})
            return

        format_string = payload.get("format")
        ext = payload.get("ext")
        output_dir = Path(payload.get("output_dir", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\+[A-Za-z0-9_-]+)?", str(format_string)) or ext not in {"mp4", "webm"}:
            emit({"type": "error", "code": "internal"})
            return
        output_dir.mkdir(parents=True, exist_ok=True)
        if output_dir.is_symlink():
            emit({"type": "error", "code": "internal"})
            return
        format_ids = format_string.split("+")
        known_formats = payload.get("formats")
        if not isinstance(known_formats, list):
            emit({"type": "error", "code": "internal"})
            return
        known_ids = {str(f.get("format_id")) for f in known_formats if isinstance(f, dict)}
        if not set(format_ids).issubset(known_ids):
            emit({"type": "error", "code": "internal"})
            return

        # Track progress without forwarding titles, URLs, paths, or extractor messages.
        last_progress = -1
        last_status = ""
        completed_parts = 0

        def progress_hook(data: dict) -> None:
            nonlocal last_progress, last_status, completed_parts
            status = data.get("status")
            if status == "download" or status == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                current = data.get("downloaded_bytes")
                # Weight each stream part equally across the whole job.
                share = 95.0 / len(format_ids)
                base = completed_parts * share
                fraction = (current / total) if isinstance(total, (int, float)) and total > 0 and isinstance(current, (int, float)) else 0
                percent = max(0, min(97, int(base + fraction * share)))
                if percent != last_progress or last_status != status:
                    emit({"type": "progress", "progress": percent, "message": "Downloading video and audio…"})
                    last_progress, last_status = percent, status
            elif status == "finished":
                completed_parts += 1
                if last_status != "processing":
                    emit({"type": "progress", "progress": min(97, completed_parts * int(95.0 / len(format_ids))), "message": "Downloading video and audio…"})

        def postprocessor_hook(data: dict) -> None:
            nonlocal last_status
            if data.get("status") in {"started", "processing"}:
                last_status = "processing"
                emit({"type": "progress", "progress": 99, "message": "Combining video and audio…", "status": "processing"})

        rejected = ""

        def permitted_video(info: dict, incomplete: bool = False):
            # Never download a video that the analysis path would refuse.
            nonlocal rejected
            if incomplete:
                return None
            try:
                validate_info(info, video_id)
            except PublicError as exc:
                rejected = str(exc)[:240]
                return rejected
            return None

        options = params()
        options.update({
            "match_filter": permitted_video,
            "format": format_string,
            "merge_output_format": ext,
            "max_filesize": MAX_BYTES,
            "paths": {"home": str(output_dir), "temp": str(output_dir)},
            "outtmpl": {"default": "video.%(ext)s"},
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
        })
        with YoutubeDL(options) as ydl:
            ydl.extract_info(url, download=True)
        if rejected:
            raise PublicError(rejected)
        candidates = []
        for item in output_dir.iterdir():
            if item.name in {f"video.{ext}", f"video.{ext}.part"} and item.name.endswith(f".{ext}"):
                if item.is_symlink() or not item.is_file():
                    continue
                if item.suffix != f".{ext}":
                    continue
                if item.stat().st_size > MAX_BYTES:
                    emit({"type": "error", "code": "too_large"})
                    return
                if item.stat().st_size > 0:
                    candidates.append(item)
        if len(candidates) != 1:
            emit({"type": "error", "code": "unavailable"})
            return
        emit({"type": "result", "filename": candidates[0].name, "size": candidates[0].stat().st_size})
    except PublicError as exc:
        emit({"type": "error", "code": "public", "message": str(exc)[:240]})
    except BaseException:
        # Deliberately suppress yt-dlp exceptions: their text may contain URLs or local details.
        emit({"type": "error", "code": "unavailable"})


def main() -> None:
    try:
        raw = sys.stdin.readline(16_385)
        if not raw or len(raw) > 16_384:
            emit({"type": "error", "code": "internal"})
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            emit({"type": "error", "code": "internal"})
            return
        run(payload)
    except BaseException:
        emit({"type": "error", "code": "internal"})


if __name__ == "__main__":
    main()
