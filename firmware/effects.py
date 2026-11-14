"""On-board effects: the assembly plays by itself, no network needed.

Pure Python over the layout geometry; runs unchanged in MicroPython and on
the host (tests, tools/simulator.py).

    ctx = Ctx(n, positions, links)          # geometry.layout(raw) output
    eng = Engine(ctx)
    eng.set('flow', {'speed': 40}, now)
    frame = eng.tick(now)                   # list for bus.push_colors or None

An effect sees every panel through normalised features computed once per
layout (Ctx): position x/y in 0..1, angle around the centre, distance from
the centre, chunk order and the neighbours of each panel. Effects return a
whole frame, one entry per chunk: (r, g, b, w, transition) or None to leave
a panel alone.

Slow effects lean on the panels' own fades instead of computing every step:
one frame with a long transition time T and the panel interpolates by
itself (PROTOCOL.md §6.2: T is presumably 100 ms steps). That costs nothing
on the interpreter and is smoother than ~16 software frames per second.

Adding an effect: subclass Effect, describe PARAMS, implement frame(), add
it to EFFECTS. The web UI builds its controls from PARAMS.
"""
import math
import random
import time

import geometry

BLACK = (0, 0, 0, 0)
REFRESH_MS = 5000     # re-send a static frame this often (no ack on the bus)
TWO_PI = 2 * math.pi


# --- colors -----------------------------------------------------------------

def parse_color(s):
    """'#rrggbb', '#rrggbbww', '#rgb' or [r, g, b(, w)] -> (r, g, b, w)."""
    if isinstance(s, (list, tuple)):
        v = [int(x) & 255 for x in s]
    else:
        h = str(s).strip().lstrip('#')
        if len(h) == 3:
            h = h[0] * 2 + h[1] * 2 + h[2] * 2
        v = []
        try:
            for i in range(0, min(len(h), 8) - 1, 2):
                v.append(int(h[i:i + 2], 16))
        except ValueError:
            v = []
    while len(v) < 4:
        v.append(0)
    return (v[0], v[1], v[2], v[3])


def color_str(c):
    """(r, g, b, w) -> '#rrggbb', or '#rrggbbww' when white is used."""
    if len(c) > 3 and c[3]:
        return '#%02x%02x%02x%02x' % (c[0], c[1], c[2], c[3])
    return '#%02x%02x%02x' % (c[0], c[1], c[2])


def mix(a, b, f):
    if f <= 0:
        return a
    if f >= 1:
        return b
    return (int(a[0] + (b[0] - a[0]) * f), int(a[1] + (b[1] - a[1]) * f),
            int(a[2] + (b[2] - a[2]) * f), int(a[3] + (b[3] - a[3]) * f))


def dim(c, f):
    return (int(c[0] * f), int(c[1] * f), int(c[2] * f), int(c[3] * f))


def with_t(c, t):
    return (c[0], c[1], c[2], c[3], max(1, min(255, int(t))))


class Palette:
    """Colors interpolated into a lookup table; at(f) for f in 0..1.
    wrap=True joins the last color back to the first (for scrolling)."""

    def __init__(self, colors, wrap=True, size=64):
        cs = [parse_color(c) for c in colors] or [(255, 255, 255, 0)]
        if wrap:
            cs = cs + [cs[0]]
        n = len(cs) - 1
        self.wrap = wrap
        self.size = size
        self.lut = []
        for k in range(size):
            if n == 0:
                self.lut.append(cs[0])
                continue
            f = k * n / (size if wrap else size - 1)
            i = min(int(f), n - 1)
            self.lut.append(mix(cs[i], cs[i + 1], f - i))

    def at(self, f):
        """Blends the two nearest table entries: a slow scroll changes the
        color smoothly instead of in 64 visible steps."""
        x = f * self.size
        k = math.floor(x)
        if self.wrap:
            a = self.lut[k % self.size]
            b = self.lut[(k + 1) % self.size]
        else:
            if k < 0:
                return self.lut[0]
            if k >= self.size - 1:
                return self.lut[self.size - 1]
            a, b = self.lut[k], self.lut[k + 1]
        return mix(a, b, x - k)


# --- geometry as effects see it -----------------------------------------------

class Ctx:
    """Per-panel features, indexed by chunk.

    x, y   0..1 across the bounding box (after rotation / mirror)
    ix, iy same, but one scale for both axes (aspect kept)
    ang    0..1 counter-clockwise around the centre
    rad    0..1 distance from the centre
    idx    0..1 chunk order
    adj    neighbouring chunks
    on     False for panels excluded from effects (kept dark)
    """

    def __init__(self, n, positions=None, links=(), rotation=0, mirror=False,
                 disabled=()):
        n = max(1, n)
        self.n = n
        self.on = [i not in disabled for i in range(n)]
        self.idx = [i / (n - 1) if n > 1 else 0.0 for i in range(n)]
        pos = {}
        for p in positions or ():
            pos[p['chunk']] = p
        known = len(pos) == n
        a = math.radians(rotation or 0)
        ca, sa = math.cos(a), math.sin(a)
        xs, ys = [], []
        for i in range(n):
            p = pos.get(i)
            x, y = (p['x'], p['y']) if p else (float(i), 0.0)
            if mirror:
                x = -x
            xs.append(x * ca - y * sa)
            ys.append(x * sa + y * ca)
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        w, h = (x1 - x0) or 1.0, (y1 - y0) or 1.0
        s = max(w, h)
        self.x = [(v - x0) / w if x1 > x0 else 0.5 for v in xs]
        self.y = [(v - y0) / h if y1 > y0 else 0.5 for v in ys]
        self.ix = [(v - x0) / s for v in xs]
        self.iy = [(v - y0) / s for v in ys]
        cx, cy = sum(xs) / n, sum(ys) / n
        d = [math.sqrt((xs[i] - cx) ** 2 + (ys[i] - cy) ** 2) for i in range(n)]
        dmax = max(d) or 1.0
        self.rad = [v / dmax for v in d]
        self.ang = [(math.atan2(ys[i] - cy, xs[i] - cx) / TWO_PI) % 1.0
                    for i in range(n)]
        if known:
            self.adj = geometry.neighbours([pos[i] for i in range(n)], links)
        else:
            self.adj = [[j for j in (i - 1, i + 1) if 0 <= j < n] for i in range(n)]
        for i in range(n):  # a panel without neighbours falls back to the chain
            if not self.adj[i] and n > 1:
                self.adj[i] = [j for j in (i - 1, i + 1) if 0 <= j < n]
        self._hops = {}

    def hops(self, src):
        """Hop count from panel src to every panel (breadth-first)."""
        h = self._hops.get(src)
        if h is not None:
            return h
        h = [-1] * self.n
        h[src] = 0
        q = [src]
        k = 0
        while k < len(q):
            i = q[k]
            k += 1
            for j in self.adj[i]:
                if h[j] < 0:
                    h[j] = h[i] + 1
                    q.append(j)
        far = max(h) + 1
        h = [v if v >= 0 else far for v in h]
        if len(self._hops) > 12:
            self._hops = {}
        self._hops[src] = h
        return h


# --- parameters ---------------------------------------------------------------
# (key, label, type, default[, min, max | options])
# types: color, colors (palette), scene (one color per panel), int, bool, select

RAINBOW = ['#ff0000', '#ffb000', '#d0ff00', '#00ff40', '#00e0ff', '#0040ff', '#c000ff']
DIRECTIONS = ('horizontal', 'vertical', 'diagonal', 'radial', 'spin', 'order')


def _int(v, lo, hi, dv):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return dv


def clean(cls, given, n=1):
    """Params for cls: defaults filled in, values coerced and clamped."""
    given = given if isinstance(given, dict) else {}
    out = {}
    for d in cls.PARAMS:
        k, typ, dv = d[0], d[2], d[3]
        v = given.get(k, dv)
        if typ == 'int':
            v = _int(v, d[4], d[5], dv)
        elif typ == 'bool':
            v = v in (True, 1, 'true', '1', 'on')
        elif typ == 'select':
            v = v if v in d[4] else dv
        elif typ == 'color':
            v = color_str(parse_color(v))
        elif typ == 'colors':
            v = [color_str(parse_color(c)) for c in v] if isinstance(v, list) else []
            v = v[:16] or list(dv)
        elif typ == 'scene':
            v = [color_str(parse_color(c)) for c in v] if isinstance(v, list) else []
            v = v[:n]
            fill = v[-1] if v else '#ff8a3c'
            while len(v) < n:
                v.append(fill)
        out[k] = v
    return out


def schema(cls):
    ps = []
    for d in cls.PARAMS:
        p = {'key': d[0], 'label': d[1], 'type': d[2], 'default': d[3]}
        if d[2] == 'int':
            p['min'], p['max'] = d[4], d[5]
        elif d[2] == 'select':
            p['options'] = list(d[4])
        ps.append(p)
    return {'label': cls.LABEL, 'desc': cls.DESC, 'touch': cls.TOUCH, 'params': ps}


# --- effects -------------------------------------------------------------------

class Effect:
    LABEL = ''
    DESC = ''
    TOUCH = False          # reacts to touch
    PARAMS = ()
    INTERVAL = 50          # ms between frame() calls

    def __init__(self, ctx, p):
        self.ctx = ctx
        self.n = ctx.n
        self.p = p
        self.interval = self.INTERVAL
        self.setup()

    def setup(self):
        pass

    def frame(self, t):
        """t: ms since the effect started. A list (one entry per chunk) or
        None for 'nothing changed'."""
        return None

    def touch(self, i, t):
        pass


class Solid(Effect):
    LABEL = 'Solid'
    DESC = 'One colour on every panel.'
    PARAMS = (('color', 'Colour', 'color', '#ff8a3c'),
              ('white', 'White LEDs', 'int', 0, 0, 255))
    INTERVAL = 250

    def setup(self):
        c = parse_color(self.p['color'])
        self.c = (c[0], c[1], c[2], max(c[3], self.p['white']))
        self.sent = False

    def frame(self, t):
        if self.sent:
            return None
        self.sent = True
        return [self.c] * self.n


class Scene(Effect):
    LABEL = 'Scene'
    DESC = 'A colour for each panel, painted in the layout view.'
    PARAMS = (('colors', 'Panel colours', 'scene', []),
              ('white', 'White LEDs', 'int', 0, 0, 255))
    INTERVAL = 250

    def setup(self):
        w = self.p['white']
        self.cols = []
        for s in self.p['colors']:
            c = parse_color(s)
            self.cols.append((c[0], c[1], c[2], max(c[3], w)))
        self.sent = False

    def frame(self, t):
        if self.sent:
            return None
        self.sent = True
        return self.cols


class Flow(Effect):
    LABEL = 'Flow'
    DESC = 'A palette laid across the assembly; set speed 0 for a still gradient.'
    PARAMS = (('colors', 'Palette', 'colors',
               ['#ff3c50', '#ff8c28', '#ffd020', '#20c080', '#2080ff', '#8040ff']),
              ('direction', 'Direction', 'select', 'horizontal', DIRECTIONS),
              ('scale', 'Spread %', 'int', 100, 10, 400),
              ('speed', 'Speed', 'int', 20, 0, 100),
              ('reverse', 'Reverse', 'bool', False))

    def setup(self):
        c, p = self.ctx, self.p
        self.pal = Palette(p['colors'])
        d = p['direction']
        if d == 'horizontal':
            pos = c.x
        elif d == 'vertical':
            pos = c.y
        elif d == 'diagonal':
            pos = [(c.x[i] + c.y[i]) / 2 for i in range(self.n)]
        elif d == 'radial':
            pos = c.rad
        elif d == 'spin':
            pos = c.ang
        else:
            pos = c.idx
        k = p['scale'] / 100 * (-1 if p['reverse'] else 1)
        self.pos = [v * k for v in pos]
        s = p['speed'] / 100
        self.rate = s * s * 1.5              # palette cycles per second
        self.sent = False

    def frame(self, t):
        if self.rate == 0:
            if self.sent:
                return None
            self.sent = True
        ph = t * 0.001 * self.rate
        at = self.pal.at
        return [at(v - ph) for v in self.pos]


class Rainbow(Flow):
    LABEL = 'Rainbow'
    DESC = 'The full spectrum rolling across the layout.'
    PARAMS = (('colors', 'Palette', 'colors', RAINBOW),) + Flow.PARAMS[1:]


class Cycle(Effect):
    LABEL = 'Colour cycle'
    DESC = 'All panels fade together from one colour to the next.'
    PARAMS = (('colors', 'Colours', 'colors', RAINBOW),
              ('hold', 'Hold (s)', 'int', 3, 0, 120),
              ('fade', 'Fade (s)', 'int', 2, 0, 25),
              ('shuffle', 'Shuffle', 'bool', False))
    INTERVAL = 100

    def setup(self):
        self.cols = [parse_color(c) for c in self.p['colors']]
        self.period = max(200, (self.p['hold'] + self.p['fade']) * 1000)
        self.k = -1
        self.i = 0

    def frame(self, t):
        k = t // self.period
        if k == self.k:
            return None
        self.k = k
        m = len(self.cols)
        if self.p['shuffle'] and m > 1:
            i = self.i
            while i == self.i:
                i = random.randrange(m)
            self.i = i
        else:
            self.i = k % m
        return [with_t(self.cols[self.i], self.p['fade'] * 10)] * self.n


class Mosaic(Effect):
    LABEL = 'Mosaic'
    DESC = 'Every panel drifts to a random palette colour on its own time.'
    PARAMS = (('colors', 'Colours', 'colors',
               ['#ff0050', '#ffcc00', '#00ff90', '#0080ff', '#c000ff']),
              ('interval', 'Change every (s)', 'int', 4, 1, 120),
              ('fade', 'Fade (s)', 'int', 2, 0, 25))
    INTERVAL = 100

    def setup(self):
        self.cols = [parse_color(c) for c in self.p['colors']]
        self.cur = [-1] * self.n
        self.due = [0] * self.n

    def frame(self, t):
        out = None
        m = len(self.cols)
        iv = self.p['interval'] * 1000
        for i in range(self.n):
            if t < self.due[i]:
                continue
            if out is None:
                out = [None] * self.n
            k = random.randrange(m)
            if m > 1 and k == self.cur[i]:
                k = (k + 1 + random.randrange(m - 1)) % m
            self.cur[i] = k
            out[i] = with_t(self.cols[k], self.p['fade'] * 10)
            self.due[i] = t + int(iv * (0.5 + random.random()))
        return out


class Breathe(Effect):
    LABEL = 'Breathe'
    DESC = 'Slow swelling and fading; the colour changes at the bottom of each breath.'
    PARAMS = (('colors', 'Colours', 'colors', ['#ff3060', '#3080ff', '#30ffa0']),
              ('period', 'Breath (s)', 'int', 6, 1, 60),
              ('low', 'Lowest %', 'int', 5, 0, 90))

    def frame(self, t):
        ph = t / (self.p['period'] * 1000)
        k = int(ph)
        lo = self.p['low'] / 100
        lvl = lo + (1 - lo) * (0.5 - 0.5 * math.cos(TWO_PI * (ph - k)))
        cs = self.p['colors']
        c = dim(parse_color(cs[k % len(cs)]), lvl)
        return [c] * self.n


class Twinkle(Effect):
    LABEL = 'Twinkle'
    DESC = 'Random panels flash and fade back into the background.'
    TOUCH = True
    PARAMS = (('base', 'Background', 'color', '#080418'),
              ('colors', 'Sparkles', 'colors', ['#ffffff', '#ffd080', '#80c0ff']),
              ('density', 'Density', 'int', 30, 1, 100),
              ('fade', 'Fade (x0.1 s)', 'int', 8, 1, 60))

    def setup(self):
        self.base = parse_color(self.p['base'])
        self.cols = [parse_color(c) for c in self.p['colors']]
        self.lit = {}
        self.first = True

    def _spark(self, out, i, t):
        out[i] = with_t(random.choice(self.cols), 1)
        self.lit[i] = t + 150

    def touch(self, i, t):
        self.lit[i] = -1   # flagged: lit on the next frame

    def frame(self, t):
        if self.first:
            self.first = False
            return [self.base] * self.n
        out = [None] * self.n
        changed = False
        for i in list(self.lit):
            due = self.lit[i]
            if due < 0:
                self._spark(out, i, t)
                changed = True
            elif t >= due:
                out[i] = with_t(self.base, self.p['fade'])
                del self.lit[i]
                changed = True
        p = self.p['density'] * 0.00025    # chance per panel and frame
        for i in range(self.n):
            if i not in self.lit and random.random() < p:
                self._spark(out, i, t)
                changed = True
        return out if changed else None


class Fire(Effect):
    LABEL = 'Fire'
    DESC = 'Flickering flames, hottest at the bottom of the layout.'
    PARAMS = (('colors', 'Palette', 'colors',
               ['#000000', '#600000', '#e02000', '#ff7000', '#ffc040']),
              ('intensity', 'Intensity', 'int', 60, 10, 100),
              ('speed', 'Flicker', 'int', 50, 1, 100))

    def setup(self):
        self.pal = Palette(self.p['colors'], wrap=False)
        self.heat = [0.3] * self.n
        self.bias = [1 - v for v in self.ctx.y]
        self.interval = 160 - self.p['speed']

    def frame(self, t):
        it = self.p['intensity'] / 100
        sp = self.p['speed'] / 100
        rnd = random.random
        out = []
        for i in range(self.n):
            target = it * (0.35 + 0.65 * self.bias[i]) * (0.55 + 0.6 * rnd())
            h = self.heat[i] * 0.55 + target * 0.45
            if rnd() < 0.08 + 0.25 * sp:
                h += 0.25 * rnd() * it
            h = min(1.0, max(0.0, h))
            self.heat[i] = h
            out.append(with_t(self.pal.at(h), 2))
        return out


class Ripple(Effect):
    LABEL = 'Ripple'
    DESC = 'Waves spread from panel to panel. Touch a panel to start one there.'
    TOUCH = True
    PARAMS = (('base', 'Background', 'color', '#001020'),
              ('colors', 'Wave colours', 'colors', ['#00e0ff', '#ff40c0', '#ffd000']),
              ('speed', 'Speed', 'int', 40, 5, 100),
              ('width', 'Width (panels)', 'int', 2, 1, 6),
              ('auto', 'Auto every (s), 0 = touch only', 'int', 3, 0, 60))
    INTERVAL = 40

    def setup(self):
        self.base = parse_color(self.p['base'])
        self.cols = [parse_color(c) for c in self.p['colors']]
        self.waves = []
        self.ci = 0
        self.next = 0 if self.p['auto'] else None
        self.idle = False

    def touch(self, i, t):
        self._add(i, t)

    def _add(self, i, t):
        h = self.ctx.hops(i)
        self.waves.append((h, max(h), t, self.cols[self.ci % len(self.cols)]))
        self.ci += 1
        if len(self.waves) > 6:
            self.waves.pop(0)

    def frame(self, t):
        if self.next is not None and t >= self.next:
            self._add(random.randrange(self.n), t)
            self.next = t + self.p['auto'] * 1000
        if not self.waves:
            if self.idle:
                return None
            self.idle = True
            return [self.base] * self.n
        self.idle = False
        hps = self.p['speed'] / 10
        w = self.p['width']
        out = [self.base] * self.n
        alive = []
        for wave in self.waves:
            hops, far, t0, c = wave
            front = (t - t0) * 0.001 * hps
            if front - w > far:
                continue
            alive.append(wave)
            for i in range(self.n):
                d = front - hops[i]
                if -w < d < w:
                    out[i] = mix(out[i], c, 1 - abs(d) / w)
        self.waves = alive
        return out


class Wander(Effect):
    LABEL = 'Wander'
    DESC = 'Lights roam from neighbour to neighbour and leave fading trails.'
    TOUCH = True
    PARAMS = (('base', 'Background', 'color', '#000000'),
              ('colors', 'Colours', 'colors', ['#ff2a6d', '#05d9e8', '#d1f7ff']),
              ('count', 'Walkers', 'int', 2, 1, 8),
              ('speed', 'Steps per second', 'int', 4, 1, 20),
              ('trail', 'Trail (x0.1 s)', 'int', 10, 1, 60))

    def setup(self):
        self.base = parse_color(self.p['base'])
        self.cols = [parse_color(c) for c in self.p['colors']]
        self.walkers = []
        for k in range(min(self.p['count'], self.n)):
            self.walkers.append([random.randrange(self.n), -1, k % len(self.cols)])
        self.next = None

    def touch(self, i, t):
        if self.walkers:
            w = self.walkers[0]
            self.walkers.append([i, -1, (w[2] + len(self.walkers)) % len(self.cols)])
            if len(self.walkers) > self.p['count'] + 4:
                self.walkers.pop(1)

    def frame(self, t):
        if self.next is None:
            self.next = t
            out = [self.base] * self.n
            for w in self.walkers:
                out[w[0]] = self.cols[w[2]]
            return out
        if t < self.next:
            return None
        self.next = t + 1000 // self.p['speed']
        out = [None] * self.n
        for w in self.walkers:
            nb = self.ctx.adj[w[0]] or [w[0]]
            ch = [j for j in nb if j != w[1]] or nb
            out[w[0]] = with_t(self.base, self.p['trail'])
            w[1], w[0] = w[0], random.choice(ch)
        for w in self.walkers:
            out[w[0]] = with_t(self.cols[w[2]], 1)
        return out


class Plasma(Effect):
    LABEL = 'Plasma'
    DESC = 'Liquid, slowly morphing colour fields.'
    PARAMS = (('colors', 'Palette', 'colors',
               ['#001848', '#0060a0', '#00c0c0', '#40ffd0', '#0090ff']),
              ('speed', 'Speed', 'int', 30, 1, 100),
              ('scale', 'Scale', 'int', 50, 10, 100))

    def setup(self):
        self.pal = Palette(self.p['colors'])
        k = self.p['scale'] / 10
        self.px = [v * k for v in self.ctx.ix]
        self.py = [v * k for v in self.ctx.iy]

    def frame(self, t):
        s = t * 0.001 * self.p['speed'] / 25
        sin = math.sin
        out = []
        for i in range(self.n):
            x, y = self.px[i], self.py[i]
            v = sin(x + s) + sin(y * 1.3 - s * 0.7) + sin((x + y) * 0.7 + s * 1.3)
            out.append(self.pal.at((v + 3) / 6))
        return out


class Sunrise(Effect):
    LABEL = 'Sunrise'
    DESC = 'Dark to deep red to orange to warm white over many minutes. Pair it with a schedule.'
    PARAMS = (('minutes', 'Duration (min)', 'int', 20, 1, 180),
              ('color', 'Final colour', 'color', '#ffa050ff'))
    INTERVAL = 1000

    def setup(self):
        self.stops = [BLACK, (40, 0, 0, 0), (180, 25, 0, 0), (255, 90, 10, 20),
                      parse_color(self.p['color'])]
        self.done = False

    def frame(self, t):
        if self.done:
            return None
        p = t / (self.p['minutes'] * 60000)
        if p >= 1.25:
            self.done = True
        out = []
        m = len(self.stops) - 1
        for i in range(self.n):
            # the bottom of the layout rises first
            q = min(1.0, max(0.0, p * 1.25 - 0.25 * self.ctx.y[i]))
            f = q * m
            k = min(int(f), m - 1)
            out.append(with_t(mix(self.stops[k], self.stops[k + 1], f - k), 12))
        return out


EFFECTS = (('solid', Solid), ('scene', Scene), ('rainbow', Rainbow), ('flow', Flow),
           ('plasma', Plasma), ('cycle', Cycle), ('mosaic', Mosaic),
           ('breathe', Breathe), ('twinkle', Twinkle), ('fire', Fire),
           ('ripple', Ripple), ('wander', Wander), ('sunrise', Sunrise))
REGISTRY = dict(EFFECTS)


def catalogue():
    out = []
    for name, cls in EFFECTS:
        d = schema(cls)
        d['name'] = name
        out.append(d)
    return out


# --- engine -------------------------------------------------------------------

class Engine:
    """Runs one effect: timing, crossfades, dark panels, de-duplication and
    the periodic refresh of static frames."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.name = None
        self.params = {}
        self.eff = None
        self.t0 = 0
        self.next = 0
        self.fade = 0
        self.last = None
        self.last_push = 0
        self.force = False

    def set(self, name, params, now, fade=5, keep_time=False):
        cls = REGISTRY.get(name)
        if cls is None:
            raise ValueError('unknown effect %r' % name)
        self.params = clean(cls, params, self.ctx.n)
        self.name = name
        if not keep_time:
            self.t0 = now
        self.eff = cls(self.ctx, self.params)
        self.next = now
        self.fade = fade
        self.last = None

    def restart(self, now, fade=5, ctx=None):
        """Same effect again, e.g. after power-on or a new layout."""
        if ctx is not None:
            self.ctx = ctx
        if self.name:
            self.set(self.name, self.params, now, fade)

    def resume(self, now, fade=5):
        """Paint the current frame again after something else used the
        panels (a stream, identify), without restarting the effect."""
        self.force = True
        self.fade = fade
        self.next = now

    def touch(self, i, now):
        if self.eff and 0 <= i < self.ctx.n:
            self.eff.touch(i, time.ticks_diff(now, self.t0))
            self.next = now

    def tick(self, now):
        """The frame to push now, or None."""
        if self.eff is None or time.ticks_diff(now, self.next) < 0:
            return None
        self.next = time.ticks_add(now, self.eff.interval)
        fr = self.eff.frame(time.ticks_diff(now, self.t0))
        refresh = self.last is not None and (
            self.force or time.ticks_diff(now, self.last_push) >= REFRESH_MS)
        self.force = False
        if fr is None:
            if not refresh:
                return None
            fr = self.last
        n = self.ctx.n
        on = self.ctx.on
        fade = self.fade
        self.fade = 0
        out = []
        last = self.last or [BLACK] * n
        merged = list(last)
        same = self.last is not None
        for i in range(n):
            c = fr[i] if i < len(fr) else None
            if not on[i]:
                c = BLACK if (c is not None or self.last is None) else None
            if c is None:
                out.append(None)
                continue
            if len(c) < 5:
                c = (c[0], c[1], c[2], c[3] if len(c) > 3 else 0, 1)
            if fade and c[4] < fade:
                c = (c[0], c[1], c[2], c[3], fade)
            if same and c[:4] != last[i][:4]:
                same = False
            merged[i] = c
            out.append(c)
        if same and not refresh:
            return None
        self.last = merged
        self.last_push = now
        return out
