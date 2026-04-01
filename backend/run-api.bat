@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
    echo Create the venv first: py -3 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    echo ^(or: python -m venv .venv if py is not installed^)
    pause
    exit /b 1
)

REM Default OCR tuning for scanned / handwritten PDFs (override in Environment Variables if needed)
REM 200–240 is usually enough for Tesseract; higher DPI is much slower on large scans
if not defined SMART_FIND_OCR_DPI set SMART_FIND_OCR_DPI=220
REM Tesseract layout: PSM 6 = block of text (notes/slides). Try PSM 11 for sparse text: --oem 3 --psm 11
if not defined SMART_FIND_TESSERACT_CONFIG set "SMART_FIND_TESSERACT_CONFIG=--oem 3 --psm 6"
REM Optional: PIL preprocessing before OCR (prescriptions / noisy scans): 1=light contrast+sharpen, 2=strong
REM set SMART_FIND_OCR_IMAGE_ENHANCE=1
REM Optional: extra cursive handling on weak pages (slower)
REM set SMART_FIND_HANDWRITING_MODE=1
if exist "C:\Program Files\Tesseract-OCR\tesseract.exe" set "TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe"
if not defined TESSERACT_CMD if exist "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe" set "TESSERACT_CMD=C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"
if not defined TESSERACT_CMD if exist "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe" set "TESSERACT_CMD=%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"

if not defined SMART_FIND_API_PORT set SMART_FIND_API_PORT=8000
REM Bind API on all interfaces for direct access from other machines (optional; with npm run dev + Vite proxy, 127.0.0.1 is enough)
if not defined SMART_FIND_API_HOST set SMART_FIND_API_HOST=127.0.0.1

REM Local dev: no real mail server — "Send PDFs by email" is simulated (API log only). Replace with real SMTP_HOST for production.
if not defined SMTP_HOST set SMTP_HOST=dummy

REM Pre-ops: require notification email before Next field / Find (set one or both)
REM set SMART_FIND_REQUIRE_EMAIL_BEFORE_OPS=1
REM When SMTP is configured, require email before ops (no need for line above)
REM set SMART_FIND_REQUIRE_EMAIL_IF_SMTP=1
REM Public API URL for email preview links, e.g. https://api.yourcompany.com
REM set SMART_FIND_PUBLIC_API_URL=https://127.0.0.1:8000
REM Preview link TTL in hours (default 168)
REM set SMART_FIND_SHARE_TTL_HOURS=168

set "CHECK_PORT=%SMART_FIND_API_PORT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "& { $p = [int]$env:CHECK_PORT; if ($p -lt 1) { $p = 8000 }; $ln = @(Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue); if ($ln.Count -gt 0) { $pids = ($ln | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '; Write-Host ''; Write-Host '[run-api] Port' $p 'is already in use (PID:' $pids ').' -ForegroundColor Yellow; Write-Host '  Close that uvicorn window, or Task Manager - End task on that PID, or use a free port:' -ForegroundColor Yellow; Write-Host '  set SMART_FIND_API_PORT=8001' -ForegroundColor Yellow; Write-Host '  frontend/.env.local: VITE_BACKEND_PORT=8001' -ForegroundColor Yellow; Write-Host ''; exit 2 } }"
if errorlevel 2 (
  set "CHECK_PORT="
  pause
  exit /b 2
)
set "CHECK_PORT="

call .venv\Scripts\activate.bat
echo Starting uvicorn on http://%SMART_FIND_API_HOST%:%SMART_FIND_API_PORT%  (OCR: /health/ocr )
uvicorn main:app --reload --host %SMART_FIND_API_HOST% --port %SMART_FIND_API_PORT%
