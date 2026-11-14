"""The application against a simulated bus: startup, touch, presets,
settings, schedule and the HTTP layer. No network, no hardware."""
import json
import time

import pytest

import app as appmod
import config
import mpshim
import web


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def make(cfg=None, state=None, presets=None):
        if state is not None:
            config.save_json('state.json', state)
        if presets is not None:
            config.save_presets(presets)
        c = config.load()
        for k, v in (cfg or {}).items():
            c[k] = v if not isinstance(v, dict) else dict(c[k], **v)
        return appmod.App(c, bus=mpshim.FakeBus(), verbose=False)
    return make


def http(a, method, path, body=None):
    raw = json.dumps(body).encode() if body is not None else b''
    req = web.Request(method, path, {}, {}, raw)
    try:
        return web.make_handler(a)(req)
    except ValueError as e:      # what WebServer._respond turns into a 400
        return 400, {'error': str(e)}


# --- startup -----------------------------------------------------------------

def test_first_boot_plays_first_preset(board):
    a = board()
    assert a.bus.npanels == 14
    assert a.engine.name == config.DEFAULT_PRESETS[0]['effect']
    assert a.preset == 'Rainbow' and a.power


def test_startup_last_restores_everything(board):
    a = board(state={'power': True, 'brightness': 40, 'effect': 'fire',
                     'params': {'intensity': 90}, 'preset': None})
    assert a.engine.name == 'fire' and a.engine.params['intensity'] == 90
    assert a.brightness == 40 and a.bus.level == 40


def test_startup_last_stays_off(board):
    a = board(state={'power': False, 'brightness': 255, 'effect': 'solid', 'params': {}})
    assert not a.power
    assert a.bus.colors[0] == (0, 0, 0, 0)


def test_startup_preset_wins_over_saved_state(board):
    a = board(cfg={'startup': {'mode': 'preset', 'preset': 'Fireplace', 'brightness': 100}},
              state={'power': False, 'brightness': 10, 'effect': 'solid', 'params': {}})
    assert a.engine.name == 'fire' and a.preset == 'Fireplace'
    assert a.power and a.brightness == 100


def test_startup_missing_preset_falls_back(board):
    a = board(cfg={'startup': {'mode': 'preset', 'preset': 'gone'}},
              state={'power': True, 'brightness': 200, 'effect': 'plasma', 'params': {}})
    assert a.engine.name == 'plasma'


def test_startup_off(board):
    a = board(cfg={'startup': {'mode': 'off'}})
    assert not a.power and a.engine.name  # an effect is ready for power-on


def test_bus_silent_still_starts(board, monkeypatch):
    real = mpshim.FakeBus

    class Silent(real):
        def __init__(self, *a):
            super().__init__(*a)
            self.silent = True
    monkeypatch.setattr(mpshim, 'FakeBus', Silent)
    a = board()
    assert a.bus_error and 'silent' in a.bus_error
    assert http(a, 'GET', '/api/layout')[1]['error']


# --- state and presets ---------------------------------------------------------

def test_state_changes_and_persistence(board):
    a = board()
    st = a.update_state({'effect': 'flow', 'params': {'speed': 70}, 'brightness': 128})
    assert st['effect'] == 'flow' and st['params']['speed'] == 70 and st['brightness'] == 128
    assert st['preset'] is None
    a.update_state({'params': {'reverse': True}})
    assert a.engine.params['speed'] == 70 and a.engine.params['reverse'] is True
    a.save_state()
    saved = config.load_json('state.json')
    assert saved['effect'] == 'flow' and saved['params']['reverse'] is True


def test_power_toggle_and_timer(board):
    a = board()
    a.update_state({'power': 'toggle'})
    assert not a.power
    st = a.update_state({'power': True, 'timer': 30})
    assert a.power and 29 <= st['timer'] <= 30 and st['timer_set'] == 30
    st = a.update_state({'timer': 0})
    assert st['timer'] is None and st['timer_set'] is None


def test_presets_save_apply_delete(board):
    a = board()
    a.update_state({'effect': 'breathe', 'params': {'period': 9}})
    a.save_preset('Calm')
    assert a.preset == 'Calm'
    a.update_state({'preset': 'Rainbow'})
    assert a.engine.name == 'rainbow'
    a.update_state({'preset': 'Calm'})
    assert a.engine.name == 'breathe' and a.engine.params['period'] == 9
    assert config.load_presets()[-1]['name'] == 'Calm'
    a.delete_preset('Calm')
    assert a.preset is None and all(p['name'] != 'Calm' for p in config.load_presets())
    with pytest.raises(ValueError):
        a.update_state({'preset': 'Calm'})


def test_preset_order(board):
    a = board()
    names = [p['name'] for p in a.presets]
    a.order_presets([names[2], names[0]])
    assert [p['name'] for p in a.presets][:3] == [names[2], names[0], names[1]]


# --- touch -----------------------------------------------------------------------

def press(a, chunk, ms):
    """Hold a panel for ms milliseconds through real bus polls."""
    a.bus.touching = {chunk}
    t0 = time.ticks_ms()
    a._touch(_status(a), t0)
    a._touch(_status(a), time.ticks_add(t0, ms))
    a.bus.touching = set()
    a._touch(_status(a), time.ticks_add(t0, ms + 10))


def _status(a):
    import panelbus
    return panelbus.touch_status(a.bus.bulk_pull(), a.bus.npanels)


def test_tap_runs_effect_touch(board):
    a = board()
    a.update_state({'effect': 'ripple', 'params': {'auto': 0}})
    press(a, 4, 50)
    assert len(a.engine.eff.waves) == 1


def test_hold_toggles_power(board):
    a = board()
    press(a, 4, appmod.HOLD_MS + 50)
    assert not a.power
    press(a, 4, 50)                 # any touch wakes it up
    assert a.power


def test_per_panel_tap_action(board):
    a = board()
    a.update_settings({'touch': {'panels': {'0': {'tap': 'next'}}}})
    first = a.preset
    press(a, 0, 50)
    assert a.preset != first
    press(a, 5, 50)                 # other panels keep the default
    assert a.power


def test_touch_from_web(board):
    a = board()
    a.update_state({'effect': 'ripple', 'params': {'auto': 0}})
    code, st = http(a, 'POST', '/api/touch', {'chunk': 7})
    assert code == 200 and len(a.engine.eff.waves) == 1
    code, _ = http(a, 'POST', '/api/touch', {'chunk': 99})
    assert code == 400


def test_bright_action_steps_down_and_wraps(board):
    a = board()
    a.cfg['touch']['tap'] = 'bright'
    a.cfg['touch']['hold'] = 'none'
    seen = []
    for _ in range(4):
        press(a, 1, 50)
        seen.append(a.brightness)
    assert seen == [128, 48, 255, 128]


# --- settings -----------------------------------------------------------------------

def test_settings_mask_and_keep_wifi_key(board):
    a = board(cfg={'wifi': {'ssid': 'Home', 'key': 'secret'}})
    s = a.settings_json()
    assert s['wifi']['key'] == appmod.MASK
    a.update_settings({'wifi': {'ssid': 'Home', 'key': appmod.MASK}})
    assert a.cfg['wifi']['key'] == 'secret'
    assert config.load()['wifi']['key'] == 'secret'


@pytest.mark.parametrize('body', [
    {'startup': {'mode': 'sometimes'}},
    {'touch': {'tap': 'explode'}},
    {'schedule': [{'time': '25:00'}]},
    {'hostname': 'my board!'},
    {'ap': {'key': 'short'}},
    {'http_port': 0},
])
def test_settings_validation(board, body):
    a = board()
    code, out = http(a, 'POST', '/api/settings', body)
    assert code == 400 and out['error']


def test_restart_flag(board):
    a = board()
    assert a.update_settings({'http_port': 8081})['restart']
    assert not a.update_settings({'timezone': 60})['restart']


def test_streams_disable(board):
    a = board()
    a.start_network()
    a.update_settings({'streams': {'enabled': False}})
    assert not a.ddp.enabled and not a.e131.enabled
    for s in (a.ddp.sock, a.e131.sock, a.web.sock):
        s.close()


# --- layout ---------------------------------------------------------------------------

def test_layout_config_rebuilds_effects(board):
    a = board()
    L = http(a, 'POST', '/api/layout', {'rotation': 390, 'mirror': True,
                                        'names': {'0': 'left', '3': ''},
                                        'disabled': [1, 99]})[1]
    assert L['config'] == {'rotation': 30, 'mirror': True, 'names': {'0': 'left'},
                           'disabled': [1]}
    assert not a.engine.ctx.on[1]
    assert not L['stale']
    a.raw = bytes.fromhex('b1 04 95 40')
    assert a.layout_json()['stale']


def test_layout_json_is_complete(board):
    L = http(board(), 'GET', '/api/layout')[1]
    assert L['panels'] == 14 and len(L['positions']) == 14
    assert L['psu']['chunk'] == 3 and len(L['neighbours']) == 14
    assert L['tree'][0]['chunk'] == 13 and L['tree'][0]['parent'] == -1
    assert len([n for n in L['tree'] if not n.get('psu')]) == 14


# --- schedule ---------------------------------------------------------------------------

def test_schedule_runs_entries(board):
    a = board()
    entries = appmod.clean_schedule([
        {'time': '7:5', 'days': [], 'action': 'preset', 'preset': 'Fireplace', 'brightness': 90}])
    assert entries[0]['time'] == '07:05'
    a.update_state({'power': False})
    a.run_entry(entries[0], time.ticks_ms())
    assert a.power and a.preset == 'Fireplace' and a.brightness == 90
    a.run_entry({'action': 'off'}, time.ticks_ms())
    assert not a.power


def test_schedule_fires_on_minute_change(board, monkeypatch):
    a = board(cfg={'schedule': [{'enabled': True, 'time': '07:00', 'days': [],
                                 'action': 'off', 'preset': '', 'brightness': -1}],
                   'timezone': 60})
    start = 1767247140          # 2026-01-01 05:59 UTC = 06:59 at UTC+1
    t = [start]
    monkeypatch.setattr(time, 'time', lambda: t[0])
    a._schedule(0)
    assert a.power
    t[0] = start + 60
    a._schedule(0)
    assert not a.power


# --- HTTP layer -------------------------------------------------------------------------

def test_parse_request_waits_for_body():
    head = b'POST /api/state?x=a%20b HTTP/1.1\r\nContent-Length: 11\r\n\r\n'
    assert web.parse_request(head + b'{"a":') is None
    r = web.parse_request(head + b'{"a": true}')
    assert r.method == 'POST' and r.path == '/api/state'
    assert r.query == {'x': 'a b'} and r.json() == {'a': True}


def test_parse_request_rejects_garbage():
    with pytest.raises(ValueError):
        web.parse_request(b'x' * (web.MAX_HEAD + 1))
    with pytest.raises(ValueError):
        web.parse_request(b'POST / HTTP/1.1\r\nContent-Length: 999999\r\n\r\n')


def test_legacy_endpoints(board):
    a = board()
    assert http(a, 'GET', '/layout')[1]['panels'] == 14
    assert http(a, 'GET', '/state')[1]['panels'] == 14
    assert http(a, 'GET', '/identify/3')[1] == {'identify': 3}
    assert a.source() == 'identify'
    assert http(a, 'GET', '/brightness/30')[1]['brightness'] == 30
    assert http(a, 'GET', '/off')[1]['power'] is False


def test_unknown_api_is_404(board):
    assert http(board(), 'GET', '/api/nope')[0] == 404


def test_static_ui_is_served(board, monkeypatch):
    monkeypatch.setattr(web, 'WWW', mpshim.firmware_path() + '/www')
    code, f = http(board(), 'GET', '/')
    assert code == 200 and isinstance(f, web.StaticFile)
    assert web.static('/../config.json') is None


def test_server_round_trip(board):
    """A real socket: request in, response streamed out in pieces."""
    import socket
    a = board()
    srv = web.WebServer(web.make_handler(a), port=0)
    port = srv.sock.getsockname()[1]
    c = socket.create_connection(('127.0.0.1', port))
    c.sendall(b'GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n')
    c.settimeout(0.05)
    data = b''
    deadline = time.time() + 3
    while time.time() < deadline:
        srv.tick()
        try:
            chunk = c.recv(4096)
            if not chunk:
                break
            data += chunk
        except OSError:
            pass
    c.close()
    srv.sock.close()
    head, body = data.split(b'\r\n\r\n', 1)
    assert head.startswith(b'HTTP/1.1 200')
    assert json.loads(body)['panels'] == 14


def test_parallel_connections_are_answered_one_at_a_time(board):
    """A browser opens several connections at once; every one gets its
    answer, but only one sends at any moment (ESP32 TCP buffers)."""
    import socket
    a = board()
    srv = web.WebServer(web.make_handler(a), port=0)
    port = srv.sock.getsockname()[1]
    clients = []
    for p in ('/api/state', '/api/layout', '/api/effects', '/api/presets'):
        srv.tick()   # Windows refuses on a full backlog where lwIP lets the client retry
        c = socket.create_connection(('127.0.0.1', port))
        c.sendall(b'GET ' + p.encode() + b' HTTP/1.1\r\n\r\n')
        c.settimeout(0.01)
        clients.append([c, b''])
    deadline = time.time() + 5
    sending = []
    real_send = srv._send

    def spy(c):
        sending.append(sum(1 for x in srv.conns if x.out))
        real_send(c)
    srv._send = spy
    while time.time() < deadline and any(c[0] for c in clients):
        srv.tick()
        for c in clients:
            if not c[0]:
                continue
            try:
                d = c[0].recv(65536)
                if not d:
                    c[0].close()
                    c[0] = None
                c[1] += d
            except OSError:
                pass
    srv.sock.close()
    assert sending and max(sending) == 1
    for _, data in clients:
        head, body = data.split(b'\r\n\r\n', 1)
        assert head.startswith(b'HTTP/1.1 200')
        json.loads(body)


def test_json_is_built_in_small_parts(board):
    """Large replies must never need one big allocation (ESP32 heap)."""
    body = board().layout_json()
    parts, size = web.json_parts(body)
    assert len(parts) > 1 and max(len(p) for p in parts) < 2 * web._Chunks.PART
    assert size == sum(len(p) for p in parts)
    assert json.loads(b''.join(parts)) == json.loads(json.dumps(body))


def test_rescan_frees_the_old_geometry_first(board):
    """The old layout and effect geometry are released before the new ones
    are computed (on the board old + new did not fit with 26 panels)."""
    a = board()
    seen = []
    real = appmod.geometry.layout

    def layout(raw):
        seen.append((a.layout, a.engine.eff, a.engine.ctx.n))
        return real(raw)
    appmod.geometry.layout = layout
    try:
        assert a.rescan()
    finally:
        appmod.geometry.layout = real
    assert seen == [(None, None, a.bus.npanels)]
    assert a.layout and a.engine.eff is not None and a.engine.ctx.n == a.bus.npanels


def test_rescan_out_of_memory_keeps_the_lights_running(board, monkeypatch):
    a = board()
    name = a.engine.name
    real = appmod.effects.Ctx

    def ctx(n, positions=None, *rest, **kw):
        if positions:
            raise MemoryError('memory allocation failed')
        return real(n, positions, *rest, **kw)
    monkeypatch.setattr(appmod.effects, 'Ctx', ctx)
    assert a.rescan()
    assert a.engine.name == name and a.engine.eff is not None
    assert a.engine.ctx.n == a.bus.npanels
    assert 'out of memory' in a.layout_error
    assert a.engine.tick(time.ticks_add(time.ticks_ms(), 10)) is not None


def test_network_out_of_memory_does_not_restart_the_board(board, monkeypatch):
    """poll() raises EIO when ESP-IDF's select() cannot allocate. That used to
    count as an error, and 20 in 10 s made main.py restart the board."""
    a = board()

    class Sock:
        pass

    class Net:
        def __init__(self):
            self.sock = Sock()
            self.conns = []
            self.frames = 0
            self.requests = 0
            self.stats = {}
            self.group = None

        def tick(self):
            pass

        accept = tick

    class Poll:
        calls = 0

        def register(self, *a):
            pass

        def poll(self, ms):
            Poll.calls += 1
            if Poll.calls > 30:
                raise KeyboardInterrupt
            raise OSError(appmod.EIO)

    a.ddp, a.e131, a.web = Net(), Net(), Net()
    monkeypatch.setattr(appmod.select, 'poll', Poll)
    monkeypatch.setattr(appmod.time, 'sleep_ms', lambda ms: None)
    pushes = a.bus.pushes
    with pytest.raises(KeyboardInterrupt):
        a.run()
    assert a.net_starved == 30 and a.errors == 0
    assert a.bus.pushes > pushes       # the lights kept going
