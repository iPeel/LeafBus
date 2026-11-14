#!/usr/bin/env python3
"""Run the board firmware on this computer, with simulated panels.

    python tools/simulator.py [--port 8080] [--layout "b1 b0 04 ... 40"]

Opens the real web UI at http://localhost:8080 against the real firmware
code (app, effects, web, geometry); only the panel bus and Wi-Fi are
simulated. DDP and E1.31 senders can target 127.0.0.1 as well. Settings,
presets and state are kept in .sim/ next to this repository's tools/.
Use it to work on the UI or effects without flashing a board.
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mpshim  # noqa: E402

mpshim.install()
sys.path.insert(0, mpshim.firmware_path())

import app  # noqa: E402
import config  # noqa: E402
import web  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=8080, help='HTTP port')
    ap.add_argument('--layout', default=mpshim.FOURTEEN, help='layout string, hex')
    ap.add_argument('--data', default=os.path.join(HERE, '..', '.sim'),
                    help='directory for config.json, presets.json, state.json')
    a = ap.parse_args()

    web.WWW = os.path.join(os.path.abspath(mpshim.firmware_path()), 'www')
    os.makedirs(a.data, exist_ok=True)
    os.chdir(a.data)
    cfg = config.load()
    cfg['http_port'] = a.port
    cfg['wifi']['ssid'] = cfg['wifi']['ssid'] or 'HomeNet'
    print('LeafBus simulator: http://localhost:%d  (data in %s)' % (a.port, os.getcwd()))
    try:
        app.App(cfg, bus=mpshim.FakeBus(a.layout)).run()
    except KeyboardInterrupt:
        print('stopped')


if __name__ == '__main__':
    main()
