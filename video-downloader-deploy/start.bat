@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto :setup_error
)
.venv\Scripts\python.exe -c "import fastapi, uvicorn, yt_dlp, yt_dlp_ejs" >nul 2>&1
if errorlevel 1 (
  .venv\Scripts\python.exe -m pip install -r requirements.txt
  if errorlevel 1 goto :setup_error
)
where node >nul 2>&1
if errorlevel 1 (
  echo Node.js is required. Install Node.js 22 or newer, reopen this window, and try again.
  pause
  exit /b 1
)
where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo FFmpeg is required. Install FFmpeg and add its bin folder to PATH, then try again.
  pause
  exit /b 1
)
where ffprobe >nul 2>&1
if errorlevel 1 (
  echo ffprobe is required. Add the full FFmpeg bin folder to PATH, then try again.
  pause
  exit /b 1
)
echo.
echo Video downloader: http://127.0.0.1:8000
echo Open that address in your browser. Keep this window open while using the app.
echo Press Ctrl+C to stop.
echo.
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 (
  echo.
  echo The server could not start. If port 8000 is in use, see README.md for another port.
  pause
)
exit /b
:setup_error
echo Setup failed. Check your Python installation and internet connection. See README.md.
pause
exit /b 1
