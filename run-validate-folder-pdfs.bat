@echo off
REM Validate PDFs from your Desktop\PDFs folder (edit PDFDIR if your path differs).
REM Prerequisite: API running (run-api.bat). Uses first 3 PDFs alphabetically unless you pass extra args.
cd /d "%~dp0"

set "PDFDIR=%USERPROFILE%\OneDrive - Zinnia\Desktop\PDFs"
if not exist "%PDFDIR%" (
  echo Folder not found: %PDFDIR%
  echo Edit PDFDIR in this .bat or set SMART_FIND_PDF_FOLDER
  pause
  exit /b 1
)

REM Examples:
REM   run-validate-folder-pdfs.bat
REM   run-validate-folder-pdfs.bat --interactive
REM   run-validate-folder-pdfs.bat --pick 2,5,9 --checks-file "C:\path\checks.txt"

call "%~dp0py-or-python.bat" scripts\validate_folder_pdfs.py --dir "%PDFDIR%" --count 3 %*
if errorlevel 1 pause
