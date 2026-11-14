"""Board configuration and the small JSON files the board keeps.

    config.json    settings: Wi-Fi, pins, ports, startup behaviour, touch
                   actions, schedule, layout view. Written by the web UI;
                   a hand-written file with only some keys works too,
                   missing keys fall back to DEFAULTS.
    presets.json   saved effects (name, effect, params), DEFAULT_PRESETS
                   until the first change
    state.json     what is playing, for startup mode 'last'

Files are replaced atomically (write to .tmp, rename) so a reset in the
middle of a save never leaves a broken file behind.
"""
import json
import os

DEFAULTS = {
    'wifi': {'ssid': '', 'key': ''},
    # access point opened when the Wi-Fi above is missing or unreachable;
    # empty ssid = LeafBus-XXXX from the MAC address
    'ap': {'ssid': '', 'key': 'leafbus1'},
    'hostname': 'leafbus',          # mDNS: http://leafbus.local
    'uart': {'tx': 17, 'rx': 16},
    'ddp_port': 4048,
    'e131_port': 5568,
    'e131_universe': 1,
    'e131_channels_per_panel': 3,
    'http_port': 80,
    'brightness': 255,              # used when nothing else says otherwise
    'timezone': 0,                  # minutes east of UTC, for the schedule
    'ntp': 'pool.ntp.org',
    # what plays after power-up:
    #   'last'   whatever played before (and the power state)
    #   'preset' always the preset named below
    #   'off'    panels dark until told otherwise
    # brightness -1 = keep the last one
    'startup': {'mode': 'last', 'preset': '', 'brightness': -1},
    # touch actions: none, effect, next, prev, power, bright, preset:<name>
    # 'panels' overrides per chunk index: {"3": {"tap": "power"}}
    'touch': {'tap': 'effect', 'hold': 'power', 'panels': {}},
    # DDP / E1.31 take over while frames arrive, effects resume after
    # 'timeout' seconds of silence
    'streams': {'enabled': True, 'timeout': 3},
    # how effects see the assembly; names/disabled are keyed by chunk index
    # and 'raw' remembers the layout string they were made for
    'layout': {'rotation': 0, 'mirror': False, 'names': {}, 'disabled': [],
               'raw': ''},
    # [{"enabled": true, "time": "07:00", "days": [0,1,2,3,4],
    #   "action": "preset", "preset": "Wake up", "brightness": -1}, ...]
    'schedule': [],
    # one status line per second on the serial console
    'debug': False,
}

DEFAULT_PRESETS = [
    {'name': 'Rainbow', 'effect': 'rainbow', 'params': {}},
    {'name': 'Warm white', 'effect': 'solid',
     'params': {'color': '#ff8a3c', 'white': 255}},
    {'name': 'Sunset', 'effect': 'flow',
     'params': {'colors': ['#ff3c50', '#ff8c28', '#ffd020', '#e8406a', '#6a0a8a'],
                'direction': 'vertical', 'speed': 10, 'scale': 80}},
    {'name': 'Ocean', 'effect': 'plasma',
     'params': {'colors': ['#001848', '#0060a0', '#00c0c0', '#40ffd0', '#0090ff'],
                'speed': 20, 'scale': 40}},
    {'name': 'Fireplace', 'effect': 'fire', 'params': {}},
    {'name': 'Starry night', 'effect': 'twinkle', 'params': {}},
    {'name': 'Party', 'effect': 'mosaic',
     'params': {'colors': ['#ff0050', '#ffcc00', '#00ff90', '#0080ff', '#c000ff'],
                'interval': 2, 'fade': 1}},
    {'name': 'Ripples', 'effect': 'ripple', 'params': {}},
    {'name': 'Wake up', 'effect': 'sunrise', 'params': {'minutes': 20}},
]


def _copy(obj):
    return json.loads(json.dumps(obj))


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(obj, f)
    try:
        os.replace(tmp, path)        # CPython (host tests, simulator)
    except AttributeError:
        os.rename(tmp, path)         # MicroPython: LittleFS rename replaces


def merge(base, extra):
    """One level deep: dict values are merged, everything else replaced."""
    out = _copy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            d = out[k]
            for k2, v2 in v.items():
                d[k2] = v2
        else:
            out[k] = v
    return out


def load(path='config.json'):
    return merge(DEFAULTS, load_json(path, {}))


def save(cfg, path='config.json'):
    save_json(path, cfg)


def load_presets(path='presets.json'):
    p = load_json(path)
    if not isinstance(p, list):
        return _copy(DEFAULT_PRESETS)
    return [x for x in p if isinstance(x, dict) and 'name' in x and 'effect' in x]


def save_presets(presets, path='presets.json'):
    save_json(path, presets)
