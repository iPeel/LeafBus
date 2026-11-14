"""Constants for the LeafBus integration."""
from datetime import timedelta

DOMAIN = "leafbus"
DEFAULT_HOST = "leafbus.local"

SCAN_INTERVAL = timedelta(seconds=5)
PRESETS_EVERY = 12          # refresh the preset list every 12th poll (1 min)
REQUEST_TIMEOUT = 10

# sleep timer choices: option key -> minutes (0 = no timer)
TIMER_OPTIONS = {
    "off": 0,
    "15": 15,
    "30": 30,
    "60": 60,
    "120": 120,
    "240": 240,
    "480": 480,
}
