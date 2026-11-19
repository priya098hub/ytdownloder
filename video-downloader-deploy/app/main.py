"""Local-only FastAPI service for authorized public YouTube downloads."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from fastapi.exceptions import RequestValidationError
from urllib.parse import urlsplit

from .media import MAX_BYTES, MAX_DURATION, PublicError, normalize_url

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
DATA_ROOT = Path(os.environ.get("YT_DOWNLOADER_DATA", str(Path(tempfile.gettempdir()) / "local-youtube-downloader"))).resolve()
ANALYSIS_TTL = 30 * 60
JOB_TTL = 60 * 60
ANALYZE_TIMEOUT = 110
DOWNLOAD_TIMEOUT = 40 * 60
MAX_BODY = 8192
MAX_ANALYSES = 8
MAX_JOBS = 16
ID_RE = re.compile(r"^[a-f0-9]{32}$")
FORMAT_RE = re.compile(r"^[A-Za-z0-9_-]+(?:\+[A-Za-z0-9_-]+)?$")


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=2048)
    url: str


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    analysis_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    option_id: str = Field(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
    authorized: StrictBool


analyses: OrderedDict[str, dict[str, Any]] = OrderedDict()
jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
analysis_semaphore = asyncio.Semaphore(1)
download_semaphore = asyncio.Semaphore(1)
worker_tasks: set[asyncio.Task] = set()
cleanup_task: asyncio.Task | None = None


def safe_error(message: str, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=message)


def prune_memory() -> None:
    now = time.monotonic()
    for key in list(analyses):
        if now - analyses[key]["created"] > ANALYSIS_TTL:
            analyses.pop(key, None)
    for key in list(jobs):
        if now - jobs[key]["created"] > JOB_TTL and jobs[key]["status"] in {"ready", "failed"}:
            entry = jobs.pop(key)
            cleanup_output(entry.get("directory"))
    while len(analyses) > MAX_ANALYSES:
        analyses.popitem(last=False)
    while len(jobs) > MAX_JOBS:
        key, entry = next(iter(jobs.items()))
        if entry["status"] in {"queued", "downloading", "processing"}:
            break
        jobs.pop(key)
        cleanup_output(entry.get("directory"))


def cleanup_output(directory: Path | None) -> None:
    if not isinstance(directory, Path):
        return
    try:
        resolved = directory.resolve(strict=False)
        if resolved.parent == DATA_ROOT and not directory.is_symlink():
            shutil.rmtree(resolved, ignore_errors=True)
    except (OSError, RuntimeError):
        pass


async def periodic_cleanup() -> None:
    while True:
        await asyncio.sleep(60)
        prune_memory()
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            for child in DATA_ROOT.iterdir():
                if child.is_symlink() or not child.is_dir():
                    continue
                try:
                    stale = time.time() - child.stat().st_mtime > JOB_TTL
                except OSError:
                    continue
                if stale:
                    shutil.rmtree(child, ignore_errors=True)
        except OSError:
            pass


def worker_command() -> list[str]:
    return [sys.executable, "-I", str(Path(__file__).with_name("worker.py"))]


async def run_worker(payload: dict, timeout: float | None = None) -> dict:
    """Stream bounded progress events from an isolated, time-limited worker."""
    if timeout is None:
        timeout = DOWNLOAD_TIMEOUT if payload.get("mode") == "download" else ANALYZE_TIMEOUT
    flags = 0
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        flags = getattr(__import__("subprocess"), "CREATE_NEW_PROCESS_GROUP", 0)
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    try:
        proc = await asyncio.create_subprocess_exec(
            *worker_command(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, **kwargs,
        )
    except (OSError, RuntimeError):
        raise PublicError("The downloader could not start. Check that Python and the required tools are installed.") from None
    async def consume() -> dict | None:
        worker_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
        proc.stdin.write((json.dumps(worker_payload, separators=(",", ":")) + "\n").encode())
        await proc.stdin.drain()
        proc.stdin.close()
        result = None
        received = 0
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            received += len(line)
            if received > 2 * 1024 * 1024 or len(line) > 64 * 1024:
                raise PublicError("The downloader returned an invalid response. Please try again.")
            try:
                data = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("type") == "progress":
                callback = payload.get("_on_progress")
                if callback:
                    callback(data)
            elif data.get("type") in {"result", "error"}:
                result = data
        await proc.wait()
        return result

    try:
        result = await asyncio.wait_for(consume(), timeout=timeout)
    except asyncio.TimeoutError:
        raise PublicError("The task timed out. Check your connection and try a shorter video or lower quality.") from None
    finally:
        # Cancellation and server shutdown must also stop FFmpeg and JavaScript children.
        if proc.returncode is None:
            await terminate_process(proc)
    if result is None:
        raise PublicError("YouTube could not be reached or the video is unavailable. Check the link and try again.")
    if result.get("type") == "error":
        code = result.get("code")
        messages = {
            "too_large": "The selected download is larger than 500 MiB. Choose a lower quality.",
            "unavailable": "This video could not be downloaded. Check that it is public and try another quality.",
            "internal": "The download request could not be prepared. Analyze the video again.",
        }
        raise PublicError(result.get("message") if code == "public" and isinstance(result.get("message"), str) else messages.get(code, "YouTube could not process this video. Check the link and try again."))
    if proc.returncode != 0:
        raise PublicError("YouTube could not process this video. Check the link and try again.")
    return result


async def terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(proc.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(killer.wait(), timeout=5)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, asyncio.TimeoutError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=5)


def local_origin_ok(request: Request) -> bool:
    host_header = request.headers.get("host", "").lower()
    public_origin = os.environ.get("PUBLIC_ORIGIN", "").strip().rstrip("/")
    public_host = None
    if public_origin:
        configured = urlsplit(public_origin)
        if (configured.scheme != "https" or not configured.hostname or configured.username or
                configured.password or configured.path or configured.query or configured.fragment):
            return False
        public_host = configured.netloc.lower()
    try:
        parsed = urlsplit(f"http://{host_header}")
        if ((parsed.hostname not in {"127.0.0.1", "localhost", "::1"} and host_header != public_host) or
                parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
    except ValueError:
        return False
    origin = request.headers.get("origin")
    if origin is not None:
        expected_origin = public_origin if host_header == public_host else f"{request.url.scheme}://{host_header}"
        return origin.lower() == expected_origin.lower()
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if host_header == public_host and request.method not in {"GET", "HEAD", "OPTIONS"}:
        return False
    return fetch_site not in {"cross-site", "same-site"} and (request.method in {"GET", "HEAD", "OPTIONS"} or fetch_site != "none")


async def security_middleware(request: Request, call_next):
    if not local_origin_ok(request):
        return JSONResponse({"detail": "This local app only accepts requests from its own page."}, status_code=403)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return JSONResponse({"detail": "Send this request as JSON."}, status_code=415)
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_BODY:
                    return JSONResponse({"detail": "Request is too large."}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid request length."}, status_code=400)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return JSONResponse({"detail": "Request is too large."}, status_code=413)
        request._body = bytes(body)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https://i.ytimg.com; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    return response


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global cleanup_task
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    cleanup_task = asyncio.create_task(periodic_cleanup())
    try:
        yield
    finally:
        if cleanup_task:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
        for task in list(worker_tasks):
            task.cancel()
        if worker_tasks:
            await asyncio.gather(*worker_tasks, return_exceptions=True)
        for job in jobs.values():
            if job["status"] not in {"ready", "failed"}:
                job["status"] = "failed"
                job["message"] = "The local app stopped before this download finished. Start it again and retry."
                cleanup_output(job.get("directory"))


app = FastAPI(title="Local YouTube Downloader", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.middleware("http")(security_middleware)

if STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "The request could not be completed. Check the link and try again."
    return JSONResponse({"detail": message}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, _exc: RequestValidationError):
    return JSONResponse({"detail": "The request contains invalid or missing fields. Check the link or analyze the video again."}, status_code=422)


@app.get("/", response_class=HTMLResponse)
async def home():
    index = STATIC / "index.html"
    if not index.is_file():
        raise safe_error("The local app page is not available. Check the app installation.", 503)
    return HTMLResponse(index.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health():
    return {"status": "ok", "local_only": True}


@app.post("/api/analyze")
async def analyze(body: AnalyzeRequest):
    prune_memory()
    try:
        url = normalize_url(body.url)
    except PublicError as exc:
        raise safe_error(str(exc)) from None
    if analysis_semaphore.locked():
        raise safe_error("Another video is being analyzed. Wait a moment and try again.", 429)
    try:
        async with analysis_semaphore:
            result = await run_worker({"mode": "analyze", "url": url})
    except PublicError as exc:
        raise safe_error(str(exc), 422) from None
    if result.get("id") != url.rsplit("=", 1)[-1]:
        raise safe_error("YouTube returned a different video. Check the link and try again.", 422)
    duration = result.get("duration")
    if duration is not None and (not isinstance(duration, (int, float)) or duration <= 0 or duration > MAX_DURATION):
        raise safe_error("Choose a completed video no longer than two hours.", 422)
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices:
        raise safe_error("No supported video-and-audio formats were found. Try a lower-resolution public video.", 422)
    analysis_id = secrets.token_hex(16)
    options = []
    format_map = {}
    for choice in choices:
        fmt = choice.get("format") if isinstance(choice, dict) else None
        ext = choice.get("ext") if isinstance(choice, dict) else None
        if not isinstance(fmt, str) or not FORMAT_RE.fullmatch(fmt) or ext not in {"mp4", "webm"}:
            continue
        if choice.get("size_bytes") is not None and (not isinstance(choice["size_bytes"], int) or choice["size_bytes"] <= 0 or choice["size_bytes"] > MAX_BYTES):
            continue
        option_id = secrets.token_hex(16)
        format_map[option_id] = {"format": fmt, "ext": ext}
        options.append({"id": option_id, "label": str(choice.get("label", "Video quality"))[:80], "ext": ext,
                        "size_bytes": choice.get("size_bytes"), "estimated": bool(choice.get("estimated"))})
    if not options:
        raise safe_error("No supported video-and-audio formats were found. Try another public video.", 422)
    allowed_ids = {part for option in format_map.values() for part in option["format"].split("+")}
    analyses[analysis_id] = {"created": time.monotonic(), "url": url,
                             "formats": [{"format_id": format_id} for format_id in allowed_ids], "options": format_map}
    return {"analysis_id": analysis_id, "title": str(result.get("title") or "YouTube video")[:300],
            "channel": str(result.get("channel") or "YouTube channel")[:200], "duration": duration,
            "thumbnail": f"https://i.ytimg.com/vi/{result['id']}/hqdefault.jpg", "options": options}


@app.post("/api/download", status_code=202)
async def download(body: DownloadRequest):
    if body.authorized is not True:
        raise safe_error("Confirm that you own the video or have permission to download it.", 403)
    prune_memory()
    record = analyses.get(body.analysis_id)
    if not record or time.monotonic() - record["created"] > ANALYSIS_TTL:
        raise safe_error("This video analysis expired. Analyze the link again.", 410)
    selected = record["options"].get(body.option_id)
    if not selected:
        raise safe_error("That quality is no longer available. Analyze the link again.", 400)
    if any(job["status"] in {"queued", "downloading", "processing"} for job in jobs.values()):
        raise safe_error("A download is already in progress. Wait for it to finish before starting another.", 429)
    job_id = secrets.token_hex(16)
    directory = DATA_ROOT / job_id
    jobs[job_id] = {"created": time.monotonic(), "status": "queued", "progress": None,
                    "message": "Preparing your video…", "directory": directory, "filename": None}

    async def execute_job() -> None:
        job = jobs.get(job_id)
        if not job:
            return
        try:
            async with download_semaphore:
                if job_id not in jobs:
                    return
                job["status"] = "downloading"
                job["message"] = "Connecting to YouTube…"
                directory.mkdir(parents=True, exist_ok=False)

                def on_progress(event: dict) -> None:
                    current = jobs.get(job_id)
                    if not current:
                        return
                    if event.get("status") == "processing":
                        current["status"] = "processing"
                    elif event.get("progress") is not None:
                        current["status"] = "downloading"
                        current["progress"] = max(0, min(99, int(event["progress"])))
                    if isinstance(event.get("message"), str):
                        current["message"] = event["message"][:120]

                payload = {"mode": "download", "url": record["url"], "format": selected["format"], "ext": selected["ext"],
                           "output_dir": str(directory), "formats": record["formats"], "_on_progress": on_progress}
                result = await run_worker(payload)
                filename = result.get("filename")
                if not isinstance(filename, str) or filename not in {f"video.{selected['ext']}"}:
                    raise PublicError("The finished file could not be verified. Please retry the download.")
                path = directory / filename
                resolved_root = DATA_ROOT.resolve()
                resolved_path = path.resolve(strict=True)
                if path.is_symlink() or not path.is_file() or resolved_path.parent != directory.resolve(strict=True) or resolved_path.parent.parent != resolved_root:
                    raise PublicError("The finished file could not be verified. Please retry the download.")
                stat = path.stat()
                if not 0 < stat.st_size <= MAX_BYTES:
                    raise PublicError("The finished file must be between 1 byte and 500 MiB. Choose a lower quality.")
                job["filename"] = f"youtube-video.{selected['ext']}"
                job["path"] = path
                job["progress"] = 100
                job["status"] = "ready"
                job["message"] = "Your file is ready."
        except asyncio.CancelledError:
            cleanup_output(directory)
            raise
        except PublicError as exc:
            job = jobs.get(job_id)
            if job:
                job["status"] = "failed"
                job["message"] = str(exc)[:240]
            cleanup_output(directory)
        except Exception:
            job = jobs.get(job_id)
            if job:
                job["status"] = "failed"
                job["message"] = "The download failed. Check that FFmpeg is installed and try again."
            cleanup_output(directory)

    task = asyncio.create_task(execute_job())
    worker_tasks.add(task)
    task.add_done_callback(worker_tasks.discard)
    prune_memory()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    if not ID_RE.fullmatch(job_id):
        raise safe_error("That download task was not found. Analyze the video again.", 404)
    prune_memory()
    job = jobs.get(job_id)
    if not job:
        raise safe_error("That download task was not found or has expired. Analyze the video again.", 404)
    result = {"status": job["status"], "progress": job["progress"], "message": job["message"]}
    if job["status"] == "ready":
        result.update(filename=job["filename"], download_url=f"/api/files/{job_id}")
    return result


@app.get("/api/files/{job_id}")
async def get_file(job_id: str):
    if not ID_RE.fullmatch(job_id):
        raise safe_error("That download file was not found. Analyze the video again.", 404)
    job = jobs.get(job_id)
    if not job or job["status"] != "ready":
        raise safe_error("The file is not ready or has expired. Check the task status and try again.", 404)
    if time.monotonic() - job["created"] > JOB_TTL:
        raise safe_error("This file expired. Analyze the video again.", 410)
    path = job.get("path")
    directory = job.get("directory")
    try:
        if not isinstance(path, Path) or not isinstance(directory, Path):
            raise OSError
        expected_dir = (DATA_ROOT / job_id).resolve(strict=True)
        resolved = path.resolve(strict=True)
        if (not isinstance(path, Path) or path.is_symlink() or not path.is_file() or
                not isinstance(directory, Path) or directory.is_symlink() or directory.resolve(strict=True) != expected_dir or
                resolved.parent != expected_dir or expected_dir.parent != DATA_ROOT.resolve(strict=True) or
                path.name not in {"video.mp4", "video.webm"} or not 0 < path.stat().st_size <= MAX_BYTES):
            raise OSError
    except (OSError, RuntimeError, TypeError):
        raise safe_error("The file could not be verified. Analyze the video and download it again.", 404) from None
    return FileResponse(path, filename=job["filename"], media_type="video/mp4" if path.suffix == ".mp4" else "video/webm")
