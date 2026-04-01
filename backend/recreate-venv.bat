@echo off
cd /d "%~dp0"
echo.
echo This deletes backend\.venv and creates a new one for THIS PC.
echo (Fixes "uvicorn not recognized" when the venv was copied from another machine.)
echo Press Ctrl+C to cancel, or
pause
if exist ".venv" rmdir /s /q ".venv"
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Could not create venv. Install Python 3.10+ and ensure py or python is on PATH.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Done. Start the API: run-api.bat
echo Or: python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
pause
