# Build MicroPython for the ESP32 with the LeafBus modules frozen in - on
# Windows, natively (no WSL). The same image as tools/build_firmware.sh.
#
#     powershell -ExecutionPolicy Bypass -File tools\build_firmware.ps1 [-Work <build dir>]
#
# Output: dist\leafbus-esp32.bin, flashed by: python tools\deploy.py --flash
#
# Needs git and Python 3.9+ on PATH and ~4 GB of disk; no admin rights.
# ESP-IDF is cloned into the build directory (default ~\leafbus-build), its
# toolchain goes to ~\.espressif. MicroPython does not support Windows as a
# build host; tools\patch_micropython.py replaces the few shell commands of
# its build rules (see there). Run it from PowerShell or cmd, not Git Bash:
# ESP-IDF refuses MSYS shells.
param([string]$Work = "$HOME\leafbus-build")

$ErrorActionPreference = "Continue"   # native tools write progress to stderr
$MicroPython = "v1.29.0"
$Idf = "v5.5.2"
$Board = "ESP32_GENERIC"
$Repo = (Resolve-Path "$PSScriptRoot\..").Path -replace '\\', '/'

function Step($what) { Write-Host "== $what" }
function Check($what) { if ($LASTEXITCODE -ne 0) { Write-Host "build_firmware: $what failed ($LASTEXITCODE)"; exit 1 } }
function Here($dir) { Set-Location $dir; [Environment]::CurrentDirectory = $dir }

# ESP-IDF's tools refuse to run when they see an MSYS environment
foreach ($v in "MSYSTEM", "MSYSTEM_PREFIX", "MSYSTEM_CHOST", "MSYSTEM_CARCH", "MINGW_PREFIX") {
    Remove-Item "Env:$v" -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Force $Work | Out-Null
Here $Work

if (-not (Test-Path esp-idf)) {
    Step "ESP-IDF $Idf"
    git -c core.longpaths=true clone -q -b $Idf --depth 1 --recursive --shallow-submodules https://github.com/espressif/esp-idf.git
    Check "git clone esp-idf"
}
if (-not (Test-Path micropython)) {
    Step "MicroPython $MicroPython"
    git -c core.longpaths=true clone -q -b $MicroPython --depth 1 https://github.com/micropython/micropython.git
    Check "git clone micropython"
    git -C micropython -c core.longpaths=true submodule update --init --depth 1 lib/berkeley-db-1.xx lib/micropython-lib
    Check "git submodule update"
}
Step "patches"
python "$Repo/tools/patch_micropython.py" micropython
Check "patch_micropython"

if (-not (Test-Path tools-venv\Scripts\mpy-cross.exe)) {
    Step "mpy-cross"
    python -m venv tools-venv
    Check "python -m venv"
    tools-venv\Scripts\python -m pip -q install "mpy-cross==$($MicroPython.TrimStart('v')).post2"
    Check "pip install mpy-cross"
}
$env:MICROPY_MPYCROSS = "$Work\tools-venv\Scripts\mpy-cross.exe"   # read by mkrules.cmake

if (-not $env:IDF_TOOLS_PATH) { $env:IDF_TOOLS_PATH = "$HOME\.espressif" }
Step "ESP-IDF tools in $env:IDF_TOOLS_PATH"
python esp-idf\tools\idf_tools.py install --targets esp32 required cmake ninja
Check "idf_tools install"
python esp-idf\tools\idf_tools.py install-python-env
Check "idf_tools install-python-env"
. "$Work\esp-idf\export.ps1" | Out-Null

Step "build"
Here "$Work\micropython\ports\esp32"
$B = "build-$Board-leafbus"
idf.py -B $B -D MICROPY_BOARD=$Board -D "MICROPY_BOARD_DIR=$Repo/firmware/board/LEAFBUS" `
    -D "MICROPY_FROZEN_MANIFEST=$Repo/firmware/manifest.py" build 2>&1 | Out-File -Encoding utf8 "$Work\build.log"
if ($LASTEXITCODE -ne 0) { Get-Content "$Work\build.log" -Tail 40; Write-Host "build_firmware: idf.py build failed, log: $Work\build.log"; exit 1 }

New-Item -ItemType Directory -Force "$Repo/dist" | Out-Null
if (Test-Path "$B\firmware.bin") {
    Copy-Item "$B\firmware.bin" "$Repo/dist/leafbus-esp32.bin" -Force
} else {
    python makeimg.py "$B\sdkconfig" "$B\bootloader\bootloader.bin" "$B\partition_table\partition-table.bin" `
        "$B\micropython.bin" "$Repo/dist/leafbus-esp32.bin" "$Repo/dist/leafbus-esp32.uf2" | Out-Null
    Check "makeimg"
}
Get-Item "$Repo/dist/leafbus-esp32.bin" | Select-Object FullName, Length
