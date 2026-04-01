# Download Tesseract Windows installer (UB Mannheim) when winget mirror returns 403.
# Run: powershell -ExecutionPolicy Bypass -File install-tesseract-windows.ps1

$ErrorActionPreference = "Stop"
$url = "https://digi.bib.uni-mannheim.de/tesseract/tesseract-ocr-w64-setup-5.4.0.20240606.exe"
$out = Join-Path $env:TEMP "tesseract-ocr-w64-setup.exe"

Write-Host "Downloading Tesseract (large file)..."
try {
    Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing
} catch {
    Write-Host "Direct download failed. Install manually from: https://github.com/UB-Mannheim/tesseract/wiki"
    exit 1
}
Write-Host "Saved: $out"
Write-Host "Starting installer (add to PATH when prompted)..."
Start-Process -FilePath $out -Wait
Write-Host "Done. Restart terminals and run: http://127.0.0.1:8000/health/ocr"
