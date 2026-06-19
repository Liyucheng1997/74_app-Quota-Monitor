$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "Ensuring PyInstaller is available..."
python -m pip install --disable-pip-version-check pyinstaller
if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed" }

python -m PyInstaller --noconfirm --clean --onefile --windowed --name "AI-Quota-Monitor" app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
Write-Host "Built: $PSScriptRoot\dist\AI-Quota-Monitor.exe"
