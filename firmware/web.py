"""HTTP server: the web UI and a JSON API.

Non-blocking: tick() runs in the same loop as the effects and the network
receivers. Every connection is a small state machine (read request, then
send the response a piece at a time), so a slow browser or a 40 kB page
never stalls the lights.

UI
    GET  /                      www/index.html (www/index.html.gz if present)

API (JSON in, JSON out)
    GET  /api/info              firmware, network, uptime, memory, frame rate
    GET  /api/state             power, brightness, effect, colours, touch
    POST /api/state             {power, brightness, effect, params, preset, timer}
    GET  /api/effects           effect catalogue with parameter descriptions
    GET  /api/presets           saved presets
    POST /api/presets           {name[, effect, params]} save (current if omitted)
    POST /api/presets/delete    {name}
    POST /api/presets/order     {names: [...]}
    GET  /api/layout            coordinates, tree, neighbours, view settings
    POST /api/layout            {rotation, mirror, names, disabled}
    POST /api/layout/rescan     enumerate the bus again
    POST /api/identify          {chunk[, ms]} flash one panel
    POST /api/touch             {chunk[, hold]} act as if a panel was touched
    GET  /api/settings          configuration (Wi-Fi key masked)
    POST /api/settings          partial configuration
    GET  /api/wifi/scan         visible networks
    POST /api/reboot

Kept from the first version, for tools/leafbus.py and scripts:
    GET /layout  /state  /identify/N  /brightness/N  /off
"""
import io
import json
import os
import socket
import time

WWW = 'www'
MAX_CONN = 5       # accepted at once; more wait in the backlog
MAX_HEAD = 4096
MAX_BODY = 16384
CONN_TIMEOUT_MS = 8000      # reading a request, or sending a response
QUEUE_TIMEOUT_MS = 15000    # a read request waiting for its turn
CHUNK = 1460                # one TCP segment
SENDS_PER_PASS = 4          # ~TCP_SND_BUF per loop pass; stops early when lwIP is full
TRACE = None          # debugging: callable(event) called at each step of a request
WOULD_BLOCK = (11, 110, 115, 10035)   # EAGAIN, ETIMEDOUT, EINPROGRESS, WSAEWOULDBLOCK

MIME = {'html': 'text/html; charset=utf-8', 'js': 'application/javascript',
        'css': 'text/css', 'svg': 'image/svg+xml', 'json': 'application/json',
        'png': 'image/png', 'ico': 'image/x-icon', 'txt': 'text/plain'}
REASON = {200: 'OK', 204: 'No Content', 400: 'Bad Request', 404: 'Not Found',
          405: 'Method Not Allowed', 413: 'Payload Too Large',
          500: 'Internal Server Error', 503: 'Service Unavailable'}


class _Chunks(io.IOBase):
    """Stream that keeps what is written as a list of small bytes objects.

    Fallback for a fragmented heap (modules loaded from the filesystem
    instead of frozen into the firmware): a JSON reply built as one string
    needs one large block; MicroPython then grows its heap by taking the
    largest free block of the ESP-IDF heap, leaving Wi-Fi and TCP without
    memory. json.dump() into this stream allocates PART bytes at a time,
    but is slow: one Python call per JSON token."""
    PART = 1024

    def __init__(self):
        self.parts = []
        self.cur = bytearray()
        self.size = 0

    def write(self, b):
        if isinstance(b, str):        # CPython's json writes str
            b = b.encode()
        self.cur += b
        self.size += len(b)
        if len(self.cur) >= self.PART:
            self.parts.append(bytes(self.cur))
            self.cur = bytearray()
        return len(b)

    def done(self):
        if self.cur:
            self.parts.append(bytes(self.cur))
            self.cur = bytearray()
        return self.parts


def _readinto(sock, buf):
    """MicroPython sockets have readinto(), CPython ones recv_into()."""
    f = getattr(sock, 'readinto', None)
    if f is not None:
        return f(buf)
    return sock.recv_into(buf)


def json_parts(obj):
    """(list of small bytes, total length) of obj as JSON."""
    w = _Chunks()
    json.dump(obj, w)
    return w.done(), w.size


def _blocked(e):
    return e.args and e.args[0] in WOULD_BLOCK


def unquote(s):
    s = s.replace('+', ' ')
    if '%' not in s:
        return s
    out = bytearray()
    parts = s.split('%')
    out += parts[0].encode()
    for p in parts[1:]:
        try:
            out.append(int(p[:2], 16))
            out += p[2:].encode()
        except ValueError:
            out += ('%' + p).encode()
    return out.decode()


class Request:
    def __init__(self, method, path, query, headers, body):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    def json(self):
        if not self.body:
            return {}
        try:
            v = json.loads(self.body.decode())
        except ValueError:
            raise ValueError('body is not valid JSON')
        if not isinstance(v, dict):
            raise ValueError('body must be a JSON object')
        return v


def parse_request(buf):
    """Request, or None while incomplete. ValueError on garbage."""
    i = buf.find(b'\r\n\r\n')
    if i < 0:
        if len(buf) > MAX_HEAD:
            raise ValueError('header too large')
        return None
    lines = buf[:i].decode().split('\r\n')
    first = lines[0].split(' ')
    if len(first) < 2:
        raise ValueError('bad request line')
    headers = {}
    for ln in lines[1:]:
        kv = ln.split(':', 1)
        if len(kv) == 2:
            headers[kv[0].strip().lower()] = kv[1].strip()
    n = int(headers.get('content-length', '0') or 0)
    if n > MAX_BODY:
        raise ValueError('body too large')
    body = buf[i + 4:i + 4 + n]
    if len(body) < n:
        return None
    target = first[1]
    query = {}
    if '?' in target:
        target, qs = target.split('?', 1)
        for kv in qs.split('&'):
            if kv:
                kv = kv.split('=', 1)
                query[unquote(kv[0])] = unquote(kv[1]) if len(kv) > 1 else ''
    return Request(first[0].upper(), unquote(target), query, headers, body)


class Raw:
    """A ready-made body, e.g. JSON encoded once and cached."""

    def __init__(self, data, ctype='application/json'):
        self.data = data
        self.ctype = ctype


class StaticFile:
    def __init__(self, path, gz=False):
        self.path = path
        self.gz = gz


def static(path, accept=''):
    """StaticFile for a URL path under WWW, or None."""
    if path.endswith('/'):
        path += 'index.html'
    if '..' in path:
        return None
    full = WWW + path
    if 'gzip' in accept:
        try:
            os.stat(full + '.gz')
            return StaticFile(full + '.gz', True)
        except OSError:
            pass
    try:
        if os.stat(full)[0] & 0x4000:   # directory
            return None
        return StaticFile(full)
    except OSError:
        return None


class _Conn:
    def __init__(self, sock, now):
        self.sock = sock
        self.t0 = now
        self.inb = b''
        self.req = None      # parsed request, waiting for its turn
        self.out = None      # list of bytes / open files once responding
        self.pend = None     # memoryview still to send


class WebServer:
    """handler(Request) -> (code, body) where body is a dict or list (JSON),
    str/bytes, a StaticFile or None."""

    def __init__(self, handler, port=80):
        self.handler = handler
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', port))
        self.sock.listen(4)
        self.sock.settimeout(0)
        self.conns = []
        self.active = None   # the one connection currently sending
        self.buf = bytearray(CHUNK)   # file reads go here: no allocation per chunk
        self.rbuf = bytearray(512)    # request reads go here
        self.requests = 0
        # counters for /api/info and the debug line on the console
        self.stats = {'accepted': 0, 'accept_errors': 0, 'last_accept_error': None,
                      'evicted': 0, 'timeouts': 0, 'resets': 0}

    def busy(self):
        return bool(self.conns)

    def full(self):
        return len(self.conns) >= MAX_CONN

    def accept(self):
        """Call when the listening socket is readable. At MAX_CONN nothing is
        accepted: the connection waits in the backlog until a slot frees up
        (evicting one would show up as an error in the browser)."""
        if self.full():
            return
        try:
            s, _ = self.sock.accept()
        except OSError as e:
            self.stats['accept_errors'] += 1
            self.stats['last_accept_error'] = e.args[0] if e.args else None
            return
        self.stats['accepted'] += 1
        if TRACE:
            TRACE('accept')
        try:
            s.setblocking(False)
        except Exception:
            s.settimeout(0)
        try:
            # replies go out in pieces; Nagle plus the client's delayed ACK
            # would hold each piece back by up to 200 ms
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (AttributeError, OSError):
            pass
        self.conns.append(_Conn(s, time.ticks_ms()))

    def tick(self):
        """Accept and serve (for loops that do not poll the listener)."""
        self.accept()
        self.service()

    def service(self):
        """Move every open connection one step forward.

        Requests are read on all connections, but only one connection at a
        time sends its response: on an ESP32 the TCP stack has some 20 kB of
        buffers once Wi-Fi is up, and a browser opening four connections at
        once used to exhaust them, stalling the stack for minutes."""
        now = time.ticks_ms()
        for c in list(self.conns):
            if c is self.active or c.req is not None:
                if c is not self.active and time.ticks_diff(now, c.t0) > QUEUE_TIMEOUT_MS:
                    self.stats['timeouts'] += 1
                    self._close(c)
                continue
            try:
                self._read(c, now)
            except Exception as e:
                self._fail(c, e)
        if self.active is None:
            for c in self.conns:
                if c.req is not None:
                    self.active = c
                    c.t0 = now
                    if TRACE:
                        TRACE('build %s' % (c.req.path if isinstance(c.req, Request) else '400'))
                    c.out = self._respond(c.req) if isinstance(c.req, Request) else c.req
                    if TRACE:
                        TRACE('built %d parts' % len(c.out))
                    c.req = True
                    break
        c = self.active
        if c is not None:
            if time.ticks_diff(now, c.t0) > CONN_TIMEOUT_MS:
                self.stats['timeouts'] += 1
                self._close(c)
                return
            try:
                self._send(c)
            except Exception as e:
                self._fail(c, e)

    def _fail(self, c, e):
        if not isinstance(e, OSError):
            print('web:', repr(e))
        self.stats['resets'] += 1
        self._close(c)

    def _close(self, c):
        if c in self.conns:
            self.conns.remove(c)
        if c is self.active:
            self.active = None
        for item in c.out or ():
            if not isinstance(item, (bytes, bytearray)):
                try:
                    item.close()
                except Exception:
                    pass
        c.out = None
        try:
            c.sock.close()
        except Exception:
            pass

    def _read(self, c, now):
        if time.ticks_diff(now, c.t0) > CONN_TIMEOUT_MS:
            self.stats['timeouts'] += 1
            self._close(c)
            return
        try:
            # into the buffer allocated at start-up: a fresh 1 kB buffer per
            # recv() is exactly the kind of block the fragmented heap lacks
            n = _readinto(c.sock, self.rbuf)
        except OSError as e:
            if _blocked(e):
                return
            raise
        if n is None:            # MicroPython: nothing there yet
            return
        if not n:
            self._close(c)
            return
        c.inb += self.rbuf[:n]
        try:
            req = parse_request(c.inb)
        except ValueError as e:
            c.req = self._build(400, {'error': str(e)})   # a ready response
            c.inb = b''
            return
        if req is None:
            return
        c.inb = b''
        c.t0 = now                # waiting in line from now
        self.requests += 1
        c.req = req

    def _send(self, c):
        """Up to SENDS_PER_PASS pieces of at most CHUNK bytes. Never one large
        write: lwIP would allocate buffers for all of it at once."""
        sent = 0
        while sent < SENDS_PER_PASS:
            if c.pend is None:
                if not c.out:
                    self._close(c)
                    return
                item = c.out[0]
                if isinstance(item, (bytes, bytearray)):
                    c.out.pop(0)
                    data = item
                else:
                    n = item.readinto(self.buf)
                    if not n:
                        item.close()
                        c.out.pop(0)
                        continue
                    data = memoryview(self.buf)[:n]
                c.pend = memoryview(data)
            try:
                # never more than CHUNK per call: one large write makes lwIP
                # grab buffers for all of it at once, which the ESP32 lacks
                k = c.sock.send(c.pend[:CHUNK])
            except OSError as e:
                if _blocked(e):
                    if TRACE:
                        TRACE('send blocked')
                    return
                raise
            if not k:
                return
            c.pend = c.pend[k:] if k < len(c.pend) else None
            sent += 1
            if TRACE:
                TRACE('sent %d' % k)

    def _respond(self, req):
        if req.method == 'OPTIONS':
            return self._build(204, None)
        try:
            r = self.handler(req)
        except ValueError as e:
            r = (400, {'error': str(e)})
        except Exception as e:
            print('web: %s %s: %r' % (req.method, req.path, e))
            r = (500, {'error': str(e)})
        return self._build(r[0], r[1])

    def _build(self, code, body):
        extra = ''
        if isinstance(body, StaticFile):
            ext = body.path.rsplit('.', 2)
            ext = ext[-2] if body.gz else ext[-1]
            ctype = MIME.get(ext, 'application/octet-stream')
            size = os.stat(body.path)[6]
            f = open(body.path, 'rb')
            if body.gz:
                extra = 'Content-Encoding: gzip\r\n'
            parts = [f]
        elif isinstance(body, (dict, list)):
            try:
                data = json.dumps(body).encode()
                parts, size = [data], len(data)
            except MemoryError:   # fragmented heap: build it in small pieces
                parts, size = json_parts(body)
            ctype = 'application/json'
        elif isinstance(body, Raw):
            parts, size, ctype = [body.data], len(body.data), body.ctype
        else:
            if body is None:
                data = b''
                ctype = 'text/plain'
            elif isinstance(body, str):
                data = body.encode()
                ctype = 'text/plain; charset=utf-8'
            else:
                data = bytes(body)
                ctype = 'application/octet-stream'
            size = len(data)
            parts = [data] if data else []
        head = ('HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n'
                '%sCache-Control: no-cache\r\nConnection: close\r\n'
                'Access-Control-Allow-Origin: *\r\n'
                'Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n'
                'Access-Control-Allow-Headers: Content-Type\r\n\r\n'
                % (code, REASON.get(code, 'OK'), ctype, size, extra))
        return [head.encode()] + parts


# --- routes ---------------------------------------------------------------------

def _chunk_arg(app, v):
    try:
        i = int(v)
    except (TypeError, ValueError):
        i = -1
    if not 0 <= i < app.bus.npanels:
        raise ValueError('chunk must be in 0..%d' % (app.bus.npanels - 1))
    return i


def make_handler(app):
    """Route table bound to an app.App."""

    def handle(req):
        p = req.path
        if len(p) > 1 and p.endswith('/'):
            p = p[:-1]
        get = req.method == 'GET'
        post = req.method == 'POST'

        if p.startswith('/api/'):
            if p == '/api/state':
                if post:
                    return 200, app.update_state(req.json())
                return 200, app.state_json()
            if p == '/api/info':
                return 200, app.info_json()
            if p == '/api/effects':
                return 200, Raw(app.effects_bytes())
            if p == '/api/presets':
                if post:
                    b = req.json()
                    return 200, app.save_preset(b.get('name'), b.get('effect'),
                                                b.get('params'))
                return 200, app.presets
            if p == '/api/presets/delete' and post:
                return 200, app.delete_preset(req.json().get('name'))
            if p == '/api/presets/order' and post:
                return 200, app.order_presets(req.json().get('names'))
            if p == '/api/layout':
                if post:
                    return 200, app.update_layout(req.json())
                return 200, app.layout_json()
            if p == '/api/layout/rescan' and post:
                app.rescan()
                return 200, app.layout_json()
            if p == '/api/identify' and post:
                b = req.json()
                return 200, app.identify(_chunk_arg(app, b.get('chunk')), b.get('ms', 3000))
            if p == '/api/touch' and post:
                b = req.json()
                return 200, app.touch_event(_chunk_arg(app, b.get('chunk')), bool(b.get('hold')))
            if p == '/api/settings':
                if post:
                    return 200, app.update_settings(req.json())
                return 200, app.settings_json()
            if p == '/api/wifi/scan':
                return 200, app.wifi_scan()
            if p == '/api/reboot' and post:
                return 200, app.reboot()
            return 404, {'error': 'no such endpoint: %s %s' % (req.method, p)}

        # first-version endpoints
        if p == '/layout':
            return 200, app.layout_json()
        if p == '/state':
            return 200, app.legacy_state()
        if p.startswith('/identify/'):
            return 200, app.identify(_chunk_arg(app, p[10:]), 3000)
        if p.startswith('/brightness/'):
            seg = p[12:]
            if not seg.isdigit():
                raise ValueError('/brightness/N with N in 0..255')
            return 200, app.update_state({'brightness': int(seg)})
        if p == '/off':
            return 200, app.update_state({'power': False})

        if get:
            f = static(p, req.headers.get('accept-encoding', ''))
            if f:
                return 200, f
            if p == '/':
                return 200, ('LeafBus: the web UI is missing. Copy firmware/www/ to the '
                             'board (see README). The API is at /api/state.')
        return 404, {'error': 'not found: %s' % p}

    return handle
