#!/usr/bin/env python3
"""Put the firmware on a board over USB.

    python tools/deploy.py --flash [--port COM5]     LeafBus MicroPython image + files
    python tools/deploy.py --frozen [--port COM5]    files only, board already flashed
    python tools/deploy.py [--port COM5] [--source]  stock MicroPython, modules as files

Recommended on a plain ESP32 (no PSRAM): --flash. It writes
dist/leafbus-esp32.bin (tools/build_firmware.sh), MicroPython with the
LeafBus modules frozen in: they run from flash instead of taking ~70 kB of
RAM, which the Wi-Fi and TCP stack need for the web interface to stay
reachable. Only the firmware area is written; the filesystem with
config.json, presets and state is kept.

Without --flash/--frozen the modules are copied as files, precompiled to
.mpy (or .py with --source) for stock MicroPython. That runs the lights
fine, but on a board without PSRAM the web server starves for memory.

Always: the web UI goes up gzipped (64 kB -> 20 kB); config.json,
presets.json and state.json on the board are left alone (--config uploads a
config file explicitly); the board is reset at the end.

Needs mpremote, plus esptool for --flash and mpy-cross (matching the
board's MicroPython) for the default mode: pip install -r requirements-dev.txt
"""
import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
FW = os.path.join(ROOT, 'firmware')
IMAGE = os.path.join(ROOT, 'dist', 'leafbus-esp32.bin')
MODULES = ['panelbus', 'geometry', 'effects', 'config', 'wifi', 'web', 'ddp',
           'e131', 'anim', 'app']
FLASH_OFFSET = '0x1000'      # original ESP32: bootloader, partitions, app


def tool(name):
    exe = shutil.which(name)
    if not exe:
        here = os.path.dirname(sys.executable)
        for cand in (os.path.join(here, name), os.path.join(here, name + '.exe')):
            if os.path.exists(cand):
                return cand
        sys.exit('deploy: %s not found; pip install %s' % (name, name))
    return exe


def flash(port, image):
    if not os.path.exists(image):
        sys.exit('deploy: %s missing; build it with tools/build_firmware.sh' % image)
    print('flashing %s (%d kB) at %s; the filesystem is not touched'
          % (os.path.basename(image), os.path.getsize(image) // 1024, FLASH_OFFSET))
    cmd = [sys.executable, '-m', 'esptool', '--chip', 'esp32']
    if port:
        cmd += ['--port', port]
    subprocess.run(cmd + ['--baud', '460800', 'write_flash', '-z', FLASH_OFFSET, image],
                   check=True)
    time.sleep(3)     # first boot after flashing


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', help='serial port (default: auto-detect)')
    ap.add_argument('--flash', nargs='?', const=IMAGE, metavar='IMAGE',
                    help='write the LeafBus MicroPython image first (default %(const)s)')
    ap.add_argument('--frozen', action='store_true',
                    help='board runs the LeafBus image: upload main.py and the UI only')
    ap.add_argument('--source', action='store_true', help='copy .py instead of .mpy')
    ap.add_argument('--config', help='also upload this file as config.json')
    a = ap.parse_args()
    frozen = a.frozen or bool(a.flash)

    if a.flash:
        flash(a.port, a.flash)

    mpremote = tool('mpremote')
    conn = ['connect', a.port] if a.port else []
    tmp = tempfile.mkdtemp(prefix='leafbus-')
    # one mpremote session, '+'-chained. Opening the port resets many ESP32
    # boards (auto-reset wiring on DTR/RTS); an interrupt sent at once is lost
    # in the ROM bootloader. After 1.5 s it lands in main.py's start-up pause.
    cmds = ['sleep', '1.5', '+']
    try:
        if frozen:
            remove = [m + e for m in MODULES for e in ('.py', '.mpy')]
            what = 'main.py and the web UI (modules are in the firmware)'
        else:
            ext, other = ('.py', '.mpy') if a.source else ('.mpy', '.py')
            if not a.source:
                mpy = tool('mpy-cross')
                for m in MODULES:
                    subprocess.run([mpy, '-march=xtensawin', '-o', os.path.join(tmp, m + '.mpy'),
                                    os.path.join(FW, m + '.py')], check=True)
            for m in MODULES:
                src = os.path.join(FW, m + '.py') if a.source else os.path.join(tmp, m + '.mpy')
                cmds += ['cp', src, ':' + m + ext, '+']
            remove = [m + other for m in MODULES]
            what = '%d modules (%s), main.py and the web UI' % (len(MODULES), ext)
        cmds += ['cp', os.path.join(FW, 'main.py'), ':main.py', '+']
        gz = os.path.join(tmp, 'index.html.gz')
        with open(os.path.join(FW, 'www', 'index.html'), 'rb') as f, open(gz, 'wb') as g:
            g.write(gzip.compress(f.read(), 9))
        cmds += ['exec', "import os\ntry: os.mkdir('www')\nexcept OSError: pass", '+']
        cmds += ['cp', gz, ':www/index.html.gz', '+']
        # files on the filesystem win over frozen modules and .py over .mpy:
        # leftovers of the other kind must go
        remove.append('www/index.html')
        cmds += ['exec', "import os\nfor f in %r:\n  try: os.remove(f)\n  except OSError: pass"
                 % (tuple(remove),), '+']
        if a.config:
            cmds += ['cp', a.config, ':config.json', '+']
        cmds += ['reset']
        print('uploading %s%s' % (what, ' and config.json' if a.config else ''))
        for attempt in (1, 2):
            if subprocess.run([mpremote] + conn + cmds).returncode == 0:
                break
            if attempt == 2:
                sys.exit('deploy: mpremote failed twice; press the board\'s reset button and retry')
            print('retrying once')
            time.sleep(2)
        print('done: the board restarts; open http://leafbus.local or watch the console with')
        print('      mpremote%s repl' % (' connect ' + a.port if a.port else ''))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()
