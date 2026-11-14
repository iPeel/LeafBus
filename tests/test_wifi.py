"""wifi.py on a firmware without SoftAP: the access point fails to open and
must not be retried on every tick."""
import time

import config
import wifi


class NoAP:
    """network.WLAN(AP_IF) of a firmware built without SoftAP support."""

    def __init__(self):
        self.tries = 0

    def active(self, v=None):
        if v:
            self.tries += 1
            raise OSError(258)   # ESP_ERR_INVALID_ARG from esp_wifi_set_mode
        return False

    def config(self, *a, **k):
        raise OSError(258)


def test_failed_access_point_is_retried_once_a_minute(monkeypatch, capsys):
    cfg = config.load('missing.json')        # no Wi-Fi configured: wants the AP
    w = wifi.WiFi(cfg)
    w.ap = ap = NoAP()
    w.ap_on = False
    w.ap_failed = None
    for _ in range(20):
        w.start_ap()
        w.tick(time.ticks_ms())
    assert ap.tries == 1 and not w.ap_on
    assert capsys.readouterr().out.count('access point failed') == 1
    w.ap_failed = time.ticks_add(time.ticks_ms(), -wifi.RETRY_MS - 1)
    w.start_ap()
    assert ap.tries == 2
