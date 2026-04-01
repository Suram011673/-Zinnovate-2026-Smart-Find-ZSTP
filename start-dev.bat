@echo off
REM API in a new window, then Vite here (fixes ECONNREFUSED if you only ran npm run dev).
start "Smart Find API (port 8000)" cmd /k "%~dp0backend\run-api.bat"
echo Waiting for API to start...
timeout /t 3 /nobreak >nul
cd /d "%~dp0frontend"
npm run dev
