#!/usr/bin/env bash
# Build MicroPython for the ESP32 with the LeafBus modules frozen in.
#
#     tools/build_firmware.sh [build-dir]        (Linux or WSL)
#
# Output: dist/leafbus-esp32.bin, flashed by tools/deploy.py --flash.
#
# Needs git, python3 and ~3 GB of disk; no root. ESP-IDF, its toolchain,
# cmake and ninja are fetched into the build directory (default
# ~/leafbus-build) and ~/.espressif. make and a host compiler are not
# needed: idf.py drives the build and the pip mpy-cross compiles the frozen
# modules.
set -euo pipefail

MICROPYTHON=v1.29.0
IDF=v5.5.2
BOARD=ESP32_GENERIC

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="${1:-$HOME/leafbus-build}"
mkdir -p "$WORK"
cd "$WORK"

# Python environments without ensurepip (Ubuntu without python3-venv)
venv() {
    [ -x "$1/bin/pip" ] && return
    python3 -m venv --without-pip "$1"
    [ -f "$WORK/get-pip.py" ] || wget -q -O "$WORK/get-pip.py" https://bootstrap.pypa.io/get-pip.py
    "$1/bin/python" "$WORK/get-pip.py" -q
}

[ -d esp-idf ] || git clone -q -b "$IDF" --depth 1 --recursive --shallow-submodules \
    https://github.com/espressif/esp-idf.git
[ -d micropython ] || {
    git clone -q -b "$MICROPYTHON" --depth 1 https://github.com/micropython/micropython.git
    git -C micropython submodule update --init --depth 1 lib/berkeley-db-1.xx lib/micropython-lib
}
python3 "$REPO/tools/patch_micropython.py" micropython   # heap patches, see there

venv tools-venv
tools-venv/bin/pip -q install "mpy-cross==${MICROPYTHON#v}.post2"
export MICROPY_MPYCROSS="$WORK/tools-venv/bin/mpy-cross"   # read from the environment by mkrules.cmake

export IDF_TOOLS_PATH="${IDF_TOOLS_PATH:-$HOME/.espressif}"
PYVER="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
venv "$IDF_TOOLS_PATH/python_env/idf${IDF:1:3}_py${PYVER}_env"
python3 esp-idf/tools/idf_tools.py install --targets esp32 required cmake ninja
python3 esp-idf/tools/idf_tools.py install-python-env
# shellcheck disable=SC1091
. esp-idf/export.sh > /dev/null

cd micropython/ports/esp32
# firmware/board/LEAFBUS: ESP32_GENERIC plus LeafBus memory settings
idf.py -B "build-$BOARD-leafbus" \
    -D MICROPY_BOARD="$BOARD" \
    -D MICROPY_BOARD_DIR="$REPO/firmware/board/LEAFBUS" \
    -D MICROPY_FROZEN_MANIFEST="$REPO/firmware/manifest.py" \
    build

B="build-$BOARD-leafbus"
mkdir -p "$REPO/dist"
if [ -f "$B/firmware.bin" ]; then
    cp "$B/firmware.bin" "$REPO/dist/leafbus-esp32.bin"
else
    python3 makeimg.py "$B/sdkconfig" "$B/bootloader/bootloader.bin" \
        "$B/partition_table/partition-table.bin" "$B/micropython.bin" \
        "$REPO/dist/leafbus-esp32.bin" "$REPO/dist/leafbus-esp32.uf2" > /dev/null
fi
ls -l "$REPO/dist/leafbus-esp32.bin"
