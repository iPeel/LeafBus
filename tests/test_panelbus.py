"""PanelBus transport against a scripted UART: hot-plug replies and the
keep-alive that keeps the panels from dropping off a quiet bus."""
import time

import panelbus


class ScriptedUART:
    """Single-wire UART: every write is echoed, then the reply queued for
    that frame (by its first byte) follows."""

    def __init__(self, replies=None):
        self.replies = dict(replies or {})
        self.rx = bytearray()
        self.sent = []

    def write(self, b):
        b = bytes(b)
        self.sent.append(b)
        self.rx += b + self.replies.get(b[0], b'')
        return len(b)

    def flush(self):
        pass

    def any(self):
        return len(self.rx)

    def read(self, n=None):
        n = len(self.rx) if n is None else n
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out or None


def make_bus(npanels=20, pull_reply=None):
    bus = panelbus.PanelBus(open_drain=False)
    pairs = bytes(2 * npanels) if pull_reply is None else pull_reply
    bus.uart = ScriptedUART({panelbus.BULK_PULL: pairs})
    bus.npanels = npanels
    bus.nodes = [{'code': 3, 'root_edge': 0}] * npanels
    bus.last_ka = time.ticks_add(time.ticks_ms(), -1000)   # no settle wait
    return bus


def test_full_pull_reply():
    bus = make_bus()
    assert bus.bulk_pull() == bytes(40)
    assert bus.hotplug_replies == 0


def test_lone_hotplug_byte_ends_the_read_at_once():
    """A dropped-out assembly answers every poll with CC alone; waiting out
    the 440 ms window on each one stalled the main loop to 2 passes/s."""
    bus = make_bus(pull_reply=bytes([panelbus.HOTSWAP]))
    t0 = time.ticks_ms()
    r = bus.bulk_pull()
    assert time.ticks_diff(time.ticks_ms(), t0) < 100
    assert r == bytes([panelbus.HOTSWAP])
    assert bus.hotplug_replies == 1
    assert bus.short_replies == 0


def test_partial_pairs_then_hotplug_byte_end_the_read():
    bus = make_bus(pull_reply=bytes(6) + bytes([panelbus.HOTSWAP]))
    t0 = time.ticks_ms()
    r = bus.bulk_pull()
    assert time.ticks_diff(time.ticks_ms(), t0) < 100
    assert r[-1] == panelbus.HOTSWAP and len(r) == 7


def test_keepalive_polls_only_a_quiet_bus():
    bus = make_bus()
    bus.last_tx = time.ticks_ms()
    bus._keepalive(None)
    assert bus.uart.sent == []              # traffic just now
    bus.last_tx = time.ticks_add(time.ticks_ms(), -panelbus.KEEPALIVE_MS - 1)
    bus._keepalive(None)
    assert bus.uart.sent == [bytes([panelbus.BULK_PULL])]
    assert bus.keepalives == 1
    bus._keepalive(None)
    assert bus.keepalives == 1              # it counts as traffic itself


def test_keepalive_keeps_off_during_an_exchange_and_before_enumeration():
    bus = make_bus()
    bus.last_tx = time.ticks_add(time.ticks_ms(), -1000)
    bus.busy = True
    bus._keepalive(None)
    bus.busy = False
    bus.nodes = []
    bus._keepalive(None)
    assert bus.uart.sent == []


def test_exchange_counts_as_traffic_and_releases_the_bus():
    bus = make_bus()
    bus.last_tx = time.ticks_add(time.ticks_ms(), -1000)
    bus.push_colors([(1, 2, 3)] * bus.npanels)
    assert not bus.busy
    assert time.ticks_diff(time.ticks_ms(), bus.last_tx) < 50


def test_exchange_waits_for_a_keepalive_reply_to_finish(monkeypatch):
    slept = []
    monkeypatch.setattr(time, 'sleep_ms', slept.append)
    bus = make_bus()
    bus.last_ka = time.ticks_ms()
    bus.bulk_pull()
    assert slept and slept[0] >= 5 + bus.npanels // 4 - 1
    slept.clear()
    bus.last_ka = time.ticks_add(time.ticks_ms(), -1000)
    bus.bulk_pull()
    assert slept == []
