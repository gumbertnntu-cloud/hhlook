$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Entry = Join-Path $ScriptDir "windows_entry.py"

if (!(Test-Path $Entry)) {
  throw "Entry file not found: $Entry"
}

Write-Host "[build] Python version"
python --version

Write-Host "[build] Upgrade pip"
python -m pip install --upgrade pip

Write-Host "[build] Install dependencies"
pip install -r requirements.txt
pip install pyinstaller

Write-Host "[build] Install Playwright Chromium into package-local path"
$env:PLAYWRIGHT_BROWSERS_PATH = '0'
python -m playwright install chromium

Write-Host "[build] Build one-file exe"
pyinstaller `
  --noconfirm `
  --clean `
  --onefile `
  --windowed `
  --name HHLook `
  --paths src `
  --collect-all playwright `
  --collect-all pydantic `
  --collect-all pydantic_core `
  --collect-all bs4 `
  --collect-all lxml `
  "$Entry"

if (!(Test-Path "dist/HHLook.exe")) {
  throw "dist/HHLook.exe not found"
}

Write-Host "[build] Done: dist/HHLook.exe"
