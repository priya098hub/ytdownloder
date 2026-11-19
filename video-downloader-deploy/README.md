# YouTube Video Downloader (local)

A one-screen web app that runs on your own computer:

**Paste a YouTube link → Analyze → pick a quality → Download.**

It shows the real video title, preview, duration, and only the qualities YouTube actually offers. Files are prepared by the local server, then handed to your browser, which saves them to your normal downloads folder.

## Requirements

- Windows with Python 3.11 or newer
- [Node.js 22 or newer](https://nodejs.org/) — required by current YouTube extraction
- [FFmpeg](https://www.gyan.dev/ffmpeg/builds/) (`ffmpeg` and `ffprobe` on PATH) — installs that add it to PATH automatically are the easiest
- Internet connection

## Run it

Double-click `start.bat`, or from a terminal in this folder:

```bat
start.bat
```

The script creates a private `.venv`, installs dependencies on first run, checks the tools above, and starts the server. Then open:

**http://127.0.0.1:8000**

Keep the black server window open while you use the app. Press `Ctrl+C` (or close the window) to stop.

### If port 8000 is already taken

Edit `start.bat` and choose another port, for example `--port 8010`, then open `http://127.0.0.1:8010`.

## How to use it

1. Copy a video link from YouTube (watch page, Shorts, or youtu.be).
2. Paste it and press **Analyze link**.
3. Pick a quality from the list. Sizes marked `~` are estimates from YouTube’s reported bitrate.
4. Tick the permission box and press **Download video**.
5. The progress bar shows the download; when it reaches “ready”, your browser saves the file. If the browser blocks the automatic start, use the **Save video** link.

## What it refuses, on purpose

- Private, unlisted, age-restricted, paid, and members-only videos
- Live and upcoming streams, playlists, channels
- DRM-protected content
- Anything requiring sign-in, cookies, or bypassing access controls
- Videos over 2 hours, or files over 500 MiB

Only download videos you own or have permission to save, in ways YouTube permits. You are responsible for complying with YouTube’s Terms of Service and copyright law in your country.

## Design and security notes

- The server binds to `127.0.0.1` only and rejects requests whose `Host`/`Origin` don’t match the page itself — no remote control, no CSRF from other sites.
- yt-dlp runs in a separate, isolated worker process (`python -I`) that talks to the server through one JSON line on stdin and bounded JSON lines on stdout. Progress is streamed live; on timeout or cancel the whole process tree is killed (`taskkill /T /F` on Windows).
- Format selections are server-issued random IDs mapped to validated stream IDs; the browser never sends a format expression, and the worker re-checks every stream against the analyzed video.
- Finished files live in a per-job temp directory under `%TEMP%/local-youtube-downloader`, are verified (regular file, size, no symlinks, inside its own job directory) before serving, and expire after 1 hour (analyses after 30 minutes). Stale directories are cleaned periodically; unfinished jobs are removed on graceful shutdown. Ready files remain temporarily available until expiry.
- Responses carry `X-Content-Type-Options`, `Referrer-Policy`, `Cache-Control: no-store`, `X-Frame-Options: DENY`, and a restrictive CSP. No secrets, cookies, or third-party APIs are involved.
- One analysis and one download at a time; small bounded in-memory registries; request bodies capped at 8 KiB.

## Project layout

```
app/main.py      FastAPI server, security middleware, job/analysis registry, file serving
app/media.py     URL normalization, video eligibility rules, format selection
app/worker.py    isolated yt-dlp subprocess (analyze/download), JSON-line protocol, progress hooks
static/          the one-screen UI (index.html, styles.css, app.js)
tests/           26 automated tests: validation, security, job lifecycle, file containment
start.bat        Windows launcher
requirements.txt runtime dependencies (fastapi, uvicorn, yt-dlp[default])
```

## Development

```bat
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
```

Tests mock the yt-dlp worker; they never touch YouTube or write real media.

## Troubleshooting

- **“Node.js is required” / “FFmpeg is required”** — install them, then reopen the terminal so PATH updates take effect.
- **“Cannot reach the local app”** — the server window was closed or crashed; run `start.bat` again.
- **“YouTube could not be reached…”** — network issue, or YouTube changed something. Update yt-dlp: `.venv\Scripts\python.exe -m pip install -U "yt-dlp[default]"`.
- **Download stuck at “processing”** — very large files can take a while; the hard limit is 40 minutes. After that the job fails with a message and cleans up.

This is a personal-use tool, not a hosted service. It has no accounts, no tracking, and no ads.
