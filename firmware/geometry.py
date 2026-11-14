"""Layout string -> assembly tree, chunk order and panel coordinates.

Layer 2 of the firmware: pure functions over bytes, no hardware access. Runs
unchanged on the host (tests, tools) and in MicroPython. Grammar, subtype
encoding and the geometry formulas are specified in PROTOCOL.md §7.

Coordinates are millimetres as in the Nanoleaf API: x right, y up,
orientation in degrees counter-clockwise. The root panel is at (0, 0)
with orientation 0.
"""
import math

TERMINATOR = 0x40
PSU_CODE = 1

# connector-count code (bits 6..4 of the node byte) -> connectors (§7.1)
_CONNECTORS = {0: 8, 1: 0, 7: 9}

# shape ids as in the Nanoleaf API
HEXAGON = 7
TRIANGLE = 8
MINI_TRIANGLE = 9
PSU = 'psu'

SIDE = {HEXAGON: 67.0, TRIANGLE: 134.0, MINI_TRIANGLE: 67.0}
SHAPE_NAMES = {HEXAGON: 'hexagon', TRIANGLE: 'triangle',
               MINI_TRIANGLE: 'mini-triangle', PSU: 'psu'}


def connectors(code):
    return _CONNECTORS.get(code, code)


def subtype(edges, sep):
    """Panel subtype from the separator bytes (§7.3)."""
    if edges == 3:
        sub = (sep[0] >> 2) & 7 if sep else 0
        return MINI_TRIANGLE if sub < 2 else None
    if edges == 6:
        sub = sep[1] & 7 if len(sep) > 1 else 0
        return {1: HEXAGON, 2: TRIANGLE}.get(sub)
    return None


def parse(raw):
    """Parse a layout string into a tree (§7.2).

    Node: {'hdr', 'edges', 'root_edge', 'shape', 'sep', 'parent_edge',
           'children': {connector: node}, 'index'}
    'index' is the panel's position in the string; the power supply has none.
    A root connector outside 0..edges-1 (corrupted byte, §9.2) is stored as
    None and resolved later by place().

    No function here recurses: a chain of panels nests one level per panel,
    and MicroPython on an ESP32 runs out of C stack a few tens of levels
    deep, which resets the board without a traceback.
    """
    body = bytes(raw)
    if TERMINATOR in body:
        body = body[:body.index(TERMINATOR)]
    if not body or not (body[0] & 0x80):
        raise ValueError('invalid first byte in layout string')
    count = 0

    def new(hdr, parent_edge):
        n = {'hdr': hdr, 'root_edge': hdr & 7, 'parent_edge': parent_edge,
             'sep': [], 'children': {}}
        code = (hdr >> 4) & 7
        if code == PSU_CODE:
            n['edges'] = 1
            n['shape'] = PSU
            return n, None
        edges = connectors(code)
        n['edges'] = edges
        root = n['root_edge']
        if root >= edges:
            n['root_edge'] = None
            e = 0
        else:
            e = (root + 1) % edges
        return n, [n, e, 0]        # open node: [node, current connector, separators]

    def close(n):
        n['shape'] = subtype(n['edges'], n['sep'])

    root, top = new(body[0], None)
    if top:
        root['index'] = count
        count += 1
    stack = [top] if top else []
    i = 1
    while stack and i < len(body):
        top = stack[-1]
        n = top[0]
        b = body[i]
        if b & 0x80:
            child, frame = new(b, top[1])
            n['children'][top[1]] = child
            i += 1
            if frame:
                child['index'] = count
                count += 1
                stack.append(frame)
        else:
            top[2] += 1
            if top[2] == n['edges'] - 1:
                close(n)           # the byte belongs to the parent
                stack.pop()
                continue
            n['sep'].append(b)
            top[1] = (top[1] + 1) % n['edges']
            i += 1
    while stack:                   # string ended inside open nodes
        close(stack.pop()[0])
    root['npanels'] = count
    if i != len(body):
        root['unparsed'] = body[i:]
    return root


def walk(root, sort=False):
    """Every node of the tree in string order (depth first, pre-order), as
    (node, parent, connector on the parent, depth). Iterative."""
    out = []
    stack = [(root, None, None, 0)]
    while stack:
        item = stack.pop()
        out.append(item)
        n, d = item[0], item[3]
        kids = n['children']
        keys = sorted(kids) if sort else list(kids)
        for e in reversed(keys):
            stack.append((kids[e], n, e, d + 1))
    return out


# --- geometry (§7.6) --------------------------------------------------------

def rot(x, y, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c - y * s, x * s + y * c


def side_of(shape, e):
    """Connector -> geometric side. A Triangle has two connectors per side."""
    return ((e + 1) >> 1) % 3 if shape == TRIANGLE else e


def edge_angle(shape, e):
    """Angle of the side carrying connector e. Connectors are numbered
    clockwise, so the angle grows with e while rotations stay CCW."""
    step = 60 if shape == HEXAGON else 120
    return step * side_of(shape, e)


def edge_vec(shape, e):
    """Vector from the panel centre to connector e, panel frame."""
    s = SIDE[shape]
    if shape == HEXAGON:
        return rot(0, -s * math.sqrt(3) / 2, -edge_angle(shape, e))
    r = s / (2 * math.sqrt(3))
    x = 0.0
    if shape == TRIANGLE:
        x = -s / 4 if e in (0, 2, 4) else s / 4
    return rot(x, -r, -edge_angle(shape, e))


def _attach(p, pe, c, ce):
    """Place child c with its connector ce on connector pe of parent p."""
    delta = (180 + edge_angle(c['shape'], ce) - edge_angle(p['shape'], pe)) % 360
    ax, ay = edge_vec(p['shape'], pe)
    bx, by = rot(*edge_vec(c['shape'], ce), delta)
    ox, oy = rot(ax - bx, ay - by, p['o'])
    c['x'], c['y'] = p['x'] + ox, p['y'] + oy
    c['o'] = (p['o'] + delta) % 360


def place(root):
    """Assign x, y (mm) and o (degrees) to every node.

    Nodes whose root connector is unknown are placed in a second pass: every
    connector is tried and the one that puts the panel farthest from the
    panels already placed wins (the others overlap the assembly).
    """
    root['x'] = root['y'] = root['o'] = 0.0
    placed = []
    deferred = []

    def walk(top):
        # pre-order like the recursive form: a child is placed (or deferred)
        # when it is reached, after the subtrees of its earlier siblings
        stack = [(None, None, top)]
        while stack:
            p, pe, c = stack.pop()
            if p is not None:
                if c['shape'] == PSU or p['shape'] is None or c['shape'] is None:
                    c['x'], c['y'], c['o'] = p['x'], p['y'], p['o']
                elif c['root_edge'] is None:
                    deferred.append((p, pe, c))
                    continue
                else:
                    _attach(p, pe, c, c['root_edge'])
            placed.append(c)
            kids = list(c['children'].items())
            for e, k in reversed(kids):
                stack.append((c, e, k))
    walk(root)

    for p, pe, c in deferred:
        best = None
        for ce in range(c['edges']):
            _attach(p, pe, c, ce)
            others = [q for q in placed if q is not p and q['shape'] != PSU]
            d = min(math.sqrt((c['x'] - q['x']) ** 2 + (c['y'] - q['y']) ** 2)
                    for q in others) if others else 0
            if best is None or d > best[0] + 1e-6:
                best = (d, ce)
        c['guessed_edge'] = best[1]
        _attach(p, pe, c, best[1])
        walk(c)
    return root


def panels(root):
    """Flat list of panels (no PSU) in string order."""
    return [n for n, _, _, _ in walk(root, sort=True) if n['shape'] != PSU]


def _connector_xy(p, e):
    """World position of connector e of a placed panel, rounded."""
    if p['shape'] not in SIDE:  # unknown subtype: no connector geometry
        return [round(p['x'], 2), round(p['y'], 2)]
    x, y = rot(*edge_vec(p['shape'], e), p['o'])
    return [round(p['x'] + x, 2), round(p['y'] + y, 2)]


def layout(raw):
    """Everything derived from one layout string:

    {'psu': {'chunk', 'edge', 'x', 'y'} or None,
     'positions': [{'chunk', 'x', 'y', 'o', 'shape', 'side', 'link'[, 'guessed']}, ...]
                  sorted by chunk, like positionData in the Nanoleaf API;
                  'link' is the connector through which the panel is reached
                  (on the master panel: where the bus master is attached),
     'tree': the parsed tree with coordinates}

    chunk = npanels - 1 - index: the panel next to the master is the first
    node of the string and gets the last chunk (§5.3).
    """
    root = place(parse(raw))
    n = root['npanels']
    ps = panels(root)
    for p in ps:
        p['chunk'] = n - 1 - p['index']
    psu = None
    for c, node, e, _ in walk(root):
        if c['shape'] == PSU and node is not None:
            psu = {'chunk': node.get('chunk'), 'edge': e}   # the last one, as before
    if psu:
        host = next(p for p in ps if p['chunk'] == psu['chunk'])
        psu['x'], psu['y'] = _connector_xy(host, psu['edge'])
    positions = []
    for p in sorted(ps, key=lambda p: p['chunk']):
        if 'x' not in p:  # below a panel of unknown shape: no coordinates
            p['x'], p['y'], p['o'] = 0.0, 0.0, 0.0
        edge = p.get('guessed_edge', 0) if p['root_edge'] is None else p['root_edge']
        d = {'chunk': p['chunk'], 'x': round(p['x'], 2), 'y': round(p['y'], 2),
             'o': int(p['o']), 'shape': p['shape'], 'side': SIDE.get(p['shape']),
             'link': _connector_xy(p, edge)}
        if p['root_edge'] is None:
            d['guessed'] = True
        positions.append(d)
    return {'psu': psu, 'positions': positions, 'links': links(root), 'tree': root}


def links(root):
    """Pairs of chunk indices joined directly by a connector (the bus tree
    without the power supply). Neighbours that touch without a link, as in
    rings (§7.7), are not included; see neighbours()."""
    return [[n['chunk'], c['chunk']] for c, n, _, _ in walk(root, sort=True)
            if n is not None and c['shape'] != PSU and 'chunk' in n and 'chunk' in c]


def inradius(shape, side):
    if shape == HEXAGON:
        return side * math.sqrt(3) / 2
    if shape in (TRIANGLE, MINI_TRIANGLE):
        return side / (2 * math.sqrt(3))
    return None


def connector_points(p):
    """World positions of all connectors of a placed panel (positionData
    entry), or [] for unknown shapes."""
    shape = p.get('shape')
    if shape not in SIDE:
        return []
    n = 6 if shape in (HEXAGON, TRIANGLE) else 3
    out = []
    for e in range(n):
        x, y = rot(*edge_vec(shape, e), p.get('o', 0))
        out.append((p['x'] + x, p['y'] + y))
    return out


def neighbours(positions, tree_links=()):
    """Adjacency list by chunk: panels that touch, plus every bus link.

    Two panels touch exactly when they have a connector at the same point:
    every connector sits at the midpoint of a 67 mm lattice edge (§7.6).
    A distance test between centres does not work: a Mini Triangle on a
    Triangle's side sits off-centre, 67 mm away, as far as panels that only
    share a corner."""
    n = len(positions)
    adj = [set() for _ in range(n)]
    for a, b in tree_links:
        if a < n and b < n:
            adj[a].add(b)
            adj[b].add(a)
    # hash connectors into 2 mm cells; coinciding ones (< 1 mm apart, the
    # coordinates are rounded) are in the same or a neighbouring cell.
    # Integers throughout (0.01 mm; one int per cell): on the board a tuple
    # of floats per point made this allocate ~70 kB for 26 panels.
    grid = {}
    px = []
    py = []
    pi = []
    for i in range(n):
        for x, y in connector_points(positions[i]):
            xi = int(round(x * 100))
            yi = int(round(y * 100))
            k = (xi // 200) * 65536 + yi // 200
            cell = grid.get(k)
            if cell is None:
                grid[k] = cell = []
            cell.append(len(px))
            px.append(xi)
            py.append(yi)
            pi.append(i)
    for m in range(len(px)):
        x, y, i = px[m], py[m], pi[m]
        cx, cy = x // 200, y // 200
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx) * 65536 + cy + dy, ()):
                    b = pi[j]
                    if b != i and abs(px[j] - x) < 100 and abs(py[j] - y) < 100:
                        adj[i].add(b)
    return [sorted(s) for s in adj]


def tree_json(root):
    """The parsed tree as a flat JSON list in string order. Each entry names
    its parent (index into the list, -1 for the master panel) and the
    parent's connector it hangs on; a power supply is {'psu': True, ...}.
    Flat on purpose: nested, every panel of a chain adds two levels of
    recursion to the JSON encoder (see parse())."""
    out = []
    at = {}
    for n, p, e, _ in walk(root, sort=True):
        at[id(n)] = len(out)
        d = {'parent': at[id(p)] if p is not None else -1, 'edge': e}
        if n['shape'] == PSU:
            d['psu'] = True
        else:
            d.update({'chunk': n.get('chunk'), 'connectors': n['edges'],
                      'root_edge': n['root_edge'], 'shape': n['shape'],
                      'x': round(n.get('x', 0), 1), 'y': round(n.get('y', 0), 1),
                      'o': int(n.get('o', 0))})
        out.append(d)
    return out


def tree_text(root, indent=0):
    """Human-readable tree, one node per line."""
    lines = []
    for n, p, e, d in walk(root, sort=True):
        pad = '  ' * (indent + d - 1) + '  connector %d -> ' % e if p is not None \
            else '  ' * indent
        if n['shape'] == PSU:
            lines.append(pad + 'PSU\n')
            continue
        r = n['root_edge']
        if r is None:
            r = '?->%s' % n.get('guessed_edge', '?')
        lines.append('%s0x%02x %-13s connectors=%d root=%s chunk=%s pos=(%.1f, %.1f) o=%d\n' % (
            pad, n['hdr'], SHAPE_NAMES.get(n['shape'], '?'), n['edges'], r,
            n.get('chunk'), n.get('x', 0), n.get('y', 0), n.get('o', 0)))
    return ''.join(lines)


def polygon(p):
    """Corner points of a placed panel, for drawing."""
    s = SIDE[p['shape']]
    if p['shape'] == HEXAGON:
        radius, angles = s, range(0, 360, 60)
    else:
        radius, angles = s / math.sqrt(3), (-30, 90, 210)
    return [(p['x'] + x, p['y'] + y) for x, y in
            (rot(radius * math.cos(math.radians(a)),
                 radius * math.sin(math.radians(a)), p['o']) for a in angles)]
