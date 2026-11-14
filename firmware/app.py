"""Application: bus, effects, touch, schedule, Wi-Fi and the web UI in one loop.

The board is autonomous: after power-up it enumerates the panels and starts
the configured startup effect at once, before and independently of Wi-Fi.
Network streams (DDP, E1.31) take over while frames arrive and hand the
panels back to the effect when they stop.

Details that matter for throughput on MicroPython:

* Sockets are waited on with select.poll() rather than drained until
  OSError. Raising an exception on an empty socket is expensive here and it
  happens hundreds of times a second otherwise (+50 % frame rate).
* The bus is polled at least once a second even while frames stream in,
  and every 200 ms when idle, every TOUCH_POLL_MS while touch actions are
  configured. Without polling the panels stop applying frames after a few
  seconds, and after ~370 ms with no traffic at all they drop off the bus
  until re-enumerated (PROTOCOL.md §4.2); panelbus' keep-alive timer covers
  the stretches where this loop is busy elsewhere.
* Housekeeping (Wi-Fi, clock, schedule, saving) runs four times a second,
  not on every pass of the loop.
"""
import gc
import json
import sys
import time
import select

import config
import effects
import geometry
import panelbus
import ddp
import e131
import web
import wifi

VERSION = '2.0.2'
EIO = 5                # errno: select() failed (ESP32: no memory for it)

IDLE_MS = 300          # silence that counts as idle
KEEP_MS = 200          # poll interval when idle; the panels drop out after ~370 ms of silence
MAX_NOPOLL_MS = 1000   # poll interval while frames stream in
RESCAN_MS = 5000       # minimum gap between re-enumerations
TOUCH_POLL_MS = 120    # poll interval while touch actions are configured
HOLD_MS = 800          # touch longer than this is a hold
BUS_RETRY_MS = 10000   # retry a silent bus this often
SAVE_DELAY_MS = 4000   # state.json is written this long after the last change
HOUSEKEEP_MS = 250
GC_MS = 2000           # explicit garbage collection interval
NTP_RETRY_MS = 3600000
NTP_REFRESH_MS = 12 * 3600000
BRIGHT_STEPS = (255, 128, 48)
MASK = '********'
ACTIONS = ('none', 'effect', 'next', 'prev', 'power', 'bright')
SCHEDULE_ACTIONS = ('on', 'off', 'preset', 'brightness')
STARTUP_MODES = ('last', 'preset', 'off')


def idf_heap():
    """ESP-IDF heap (Wi-Fi, lwIP): (free, largest block, lowest free ever) in
    bytes, or None off the ESP32. MicroPython's own heap is gc.mem_free()."""
    try:
        import esp32
        info = esp32.idf_heap_info(esp32.HEAP_DATA)
    except (ImportError, AttributeError):
        return None
    return (sum(h[1] for h in info), max(h[2] for h in info), sum(h[3] for h in info))


def _since(now, t):
    return time.ticks_diff(now, t)


def _int(v, lo, hi, name):
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError('%s must be a number' % name)
    if not lo <= v <= hi:
        raise ValueError('%s must be in %d..%d' % (name, lo, hi))
    return v


def _action(a):
    a = str(a or 'none')
    if a in ACTIONS or (a.startswith('preset:') and len(a) > 7):
        return a
    raise ValueError('unknown touch action %r' % a)


def clean_schedule(entries):
    if not isinstance(entries, list) or len(entries) > 24:
        raise ValueError('schedule: a list of at most 24 entries')
    out = []
    for k, e in enumerate(entries):
        try:
            h, m = str(e.get('time', '')).split(':')
            h, m = int(h), int(m)
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError('schedule entry %d: time must be HH:MM' % (k + 1))
        a = e.get('action', 'preset')
        if a not in SCHEDULE_ACTIONS:
            raise ValueError('schedule entry %d: unknown action %r' % (k + 1, a))
        days = sorted(set(int(d) for d in e.get('days') or () if 0 <= int(d) <= 6))
        out.append({'enabled': e.get('enabled', True) is not False,
                    'time': '%02d:%02d' % (h, m), 'days': days, 'action': a,
                    'preset': str(e.get('preset') or '')[:32],
                    'brightness': _int(e.get('brightness', -1), -1, 255, 'brightness')})
    return out


class App:
    def __init__(self, cfg=None, bus=None, verbose=True):
        self.cfg = cfg or config.load()
        self.verbose = verbose
        self.presets = config.load_presets()
        now = time.ticks_ms()
        self.boot = now
        self.bus = bus or panelbus.PanelBus(tx=self.cfg['uart']['tx'],
                                            rx=self.cfg['uart']['rx'])
        self.raw = b''
        self.layout = None
        self.bus_error = None
        self.layout_error = None
        self.engine = effects.Engine(effects.Ctx(1))
        self.power = True
        self.brightness = self.cfg['brightness']
        self.preset = None
        self.streaming = None          # 'ddp' / 'e131' while a stream plays
        self.stream_until = now
        self.hold_until = None         # identify
        self.pressed = {}              # chunk -> press time
        self.held = set()              # presses already acted upon
        self.touches = 0
        self.off_at = None             # sleep timer
        self.timer_set = None          # minutes it was set to
        self.dirty_at = None
        self.reboot_at = None
        self.last_off = now
        self.last_pull = b''
        self.last_frame = self.last_keep = self.last_rescan = now
        self.last_house = self.last_gc = now
        self.fps = 0.0
        self._fps_t = now
        self._fps_n = 0
        self.loops = 0
        self.loop_rate = 0.0
        self.errors = 0
        self.last_error = None
        self.net_starved = 0           # main loop polls that found no network memory
        self._seen = self._seen_ddp = 0
        self.ntp_ok = False
        self.ntp_next = None
        self.minute = None
        self.wifi = None
        self.ddp = self.e131 = self.web = None
        self._catalogue = None
        self._catalogue_bytes = None
        self._touch_cfg()
        self.rescan()
        self._startup(now)

    def _log(self, *a):
        if self.verbose:
            print(*a)

    # --- bus and layout -------------------------------------------------------
    def rescan(self):
        """Enumerate, recompute geometry, give the effect the new layout."""
        now = time.ticks_ms()
        self.last_rescan = now
        try:
            raw = self.bus.enumerate()
            self.bus.brightness(self.brightness)
            self.bus_error = None
        except panelbus.BusError as e:
            self.bus_error = str(e)
            self.raw = b''
            self.layout = None
            self._rebuild(now)
            self._log('bus:', e)
            return False
        self.raw = raw
        # Free the old layout and effect geometry before computing the new
        # ones, and collect between the steps: with 26 panels the old data,
        # the new and the garbage of the computation did not fit at once.
        self.layout = None
        self.layout_error = None
        self.engine.release(self.bus.npanels)
        gc.collect()
        try:
            lay = geometry.layout(raw)
            gc.collect()
            lay['tree'] = geometry.tree_json(lay['tree'])
            self.layout = lay
        except Exception as e:      # unknown panel types, corrupted strings
            self.layout_error = str(e) or repr(e)
        gc.collect()
        try:
            self._rebuild(now)
        except MemoryError:
            # keep the lights running: the effect in chain order
            self.layout_error = 'out of memory computing the effect geometry'
            self._log('layout:', self.layout_error)
            gc.collect()
            self.engine.restart(now, fade=3, ctx=effects.Ctx(self.bus.npanels))
        return True

    def _rebuild(self, now):
        lc = self.cfg['layout']
        n = self.bus.npanels
        pos = self.layout['positions'] if self.layout else None
        links = self.layout['links'] if self.layout else ()
        if pos and len(pos) != n:
            pos = None
        dis = [d for d in lc.get('disabled') or () if isinstance(d, int)]
        ctx = effects.Ctx(n, pos, links, lc.get('rotation', 0),
                          lc.get('mirror', False), dis)
        self.engine.restart(now, fade=3, ctx=ctx)
        self.pressed = {}
        self.held = set()

    def _black(self, now, fade=6):
        self.bus.push_colors([(0, 0, 0, 0, fade)] * self.bus.npanels)
        self.last_off = now

    # --- what plays -------------------------------------------------------------
    def _startup(self, now):
        st = self.cfg['startup']
        mode = st.get('mode', 'last')
        saved = config.load_json('state.json')
        if not isinstance(saved, dict):
            saved = None
        ok = False
        if mode == 'preset' and st.get('preset'):
            ok = self.apply_preset(st['preset'], now, power=False)
        if not ok and saved:
            try:
                self.engine.set(saved['effect'], saved.get('params'), now, fade=8)
                self.preset = saved.get('preset')
                ok = True
            except (KeyError, ValueError, TypeError):
                pass
        if not ok:
            for p in self.presets:
                if self.apply_preset(p['name'], now, power=False):
                    ok = True
                    break
        if not ok:
            self.engine.set('rainbow', {}, now, fade=8)
        if mode == 'last' and saved:
            b = saved.get('brightness')
            if isinstance(b, int) and 0 <= b <= 255:
                self.brightness = b
            self.power = saved.get('power') is not False
        else:
            self.power = mode != 'off'
        b = st.get('brightness', -1)
        if isinstance(b, int) and 0 <= b <= 255:
            self.brightness = b
        self.bus.brightness(self.brightness)
        self.engine.fade = 8           # fade in from dark
        if not self.power:
            self._black(now)
        self.dirty_at = None

    def _dirty(self, now):
        self.dirty_at = now

    def save_state(self):
        config.save_json('state.json', {
            'power': self.power, 'brightness': self.brightness,
            'effect': self.engine.name, 'params': self.engine.params,
            'preset': self.preset})
        self.dirty_at = None

    def set_power(self, on, now=None):
        now = time.ticks_ms() if now is None else now
        if on == self.power:
            return
        self.power = on
        if on:
            self.bus.brightness(self.brightness)
            self.engine.restart(now, fade=6)
        else:
            self.off_at = None
            self._black(now)
        self._dirty(now)

    def set_brightness(self, v, now=None):
        self.brightness = max(0, min(255, int(v)))
        self.bus.brightness(self.brightness)
        self._dirty(time.ticks_ms() if now is None else now)

    def _find_preset(self, name):
        for p in self.presets:
            if p['name'] == name:
                return p
        return None

    def apply_preset(self, name, now=None, power=True):
        p = self._find_preset(name)
        if p is None:
            return False
        now = time.ticks_ms() if now is None else now
        try:
            self.engine.set(p['effect'], p.get('params'), now)
        except ValueError:
            return False
        self.preset = p['name']
        self.hold_until = None
        if power:
            self.set_power(True, now)
            self.engine.fade = max(self.engine.fade, 5)
        self._dirty(now)
        return True

    def step_preset(self, d, now=None):
        if not self.presets:
            return
        names = [p['name'] for p in self.presets]
        i = names.index(self.preset) + d if self.preset in names else 0
        self.apply_preset(names[i % len(names)], now)

    def update_state(self, body):
        now = time.ticks_ms()
        if 'brightness' in body:
            self.set_brightness(_int(body['brightness'], 0, 255, 'brightness'), now)
        if body.get('preset'):
            if not self.apply_preset(body['preset'], now, power='power' not in body):
                raise ValueError('no preset named %r' % body['preset'])
        elif 'effect' in body or 'params' in body:
            name = body.get('effect') or self.engine.name
            params = body.get('params')
            params = params if isinstance(params, dict) else {}
            same = name == self.engine.name
            merged = dict(self.engine.params) if same else {}
            merged.update(params)
            self.engine.set(name, merged, now, fade=2 if same else 5, keep_time=same)
            if not same or params:
                self.preset = None
            self.hold_until = None
            if 'power' not in body:
                self.set_power(True, now)
            self._dirty(now)
        if 'power' in body:
            v = body['power']
            self.set_power(not self.power if v == 'toggle' else bool(v), now)
        if 'timer' in body:
            m = _int(body['timer'] or 0, 0, 24 * 60, 'timer')
            self.off_at = time.ticks_add(now, m * 60000) if m else None
            self.timer_set = m or None
        return self.state_json()

    def source(self):
        if self.hold_until is not None:
            return 'identify'
        if self.streaming:
            return self.streaming
        return 'effect' if self.power else 'off'

    def state_json(self):
        now = time.ticks_ms()
        cs = self.bus.colors or [(0, 0, 0, 0)] * self.bus.npanels
        timer = None
        if self.off_at is not None:
            timer = max(0, round(_since(self.off_at, now) / 60000, 1))
        return {'power': self.power, 'brightness': self.brightness,
                'effect': self.engine.name, 'params': self.engine.params,
                'preset': self.preset, 'source': self.source(),
                'panels': self.bus.npanels, 'touch': sorted(self.pressed),
                'touches': self.touches, 'fps': round(self.fps, 1), 'timer': timer,
                'timer_set': self.timer_set if timer is not None else None,
                'time': self.clock_str(),
                'colors': ''.join('%02x%02x%02x%02x' % tuple(c[:4]) for c in cs)}

    def effects_json(self):
        if self._catalogue is None:
            self._catalogue = effects.catalogue()
        return self._catalogue

    def effects_bytes(self):
        """The catalogue as JSON, encoded once: it never changes."""
        if self._catalogue_bytes is None:
            self._catalogue_bytes = json.dumps(self.effects_json()).encode()
        return self._catalogue_bytes

    # --- presets ------------------------------------------------------------------
    def save_preset(self, name, effect=None, params=None):
        name = str(name or '').strip()[:32]
        if not name:
            raise ValueError('a preset needs a name')
        effect = effect or self.engine.name
        cls = effects.REGISTRY.get(effect)
        if cls is None:
            raise ValueError('unknown effect %r' % effect)
        if params is None:
            params = self.engine.params if effect == self.engine.name else {}
        params = effects.clean(cls, params, self.bus.npanels)
        entry = {'name': name, 'effect': effect, 'params': params}
        for i, p in enumerate(self.presets):
            if p['name'] == name:
                self.presets[i] = entry
                break
        else:
            if len(self.presets) >= 40:
                raise ValueError('at most 40 presets')
            self.presets.append(entry)
        config.save_presets(self.presets)
        if effect == self.engine.name and params == self.engine.params:
            self.preset = name
            self._dirty(time.ticks_ms())
        return self.presets

    def delete_preset(self, name):
        self.presets = [p for p in self.presets if p['name'] != name]
        config.save_presets(self.presets)
        if self.preset == name:
            self.preset = None
        return self.presets

    def order_presets(self, names):
        if not isinstance(names, list):
            raise ValueError('names: a list of preset names')
        by = {}
        for p in self.presets:
            by[p['name']] = p
        out = [by.pop(n) for n in names if n in by]
        self.presets = out + [p for p in self.presets if p['name'] in by]
        config.save_presets(self.presets)
        return self.presets

    # --- layout -----------------------------------------------------------------------
    def layout_json(self):
        lc = self.cfg['layout']
        d = {'panels': self.bus.npanels, 'raw': self.raw.hex(),
             'unstable': self.bus.layout_unstable}
        err = self.bus_error or self.layout_error
        if err:
            d['error'] = err
        if self.layout:
            for k in ('psu', 'positions', 'links', 'tree'):
                d[k] = self.layout[k]
        d['neighbours'] = self.engine.ctx.adj
        d['config'] = {'rotation': lc.get('rotation', 0), 'mirror': lc.get('mirror', False),
                       'names': lc.get('names') or {}, 'disabled': lc.get('disabled') or []}
        d['stale'] = bool(lc.get('raw')) and lc.get('raw') != d['raw']
        return d

    def update_layout(self, body):
        lc = self.cfg['layout']
        n = self.bus.npanels
        if 'rotation' in body:
            lc['rotation'] = _int(body['rotation'], -360, 720, 'rotation') % 360
        if 'mirror' in body:
            lc['mirror'] = bool(body['mirror'])
        if isinstance(body.get('names'), dict):
            names = {}
            for k, v in body['names'].items():
                s = str(v or '').strip()[:24]
                if s and str(k).isdigit() and int(k) < n:
                    names[str(int(k))] = s
            lc['names'] = names
        if isinstance(body.get('disabled'), list):
            lc['disabled'] = sorted(set(int(x) for x in body['disabled']
                                        if 0 <= int(x) < n))
        lc['raw'] = self.raw.hex()
        config.save(self.cfg)
        self._rebuild(time.ticks_ms())
        return self.layout_json()

    def identify(self, i, ms=3000):
        now = time.ticks_ms()
        cols = [(0, 0, 0, 0, 1)] * self.bus.npanels
        cols[i] = (255, 255, 255, 255, 1)
        self.bus.push_colors(cols)
        self.hold_until = time.ticks_add(now, max(300, min(int(ms or 3000), 30000)))
        return {'identify': i}

    # --- touch ------------------------------------------------------------------------
    def _touch_cfg(self):
        t = self.cfg['touch']
        on = t.get('tap', 'none') != 'none' or t.get('hold', 'none') != 'none'
        for o in (t.get('panels') or {}).values():
            if o.get('tap', 'none') != 'none' or o.get('hold', 'none') != 'none':
                on = True
        self.touch_on = on

    def _actions(self, i):
        t = self.cfg['touch']
        o = (t.get('panels') or {}).get(str(i)) or {}
        return o.get('tap', t.get('tap', 'none')), o.get('hold', t.get('hold', 'none'))

    def _run(self, action, i, now):
        if not action or action == 'none':
            return
        if action == 'effect':
            self.engine.touch(i, now)
        elif action == 'next':
            self.step_preset(1, now)
        elif action == 'prev':
            self.step_preset(-1, now)
        elif action == 'power':
            self.set_power(not self.power, now)
        elif action == 'bright':
            lower = [b for b in BRIGHT_STEPS if b < self.brightness - 1]
            self.set_brightness(lower[0] if lower else BRIGHT_STEPS[0], now)
        elif action.startswith('preset:'):
            self.apply_preset(action[7:], now)

    def _press(self, i, now):
        """Returns True when the press is fully handled (no hold to follow)."""
        self.touches += 1
        if not self.power:            # any touch wakes the panels up
            tap, hold = self._actions(i)
            if tap != 'none' or hold != 'none':
                self.set_power(True, now)
            return True
        tap, hold = self._actions(i)
        if tap == 'effect':
            self._run('effect', i, now)
        elif hold == 'none':
            self._run(tap, i, now)
            return True
        return False

    def _touch(self, status, now):
        for i in range(len(status)):
            on = panelbus.is_touched(status[i])
            t0 = self.pressed.get(i)
            if on:
                if t0 is None:
                    self.pressed[i] = now
                    if self._press(i, now):
                        self.held.add(i)
                elif i not in self.held and _since(now, t0) >= HOLD_MS:
                    self.held.add(i)
                    self._run(self._actions(i)[1], i, now)
            elif t0 is not None:
                del self.pressed[i]
                if i in self.held:
                    self.held.discard(i)
                else:
                    tap = self._actions(i)[0]
                    if tap != 'effect':
                        self._run(tap, i, now)

    def touch_event(self, i, hold=False):
        """A touch from the web UI, handled like a real one."""
        now = time.ticks_ms()
        if not self._press(i, now):
            tap, hold_a = self._actions(i)
            if hold:
                self._run(hold_a, i, now)
            elif tap != 'effect':
                self._run(tap, i, now)
        return self.state_json()

    # --- settings -----------------------------------------------------------------------
    def settings_json(self):
        c = config.merge(self.cfg, {})
        for k in ('wifi', 'ap'):
            if c[k].get('key'):
                c[k]['key'] = MASK
        return c

    def update_settings(self, body):
        cfg = self.cfg
        restart = reconnect = False
        for k, v in body.items():
            if k not in config.DEFAULTS or k == 'layout':
                continue
            old = cfg[k]
            if k in ('wifi', 'ap'):
                d = dict(old)
                if 'ssid' in v:
                    d['ssid'] = str(v['ssid'] or '')[:32]
                if 'key' in v and v['key'] != MASK:
                    d['key'] = str(v['key'] or '')[:64]
                    if k == 'ap' and d['key'] and len(d['key']) < 8:
                        raise ValueError('access point key: at least 8 characters, or empty for an open AP')
                if 'always' in v and k == 'ap':
                    d['always'] = bool(v['always'])
                if d != old:
                    reconnect = reconnect or k == 'wifi'
                    restart = restart or k == 'ap'
                cfg[k] = d
            elif k == 'startup':
                d = dict(old)
                if 'mode' in v:
                    if v['mode'] not in STARTUP_MODES:
                        raise ValueError('startup mode: one of %s' % ', '.join(STARTUP_MODES))
                    d['mode'] = v['mode']
                if 'preset' in v:
                    d['preset'] = str(v['preset'] or '')[:32]
                if 'brightness' in v:
                    d['brightness'] = _int(v['brightness'], -1, 255, 'startup brightness')
                cfg[k] = d
            elif k == 'touch':
                d = dict(old)
                for a in ('tap', 'hold'):
                    if a in v:
                        d[a] = _action(v[a])
                if isinstance(v.get('panels'), dict):
                    ps = {}
                    for ch, o in v['panels'].items():
                        o2 = {}
                        for a in ('tap', 'hold'):
                            if o.get(a):
                                o2[a] = _action(o[a])
                        if o2 and str(ch).isdigit():
                            ps[str(int(ch))] = o2
                    d['panels'] = ps
                cfg[k] = d
            elif k == 'streams':
                d = dict(old)
                if 'enabled' in v:
                    d['enabled'] = bool(v['enabled'])
                if 'timeout' in v:
                    d['timeout'] = _int(v['timeout'], 1, 60, 'stream timeout')
                cfg[k] = d
            elif k == 'schedule':
                cfg[k] = clean_schedule(v)
            elif k == 'uart':
                d = {'tx': _int(v.get('tx', old['tx']), 0, 48, 'tx pin'),
                     'rx': _int(v.get('rx', old['rx']), 0, 48, 'rx pin')}
                restart = restart or d != old
                cfg[k] = d
            elif k in ('ddp_port', 'e131_port', 'http_port'):
                cfg[k] = _int(v, 1, 65535, k)
                restart = restart or cfg[k] != old
            elif k == 'e131_universe':
                cfg[k] = _int(v, 1, 63999, k)
                restart = restart or cfg[k] != old
            elif k == 'e131_channels_per_panel':
                cfg[k] = _int(v, 3, 4, k)
                if self.e131:
                    self.e131.per = cfg[k]
            elif k == 'hostname':
                s = str(v or '').strip().lower()[:32]
                if not s or not all(ch.isalpha() or ch.isdigit() or ch == '-' for ch in s):
                    raise ValueError('hostname: letters, digits and dashes')
                cfg[k] = s
                restart = restart or s != old
            elif k == 'brightness':
                cfg[k] = _int(v, 0, 255, k)
            elif k == 'timezone':
                cfg[k] = _int(v, -720, 840, 'timezone (minutes)')
                self.minute = None
            elif k == 'debug':
                cfg[k] = bool(v)
            elif k == 'ntp':
                cfg[k] = str(v or 'pool.ntp.org')[:64]
                self.ntp_next = time.ticks_ms()
        config.save(cfg)
        self._touch_cfg()
        self._apply_streams()
        if reconnect and self.wifi:
            self.wifi.connect()
        return {'saved': True, 'restart': restart, 'settings': self.settings_json()}

    def _apply_streams(self):
        on = self.cfg['streams'].get('enabled', True)
        for r in (self.ddp, self.e131):
            if r:
                r.enabled = on

    def wifi_scan(self):
        if not self.wifi:
            return []
        return self.wifi.scan()

    def reboot(self):
        self.reboot_at = time.ticks_add(time.ticks_ms(), 800)
        return {'rebooting': True}

    # --- clock and schedule ------------------------------------------------------------
    def clock_ok(self):
        return time.gmtime()[0] >= 2024

    def local(self):
        return time.gmtime(int(time.time()) + self.cfg['timezone'] * 60)

    def clock_str(self):
        if not self.clock_ok():
            return None
        lt = self.local()
        return '%04d-%02d-%02d %02d:%02d' % (lt[0], lt[1], lt[2], lt[3], lt[4])

    def _schedule(self, now):
        if not self.clock_ok():
            return
        t = int(time.time()) + self.cfg['timezone'] * 60
        m = t // 60
        if m == self.minute:
            return
        first = self.minute is None
        self.minute = m
        if first:
            return
        lt = time.gmtime(t)
        hhmm = '%02d:%02d' % (lt[3], lt[4])
        for e in self.cfg['schedule']:
            if (e.get('enabled', True) and e.get('time') == hhmm and
                    (not e.get('days') or lt[6] in e['days'])):
                self.run_entry(e, now)

    def run_entry(self, e, now):
        self._log('schedule %s: %s %s' % (e.get('time'), e.get('action'), e.get('preset', '')))
        b = e.get('brightness', -1)
        if isinstance(b, int) and b >= 0:
            self.set_brightness(b, now)
        a = e.get('action')
        if a == 'off':
            self.set_power(False, now)
        elif a == 'on':
            self.set_power(True, now)
        elif a == 'preset':
            self.apply_preset(e.get('preset'), now)

    def _ntp(self, now):
        if (self.ntp_next is None or not self.wifi or not self.wifi.connected or
                _since(now, self.ntp_next) < 0):
            return
        try:
            import ntptime
            ntptime.host = self.cfg['ntp']
            ntptime.settime()
            self.ntp_ok = True
            self.ntp_next = time.ticks_add(now, NTP_REFRESH_MS)
            self.minute = None
            self._log('clock:', self.clock_str())
        except Exception as e:
            self.ntp_ok = self.clock_ok()
            self.ntp_next = time.ticks_add(now, NTP_RETRY_MS)
            if not self.ntp_ok:
                self._log('clock: NTP failed:', e)

    # --- info ---------------------------------------------------------------------------
    def info_json(self):
        now = time.ticks_ms()
        d = {'version': VERSION, 'hostname': self.cfg['hostname'],
             'mac': self.wifi.mac() if self.wifi else None,
             'uptime': _since(now, self.boot) // 1000, 'panels': self.bus.npanels,
             'fps': round(self.fps, 1), 'source': self.source(),
             'bus_error': self.bus_error, 'time': self.clock_str(),
             'timezone': self.cfg['timezone'], 'touch_polling': self.touch_on,
             'platform': sys.platform,
             'micropython': '.'.join(str(x) for x in sys.implementation.version[:3]),
             'loop_hz': round(self.loop_rate),
             'errors': self.errors, 'last_error': self.last_error,
             'net_starved': self.net_starved,
             'last_crash': self._crash_log(),
             'bus_ms': {'push': getattr(self.bus, 't_push', 0) / 1000,
                        'poll': getattr(self.bus, 't_reply', 0) / 1000,
                        'short_replies': getattr(self.bus, 'short_replies', 0),
                        'hotplug_replies': getattr(self.bus, 'hotplug_replies', 0),
                        'keepalives': getattr(self.bus, 'keepalives', 0)}}
        d['idf_heap'] = idf_heap()
        try:
            d['mem_free'] = gc.mem_free()
            d['mem_alloc'] = gc.mem_alloc()
        except AttributeError:
            pass
        if self.ddp:
            d['ddp'] = {'port': self.cfg['ddp_port'], 'frames': self.ddp.frames}
            d['e131'] = {'port': self.cfg['e131_port'], 'universe': self.cfg['e131_universe'],
                         'group': self.e131.group, 'frames': self.e131.frames}
            d['http_requests'] = self.web.requests
            d['http'] = dict(self.web.stats, open=len(self.web.conns))
        w = self.wifi
        if w:
            d['wifi'] = {'connected': w.connected, 'ip': w.ip,
                         'ssid': self.cfg['wifi'].get('ssid'), 'rssi': w.rssi(),
                         'ap': w.ap_ssid() if w.ap_on else None, 'ap_ip': w.ap_ip()}
        return d

    @staticmethod
    def _crash_log():
        """The traceback main.py saved before its last restart, if any."""
        try:
            with open('crash.log') as f:
                return f.read()[:1500]
        except OSError:
            return None

    def legacy_state(self):
        """The /state reply of the first firmware version."""
        raw = self.last_pull or b''
        n = self.bus.npanels
        d = {'panels': n, 'touch': panelbus.touch_status(raw, n) or [],
             'hotplug': len(raw) > 2 * n, 'raw': raw.hex()}
        if self.ddp:
            d['ddp_frames'] = self.ddp.frames
            d['e131_frames'] = self.e131.frames
        return d

    # --- loop ---------------------------------------------------------------------------
    def start_network(self):
        cfg = self.cfg
        self.wifi = wifi.WiFi(cfg)
        self.ddp = ddp.DDPReceiver(self.bus, port=cfg['ddp_port'])
        self.e131 = e131.E131Receiver(self.bus, universe=cfg['e131_universe'],
                                      port=cfg['e131_port'],
                                      per=cfg['e131_channels_per_panel'])
        self.web = web.WebServer(web.make_handler(self), port=cfg['http_port'])
        if cfg.get('debug'):
            web.TRACE = lambda ev: print('web %-16s idf %s gc %d' % (ev, idf_heap(), gc.mem_free() + gc.mem_alloc()))
        self._apply_streams()

    def _network_up(self, now):
        cfg = self.cfg
        ip = self.wifi.ip
        if self.e131.group is None:
            self.e131._join(cfg['e131_universe'])
        self.ntp_next = now
        self._log('Wi-Fi  %s' % cfg['wifi'].get('ssid'))
        self._log('Web UI http://%s%s  (http://%s.local)'
                  % (ip, '' if cfg['http_port'] == 80 else ':%d' % cfg['http_port'],
                     cfg['hostname']))
        self._log('DDP    udp://%s:%d' % (ip, cfg['ddp_port']))
        self._log('E1.31  udp://%s:%d  universe %d%s'
                  % (ip, cfg['e131_port'], cfg['e131_universe'],
                     '  group %s' % self.e131.group if self.e131.group else ', unicast only'))

    def run(self):
        if self.web is None:
            self.start_network()
        self._log('panels: %d%s%s' % (self.bus.npanels,
                                      ' (layout string unstable)' if self.bus.layout_unstable else '',
                                      '  BUS ERROR: %s' % self.bus_error if self.bus_error else ''))
        self._log('playing: %s%s' % (self.engine.name,
                                     ' (preset %s)' % self.preset if self.preset else ''))
        poller = select.poll()
        handlers = {}
        for sock, fn in ((self.ddp.sock, self.ddp.tick), (self.e131.sock, self.e131.tick),
                         (self.web.sock, self.web.accept)):
            poller.register(sock, select.POLLIN)
            handlers[id(sock)] = fn
        gc.collect()
        burst = 0
        t_err = time.ticks_ms()
        t_starved = None
        while True:
            try:
                try:
                    ready = poller.poll(1 if self.web.conns else 4)
                except OSError as e:
                    if not e.args or e.args[0] != EIO:
                        raise
                    # ESP-IDF's select() allocates on every call; EIO means the
                    # Wi-Fi/TCP heap is exhausted for the moment (packets queued
                    # while this loop was busy). Not a fault to restart for:
                    # keep the lights going and give the network stack time.
                    ready = ()
                    self.net_starved += 1
                    now = time.ticks_ms()
                    if t_starved is None or _since(now, t_starved) >= 5000:
                        t_starved = now
                        self._log('network: out of memory for a moment (ESP-IDF heap %s)'
                                  % (idf_heap(),))
                    time.sleep_ms(5)
                for sock, _ in ready:
                    h = handlers.get(id(sock))
                    if h:
                        h()
                if self.web.conns:
                    self.web.service()
                    if self.web.full():   # waiting connections: no busy loop on the
                        time.sleep_ms(1)  # listener that stays readable meanwhile
                self.step(time.ticks_ms())
            except Exception as e:
                # one bad packet or request must not take the lights down;
                # a steady stream of errors still ends in main.py's restart
                now = time.ticks_ms()
                burst = burst + 1 if _since(now, t_err) < 10000 else 1
                t_err = now
                self.errors += 1
                self.last_error = repr(e)
                sys.print_exception(e)
                if burst > 20:
                    raise
                time.sleep_ms(50)

    def step(self, now):
        """One pass: streams, effect frame, bus poll, housekeeping."""
        self.loops += 1
        if self.ddp:
            total = self.ddp.frames + self.e131.frames
            if total != self._seen:
                self.streaming = 'ddp' if self.ddp.frames != self._seen_ddp else 'e131'
                self._seen = total
                self._seen_ddp = self.ddp.frames
                self.stream_until = time.ticks_add(
                    now, self.cfg['streams'].get('timeout', 3) * 1000)
                self.last_frame = now
            elif self.streaming and _since(now, self.stream_until) >= 0:
                self.streaming = None
                self._resume(now)
        if self.hold_until is not None and _since(now, self.hold_until) >= 0:
            self.hold_until = None
            self._resume(now)
        if not self.streaming and self.hold_until is None:
            if self.power:
                fr = self.engine.tick(now)
                if fr is not None:
                    self.bus.push_colors(fr)
                    self.last_frame = now
            elif _since(now, self.last_off) >= effects.REFRESH_MS:
                self._black(now, 1)
        self._poll(now)
        if _since(now, self.last_house) >= HOUSEKEEP_MS:
            self.last_house = now
            self._housekeeping(now)

    def _resume(self, now):
        if self.power:
            self.engine.resume(now)
        else:
            self._black(now)

    def _poll(self, now):
        keep = _since(now, self.last_keep)
        if self.touch_on and not self.bus_error:
            due = keep >= TOUCH_POLL_MS
        else:
            due = keep >= MAX_NOPOLL_MS or (
                _since(now, self.last_frame) > IDLE_MS and keep >= KEEP_MS)
        if not due:
            return
        r = self.bus.bulk_pull()
        self.last_keep = now
        if r:
            self.last_pull = r
        # A moved panel silently stops applying frames until the bus is
        # enumerated again; the trailing 0xCC in the poll reply is the
        # signal (PROTOCOL.md §8). A short reply is not: far panels
        # legitimately miss the window now and then.
        n = self.bus.npanels
        if r and panelbus.HOTSWAP in r:
            if _since(now, self.last_rescan) >= RESCAN_MS:
                self.rescan()
                if n != self.bus.npanels:
                    self._log('assembly changed: %d -> %d panels' % (n, self.bus.npanels))
            return
        if self.touch_on:
            st = panelbus.touch_status(r, n)
            if st:
                self._touch(st, now)

    def _housekeeping(self, now):
        if _since(now, self.last_gc) >= GC_MS:
            # small, frequent collections keep the heap from fragmenting;
            # a fragmented heap grows into the memory Wi-Fi and TCP need
            self.last_gc = now
            gc.collect()
        if self.bus_error and _since(now, self.last_rescan) >= BUS_RETRY_MS:
            if self.rescan():
                self._log('bus is back: %d panels' % self.bus.npanels)
                self.engine.restart(now, fade=8)
        if self.off_at is not None and _since(now, self.off_at) >= 0:
            self.off_at = None
            self.set_power(False, now)
        if self.wifi:
            ev = self.wifi.tick(now)
            if ev == 'up':
                self._network_up(now)
            elif ev == 'down':
                self._log('Wi-Fi lost, reconnecting')
            self._ntp(now)
        self._schedule(now)
        if self.dirty_at is not None and _since(now, self.dirty_at) >= SAVE_DELAY_MS:
            try:
                self.save_state()
            except OSError as e:
                self._log('state.json:', e)
                self.dirty_at = None
        dt = _since(now, self._fps_t)
        if dt >= 1000:
            if self.cfg.get('debug') and self.web:
                st = self.web.stats
                idf = idf_heap()
                print('dbg t=%d fps=%.0f loops=%.0f http open=%d acc=%d req=%d tmo=%d evict=%d rst=%d gc=%d+%d idf=%s'
                      % (_since(now, self.boot) // 1000, self.fps, self.loop_rate,
                         len(self.web.conns), st['accepted'], self.web.requests, st['timeouts'],
                         st['evicted'], st['resets'], gc.mem_free(), gc.mem_alloc(), idf))
            self.fps = (self.bus.pushes - self._fps_n) * 1000 / dt
            self._fps_n = self.bus.pushes
            self.loop_rate = self.loops * 1000 / dt
            self.loops = 0
            self._fps_t = now
        if self.reboot_at is not None and _since(now, self.reboot_at) >= 0:
            if self.dirty_at is not None:
                self.save_state()
            import machine
            machine.reset()


def serve(cfg=None, verbose=True):
    App(cfg, verbose=verbose).run()
