"""Wi-Fi without blocking the lights.

Joins the configured network in the background while effects already run.
If there is no network configured, or it cannot be joined within
JOIN_TIMEOUT_MS, the board opens its own access point (LeafBus-XXXX, key
in config.json 'ap') so the web UI can still be reached at
http://192.168.4.1 to enter the Wi-Fi details. Once the station connection
is up the access point closes again after AP_LINGER_MS, long enough for the
setup page to show the new address.

The hostname is announced over mDNS by MicroPython: http://leafbus.local

A firmware built without SoftAP support (to save memory) cannot open the
access point; the board then keeps retrying the station connection only, so
the Wi-Fi details must be in config.json before it goes on the wall.
"""
import time

import network

JOIN_TIMEOUT_MS = 20000
RETRY_MS = 60000
AP_LINGER_MS = 90000


class WiFi:
    def __init__(self, cfg):
        self.cfg = cfg
        try:
            network.hostname(cfg['hostname'])
        except Exception:
            pass
        self.sta = network.WLAN(network.STA_IF)
        self.ap = network.WLAN(network.AP_IF)
        self.sta.active(True)
        try:
            # no modem sleep: it adds ~100 ms to every packet and drops
            # some; a light controller is mains powered anyway
            self.sta.config(pm=self.sta.PM_NONE)
        except Exception:
            pass
        self.connected = False
        self.ip = None
        self.ap_on = False
        self.ap_failed = None     # when opening the access point last failed
        self.up_since = None
        now = time.ticks_ms()
        self.t_try = now
        self.t_down = now
        try:
            self.ap.active(False)
        except Exception:
            pass
        self.connect()

    def mac(self):
        """Station MAC address, aa:bb:cc:dd:ee:ff: a stable id for the board."""
        try:
            return ':'.join('%02x' % b for b in self.sta.config('mac'))
        except Exception:
            return None

    def ap_ssid(self):
        if self.cfg['ap'].get('ssid'):
            return self.cfg['ap']['ssid']
        try:
            mac = self.sta.config('mac')
            return 'LeafBus-%02X%02X' % (mac[-2], mac[-1])
        except Exception:
            return 'LeafBus'

    def connect(self):
        """(Re)join the configured network; returns at once."""
        self.t_try = time.ticks_ms()
        ssid = self.cfg['wifi'].get('ssid')
        if not ssid:
            self.start_ap()
            return
        try:
            if self.sta.isconnected():
                self.sta.disconnect()
            self.sta.connect(ssid, self.cfg['wifi'].get('key', ''))
        except Exception as e:
            print('wifi: connect failed:', e)

    def start_ap(self):
        if self.ap_on:
            return
        # tick() asks four times a second; after a failure (e.g. a firmware
        # without SoftAP) try again only once a minute
        if (self.ap_failed is not None and
                time.ticks_diff(time.ticks_ms(), self.ap_failed) < RETRY_MS):
            return
        try:
            self.ap.active(True)
            key = self.cfg['ap'].get('key') or ''
            if len(key) >= 8:
                self.ap.config(essid=self.ap_ssid(), password=key,
                               authmode=getattr(network, 'AUTH_WPA2_PSK', 3))
            else:
                self.ap.config(essid=self.ap_ssid(),
                               authmode=getattr(network, 'AUTH_OPEN', 0))
            self.ap_on = True
            self.ap_failed = None
            print('wifi: access point %r at http://%s' % (self.ap_ssid(), self.ap_ip()))
        except Exception as e:
            self.ap_failed = time.ticks_ms()
            try:
                self.ap.active(False)
            except Exception:
                pass
            print('wifi: access point failed:', e)

    def stop_ap(self):
        if self.ap_on:
            try:
                self.ap.active(False)
            except Exception:
                pass
            self.ap_on = False
            print('wifi: access point closed')

    def ap_ip(self):
        try:
            return self.ap.ifconfig()[0] if self.ap_on else None
        except Exception:
            return None

    def ap_clients(self):
        try:
            return len(self.ap.status('stations'))
        except Exception:
            return 0

    def rssi(self):
        try:
            return self.sta.status('rssi') if self.connected else None
        except Exception:
            return None

    def scan(self):
        """Visible networks, strongest first (blocks ~2 s)."""
        seen = {}
        for r in self.sta.scan():
            ssid = r[0].decode('utf-8', 'ignore') if isinstance(r[0], bytes) else r[0]
            if ssid and (ssid not in seen or r[3] > seen[ssid]['rssi']):
                seen[ssid] = {'ssid': ssid, 'rssi': r[3], 'secure': r[4] != 0}
        return sorted(seen.values(), key=lambda x: -x['rssi'])

    def tick(self, now):
        """Returns 'up' or 'down' when the station link changes, else None."""
        ev = None
        up = self.sta.isconnected()
        if up and not self.connected:
            self.connected = True
            self.ip = self.sta.ifconfig()[0]
            self.up_since = now
            ev = 'up'
        elif not up and self.connected:
            self.connected = False
            self.ip = None
            self.t_down = now
            self.t_try = now
            ev = 'down'
        if up:
            if (self.ap_on and time.ticks_diff(now, self.up_since) > AP_LINGER_MS
                    and not self.cfg['ap'].get('always')):
                self.stop_ap()
        else:
            since = time.ticks_diff(now, self.t_down)
            if since > JOIN_TIMEOUT_MS or not self.cfg['wifi'].get('ssid'):
                self.start_ap()
            # rescanning disturbs the access point: retry only when nobody
            # is using it
            if (self.cfg['wifi'].get('ssid') and
                    time.ticks_diff(now, self.t_try) > RETRY_MS and
                    not self.ap_clients()):
                self.connect()
        return ev
