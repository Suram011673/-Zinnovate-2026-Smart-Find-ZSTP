# Install Tesseract OCR on Windows.
# 1) Prefers winget (GitHub-hosted installer; avoids Mannheim 403 in some networks).
# 2) Falls back to direct UB Mannheim download + GUI installer.
# Run: powershell -ExecutionPolicy Bypass -File install-tesseract-windows.ps1

$ErrorActionPreference = "Stop"

function Add-TesseractUserPath {
    $add = Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR"
    if (-not (Test-Path (Join-Path $add "tesseract.exe"))) { return }
    $cur = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($cur -like "*Tesseract-OCR*") { return }
    $new = if ($cur) { "$cur;$add" } else { $add }
    [Environment]::SetEnvironmentVariable("Path", $new, "User")
    Write-Host "Added to user PATH: $add"
}

Write-Host "Installing Tesseract OCR..."
$wingetOk = $false
try {
    $p = Get-Command winget -ErrorAction Stop
    & winget install --id tesseract-ocr.tesseract -e --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -eq 0) { $wingetOk = $true }
} catch {
    Write-Host "winget not available or install failed, trying direct download..."
}

if (-not $wingetOk) {
    $url = "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.4.0.20240606.exe"
    $out = Join-Path $env:TEMP "tesseract-ocr-w64-setup.exe"
    Write-Host "Downloading Tesseract (large file)..."
    try {
        Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
    } catch {
        Write-Host "Direct download failed. Install manually: https://github.com/UB-Mannheim/tesseract/wiki"
        exit 1
    }
    Write-Host "Saved: $out"
    Write-Host "Starting installer (enable 'Add to PATH' if offered)..."
    Start-Process -FilePath $out -Wait
}

Add-TesseractUserPath

$exe = Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR\tesseract.exe"
if (-not (Test-Path $exe)) {
    $exe = "C:\Program Files\Tesseract-OCR\tesseract.exe"
}
if (Test-Path $exe) {
    & $exe --version
    Write-Host ""
    Write-Host "Done. Restart terminals, then start the API and open: http://127.0.0.1:8000/health/ocr"
    Write-Host "Or set TESSERACT_CMD=$exe if the backend still cannot find tesseract."
} else {
    Write-Host "Install finished but tesseract.exe not found in default locations. Check Start Menu or PATH."
}
