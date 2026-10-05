# -DistPath builds somewhere other than .\dist, for when the app in .\dist is running.
param([string]$DistPath = "dist")

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtual environment not found. Create .venv and install requirements first."
}

Push-Location $projectRoot
try {
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --onedir `
        --name "Vellus" `
        --distpath $DistPath `
        --collect-all pyicloud `
        --collect-data fido2 `
        --exclude-module pyicloud.cli `
        --collect-submodules keyring.backends `
        --hidden-import winfspy `
        --hidden-import winfspy.plumbing._bindings `
        --add-binary ".venv\Lib\site-packages\wx\WebView2Loader.dll;wx" `
        app.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }
    Write-Host ""
    Write-Host "Built: $(Join-Path (Resolve-Path $DistPath) 'Vellus\Vellus.exe')"
} finally {
    Pop-Location
}
