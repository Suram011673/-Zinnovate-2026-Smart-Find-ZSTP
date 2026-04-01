# Starts uvicorn in a new PowerShell window, then Vite here.
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Backend = Join-Path $Root "backend"
$VenvPy = Join-Path $Backend ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    Write-Error "Missing $VenvPy — create venv in backend and pip install -r requirements.txt"
    exit 1
}

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-Command",
    "Set-Location '$Backend'; & '$VenvPy' -m uvicorn main:app --reload --host 127.0.0.1 --port 8000"
) -WindowStyle Normal

Start-Sleep -Seconds 3
Set-Location (Join-Path $Root "frontend")
npm run dev
