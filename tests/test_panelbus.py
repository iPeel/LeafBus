"""PanelBus transport against a scripted UART: hot-plug replies and the
keep-alive that keeps the panels from dropping off a quiet bus."""
import time

import pytest

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


# --- a silent bus ----------------------------------------------------------

class GarblingUART(ScriptedUART):
    """A DATA line whose rising edges are too slow (long or screened cable):
    every lone 1 bit is lost, so the echo comes back as 00 and nothing else."""

    def write(self, b):
        b = bytes(b)
        self.sent.append(b)
        self.rx += bytes(len(b))
        if b[0] == panelbus.LAYOUT_DETECT:
            self.rx += bytes.fromhex('ffc0f8c0c0f0')   # as measured: noise
        return len(b)


class MuteUART(ScriptedUART):
    """TX never reaches RX: not even our own echo."""

    def write(self, b):
        self.sent.append(bytes(b))
        return len(b)


def silent_bus(uart):
    bus = make_bus(npanels=25)
    bus.uart = uart
    calls = []

    def layout_detect(wait_ms=None):
        calls.append(wait_ms)
        return bus._xact(bytes([panelbus.LAYOUT_DETECT]), expect=-1, wait_ms=5)
    bus.layout_detect = layout_detect
    return bus, calls


@pytest.mark.parametrize('uart, hint', [
    (ScriptedUART(), 'echo fine, no panel answers'),
    (GarblingUART(), 'own echo garbled, sent 80 read 00: DATA edges too slow'),
    (MuteUART(), 'no echo of our own frame'),
])
def test_silent_bus_gives_up_after_one_try_and_says_why(uart, hint):
    """Six tries of a 25-panel layout detect blocked the main loop for ~16 s,
    long enough for every web request to time out."""
    bus, calls = silent_bus(uart)
    with pytest.raises(panelbus.BusError) as e:
        bus.enumerate()
    assert len(calls) == 1
    assert 'bus silent' in str(e.value) and hint in str(e.value)


def test_a_corrupted_reply_is_not_silence():
    """Bytes without a terminator (a corrupted head, §9.2) mean the panels
    are there: keep trying."""
    bus = make_bus(npanels=1)
    good = bytes([0xB1, 0x04, 0x40])
    replies = [bytes([0xB1, 0x93]), good, good]
    bus.layout_detect = lambda wait_ms=None: replies.pop(0)
    bus.last_echo = bytes([panelbus.LAYOUT_DETECT])   # the line itself is fine
    assert bus.enumerate() == good
    assert replies == [] and bus.npanels == 1


def test_a_garbled_line_costs_one_short_window():
    """On 1 m of screened cable the board read back 00 for its 80 and then
    noise: no long retry window, no further tries."""
    bus = make_bus(npanels=1)
    bus.uart = GarblingUART()
    t0 = time.ticks_ms()
    with pytest.raises(panelbus.BusError) as e:
        bus.enumerate()
    assert time.ticks_diff(time.ticks_ms(), t0) < 600
    assert bus.uart.sent.count(bytes([panelbus.LAYOUT_DETECT])) == 1
    assert 'DATA edges too slow' in str(e.value)
