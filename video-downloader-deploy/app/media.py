"""Shared validation and format selection; never accepts a user-supplied format expression."""
from __future__ import annotations

import math
import re
from urllib.parse import parse_qs, urlsplit

MAX_BYTES = 500 * 1024 * 1024
MAX_DURATION = 2 * 60 * 60
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class PublicError(Exception):
    pass


def normalize_url(value: str) -> str:
    try:
        if len(value) > 2048 or value != value.strip():
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port is not None:
            raise ValueError
        host = parsed.hostname
        if host == "youtu.be":
            video_id = parsed.path.removeprefix("/")
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            if parsed.path == "/watch":
                ids = parse_qs(parsed.query).get("v", [])
                if len(ids) != 1:
                    raise ValueError
                video_id = ids[0]
            else:
                match = re.fullmatch(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})/?", parsed.path)
                if not match:
                    raise ValueError
                video_id = match.group(1)
        else:
            raise ValueError
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PublicError("Use an HTTPS link to one public YouTube video.") from None
    return f"https://www.youtube.com/watch?v={video_id}"


def validate_info(info: dict, expected_id: str) -> None:
    if info.get("_type", "video") != "video" or info.get("id") != expected_id:
        raise PublicError("Choose one public video, not a playlist or channel.")
    if info.get("availability") != "public":
        raise PublicError("Only publicly available videos are supported; private, unlisted, paid, and members-only videos are not allowed.")
    if info.get("age_limit", 0) or info.get("is_private") or info.get("is_unlisted"):
        raise PublicError("Age-restricted, private, and unlisted videos are not supported.")
    if info.get("is_live") or info.get("is_upcoming") or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}:
        raise PublicError("Live and upcoming videos are not supported. Try a completed video.")
    if info.get("has_drm"):
        raise PublicError("DRM-protected videos are not supported.")
    duration = info.get("duration")
    if duration is not None and (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0 or duration > MAX_DURATION):
        raise PublicError("Choose a video no longer than two hours.")


def size_of(fmt: dict, duration: float | None) -> tuple[int | None, bool]:
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (float, int)) and math.isfinite(value) and value > 0:
            return int(value), key != "filesize"
    rate = fmt.get("tbr")
    if duration and isinstance(rate, (int, float)) and math.isfinite(rate) and rate > 0:
        return int(rate * 1000 * duration / 8), True
    return None, True


def select_options(info: dict) -> list[dict]:
    """Prefer AVC MP4 with AAC audio; otherwise WebM, with muxed formats included."""
    duration = info.get("duration")
    formats = [f for f in info.get("formats", []) if
               not f.get("has_drm") and f.get("protocol") in {"http", "https"}
               and isinstance(f.get("format_id"), str)
               and re.fullmatch(r"[A-Za-z0-9_-]+", f["format_id"])]
    candidates: dict[int, tuple[tuple, dict]] = {}
    for video in formats:
        if video.get("vcodec") in {None, "none"} or not video.get("height"):
            continue
        ext = video.get("ext")
        if ext == "mp4" and str(video.get("vcodec", "")).startswith(("avc1", "h264")):
            preference = 2
            audio_ext = "m4a"
        elif ext == "webm":
            preference = 1
            audio_ext = "webm"
        else:
            continue
        audio = None
        if video.get("acodec") in {None, "none"}:
            audios = [a for a in formats if a.get("vcodec") == "none" and a.get("acodec") not in {None, "none"}
                      and a.get("ext") == audio_ext
                      and (audio_ext != "m4a" or str(a.get("acodec")).startswith(("mp4a", "aac")))]
            if not audios:
                continue
            audio = max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        elif ext == "mp4" and not str(video.get("acodec")).startswith(("mp4a", "aac")):
            continue
        parts = [video] + ([audio] if audio else [])
        sizes = [size_of(f, duration) for f in parts]
        total = sum(s[0] for s in sizes) if all(s[0] is not None for s in sizes) else None
        if total is not None and total > MAX_BYTES:
            continue
        height = int(video["height"])
        fps = video.get("fps") or 0
        option = {"format": "+".join(f["format_id"] for f in parts), "ext": ext,
                  "label": f"{height}p{' · ' + str(round(fps)) + ' fps' if fps > 30 else ''} · {ext.upper()}",
                  "size_bytes": total, "estimated": any(s[1] for s in sizes)}
        rank = (preference, fps, video.get("tbr") or 0)
        if height not in candidates or rank > candidates[height][0]:
            candidates[height] = (rank, option)
    return [candidates[h][1] for h in sorted(candidates, reverse=True)][:12]
