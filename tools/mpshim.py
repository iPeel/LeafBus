"""Run the MicroPython firmware under CPython: tests and tools/simulator.py.

install() adds what CPython lacks (time.ticks_*, select.poll on Windows,
the machine / network / ntptime modules) and FakeBus stands in for the
panel bus, built from a real layout string.
"""
import os
import select
import socket
import sys
import time
import types

_T0 = time.monotonic()


def install():
    if getattr(time, '_leafbus_shim', False):
        return
    time._leafbus_shim = True
    period = 1 << 30
    time.ticks_ms = lambda: int((time.monotonic() - _T0) * 1000) % period
    time.ticks_us = lambda: int((time.monotonic() - _T0) * 1e6) % period
    time.ticks_add = lambda t, d: (t + d) % period

    def ticks_diff(a, b):
        d = (a - b) % period
        return d - period if d >= period // 2 else d
    time.ticks_diff = ticks_diff
    time.sleep_ms = lambda ms: time.sleep(ms / 1000)
    time.sleep_us = lambda us: time.sleep(us / 1e6)

    if not hasattr(select, 'poll'):  # Windows
        select.POLLIN = 1

        class Poll:
            def __init__(self):
                self.socks = []

            def register(self, s, mask=1):
                self.socks.append(s)

            def poll(self, timeout_ms=-1):
                t = None if timeout_ms < 0 else timeout_ms / 1000
                r, _, _ = select.select(self.socks, [], [], t)
                return [(s, 1) for s in r]
        select.poll = Poll

    machine = types.ModuleType('machine')

    class UART:
        def __init__(self, *a, **k):
            pass
    machine.UART = UART
    machine.mem32 = {}
    machine.reset = lambda: print('[sim] machine.reset() requested')
    sys.modules.setdefault('machine', machine)

    network = types.ModuleType('network')
    network.STA_IF, network.AP_IF = 0, 1
    network.AUTH_OPEN, network.AUTH_WPA2_PSK = 0, 3
    network.hostname = lambda *a: 'leafbus'

    class WLAN:
        def __init__(self, i):
            self.i = i
            self._active = False

        def active(self, v=None):
            if v is not None:
                self._active = v
            return self._active

        def isconnected(self):
            return self.i == 0

        def connect(self, *a):
            pass

        def disconnect(self):
            pass

        def ifconfig(self):
            return ('127.0.0.1', '255.0.0.0', '127.0.0.1', '127.0.0.1')

        def config(self, *a, **k):
            if a == ('mac',):
                return b'\x24\x6f\x28\x00\xbe\xef'

        def status(self, *a):
            return -52 if a == ('rssi',) else []

        def scan(self):
            return [(b'HomeNet', b'', 6, -48, 3, False),
                    (b'Garden', b'', 11, -71, 3, False),
                    (b'Cafe guest', b'', 1, -80, 0, False)]
    network.WLAN = WLAN
    sys.modules.setdefault('network', network)

    if not hasattr(sys, 'print_exception'):
        import traceback
        sys.print_exception = lambda e: traceback.print_exception(e)


FOURTEEN = ('b1 b0 04 b2 b2 b0 b1 b1 04 e3 00 02 00 00 04 04 04 b2 04 04 04 '
            'b1 b1 b2 04 b1 b1 04 04 04 95 04 40')


class FakeBus:
    """PanelBus look-alike: answers enumeration with a fixed layout string,
    remembers pushed colors, reports touches from self.touching.
    self.reads: layout strings the next enumerations return (noise)."""

    def __init__(self, layout=FOURTEEN):
        self.raw = bytes.fromhex(layout)
        self.npanels = 1
        self.nodes = []
        self.layout_unstable = False
        self.colors = []
        self.pushes = 0
        self.frames = []
        self.level = 255
        self.touching = set()
        self.silent = False

    def enumerate(self):
        import panelbus
        if self.silent:
            raise panelbus.BusError('no layout string: bus silent (simulated)')
        raw = self.reads.pop(0) if getattr(self, 'reads', None) else self.raw
        return self.adopt(raw)

    def adopt(self, raw):
        body = raw[:raw.index(0x40)]
        self.nodes = [b for b in body if b & 0x80 and (b >> 4) & 7 != 1]
        self.npanels = len(self.nodes)
        self.adopted = raw
        return raw

    def brightness(self, v):
        self.level = v

    def bulk_pull(self, wait_ms=None):
        n = self.npanels
        out = bytearray(2 * n)
        for chunk in self.touching:
            if 0 <= chunk < n:
                out[2 * (n - 1 - chunk)] = 0x11
        return bytes(out)

    def push_colors(self, colors):
        if len(self.colors) != self.npanels:
            self.colors = [(0, 0, 0, 0)] * self.npanels
        for i, c in enumerate(colors):
            if c is not None and i < self.npanels:
                self.colors[i] = (c[0], c[1], c[2], c[3] if len(c) > 3 else 0)
        self.pushes += 1
        self.frames.append(list(colors))
        if len(self.frames) > 50:
            self.frames.pop(0)

    def fill(self, r, g, b, w=0, transition=1):
        self.push_colors([(r, g, b, w, transition)] * self.npanels)


def firmware_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'firmware')
