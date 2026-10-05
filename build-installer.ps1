# Builds installer\Output\Vellus-Setup-<version>.exe from an existing build of the app.
# Run build.ps1 first. -DistPath matches build.ps1's parameter of the same name.
param([string]$DistPath = "dist")

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$appFolder = Join-Path (Resolve-Path (Join-Path $projectRoot $DistPath)) "Vellus"
$compiler = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $compiler) {
    throw "Inno Setup 6 was not found. Install it from https://jrsoftware.org/isinfo.php."
}
if (-not (Test-Path -LiteralPath (Join-Path $appFolder "Vellus.exe"))) {
    throw "No build found in $appFolder. Run build.ps1 first."
}

Push-Location $projectRoot
try {
    $version = & $python -c "import config; print(config.VERSION)"
    & $compiler "/DAppVersion=$version" "/DDistDir=$appFolder" "installer\Vellus.iss"
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE."
    }

    # Browsers and mail systems often block a bare .exe, so the download is a
    # zip holding the installer, the licences and the installer's checksum.
    $output = Join-Path $projectRoot "installer\Output"
    $setup = Join-Path $output "Vellus-Setup-$version.exe"
    $checksum = Join-Path $output "SHA256.txt"
    $zip = Join-Path $output "Vellus-$version.zip"
    "$((Get-FileHash $setup -Algorithm SHA256).Hash.ToLower())  Vellus-Setup-$version.exe" |
        Set-Content -LiteralPath $checksum -Encoding ascii
    Remove-Item -LiteralPath $zip -ErrorAction SilentlyContinue
    Compress-Archive -LiteralPath $setup, $checksum, (Join-Path $projectRoot "LICENSE"),
        (Join-Path $projectRoot "THIRD-PARTY-NOTICES.md") -DestinationPath $zip
    Write-Host ""
    Write-Host "Built: $setup"
    Write-Host "Built: $zip"
    Write-Host "SHA256 of the zip: $((Get-FileHash $zip -Algorithm SHA256).Hash.ToLower())"
} finally {
    Pop-Location
}
