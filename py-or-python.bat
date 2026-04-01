@echo off
REM Run Python via Windows launcher (py -3) when available, else "python" on PATH.
where py >nul 2>&1
if not errorlevel 1 (
  py -3 %*
  exit /b %errorlevel%
)
where python >nul 2>&1
if not errorlevel 1 (
  python %*
  exit /b %errorlevel%
)
echo [ERROR] Neither "py" nor "python" found on PATH.
echo Install Python from https://www.python.org/downloads/ ^(check "Add python.exe to PATH"^), or enable the py launcher.
exit /b 9009
