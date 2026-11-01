# Build a portable, one-folder Windows app at dist\FM1FirmwareTool.
# Creates an isolated .venv and installs only the pinned build/runtime deps.
[CmdletBinding()]
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Here

$Python = Join-Path $Here ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    & py -3.14 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv. Install Python 3.14 and the py launcher." }
}

& $Python -m pip install --disable-pip-version-check -r motor\requirements-lock.txt -r requirements-build-lock.txt
if ($LASTEXITCODE -ne 0) { throw "Could not install the pinned build dependencies." }

# Verify the vendored recovery engine before packaging it.
& $Python -c "import sys; sys.path.insert(0, 'motor'); import fm1_unbrick as f; p, bad = f.verify_pins(f.PINS_FILE); sys.exit('changed: ' + ', '.join(bad) if bad else 0)"
if ($LASTEXITCODE -ne 0) { throw "motor\ does not match motor\pins.json; refusing to build." }

if (-not $SkipTests) {
    & $Python -m unittest discover -s tests
    if ($LASTEXITCODE -ne 0) { throw "Tests failed; refusing to build. Use -SkipTests only for a deliberate local build." }
}

$hidden = @("argparse", "logging", "urllib.request", "zipfile", "select", "ctypes.wintypes",
            "yaml", "crcmod", "rtmidi")
$hiddenArgs = $hidden | ForEach-Object { "--hidden-import", $_ }
& $Python -m PyInstaller --noconfirm --clean --windowed --name FM1FirmwareTool `
    --icon "app\assets\fm1_installer.ico" `
    --add-data "app\resources;app\resources" `
    --add-data "motor;motor" `
    @hiddenArgs `
    --distpath dist --workpath build `
    fm1_installer.pyw
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

$exe = "dist\FM1FirmwareTool\FM1FirmwareTool.exe"
if (-not (Test-Path $exe)) { throw "PyInstaller reported success but $exe is missing." }
Compress-Archive -Path "dist\FM1FirmwareTool" -DestinationPath "dist\FM1FirmwareTool-Windows-x64.zip" -Force
Get-FileHash $exe -Algorithm SHA256 | Format-List Path, Hash
Write-Output "Built $exe and dist\FM1FirmwareTool-Windows-x64.zip (one-folder distribution)."
