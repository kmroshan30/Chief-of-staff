# activate.ps1 — run this once if .venv isn't auto-activated
# Usage: . .\activate.ps1   (note the leading dot — runs in current shell)

$venvPath = Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1"

if (Test-Path $venvPath) {
    & $venvPath
    Write-Host "✅ .venv activated — $(python --version)" -ForegroundColor Green
} else {
    Write-Host "❌ .venv not found at $venvPath" -ForegroundColor Red
    Write-Host "   Run: python -m venv .venv  then  pip install -r requirements.txt" -ForegroundColor Yellow
}
