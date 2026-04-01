@echo off
REM Python OCR libraries (EasyOCR, pytesseract). Tesseract itself: see README or run install-tesseract-windows.ps1
cd /d "%~dp0backend"
if not exist ".venv\Scripts\pip.exe" (
  echo Run: py -3 -m venv .venv   ^(or: python -m venv .venv^)
  pause
  exit /b 1
)
echo Installing backend requirements (includes EasyOCR, may take a few minutes)...
.venv\Scripts\pip.exe install -r requirements.txt
echo.
echo Next: install Tesseract OCR engine if not already installed.
echo Open: https://github.com/UB-Mannheim/tesseract/wiki
pause
