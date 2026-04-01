@echo off
REM 1) Production frontend build   2) Validate first N PDFs via API (same as run-validate-folder-pdfs defaults).
REM API must be listening on SMART_FIND_API or http://127.0.0.1:8000 (start backend\run-api.bat first).
cd /d "%~dp0"

cd frontend
call npm run build
if errorlevel 1 (
  cd ..
  pause
  exit /b 1
)
cd ..

set "VPY=%~dp0backend\.venv\Scripts\python.exe"
if exist "%VPY%" (
  call "%VPY%" "%~dp0scripts\validate_folder_pdfs.py" --count 3 %*
) else (
  call "%~dp0py-or-python.bat" "%~dp0scripts\validate_folder_pdfs.py" --count 3 %*
)
set "VPY="
if errorlevel 1 pause
