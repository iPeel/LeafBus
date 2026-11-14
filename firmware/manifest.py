# Freeze manifest: the LeafBus modules built into the MicroPython image.
#
# Frozen modules run from flash. On a plain ESP32 that is the difference
# between a working and a starved network: loaded from the filesystem, the
# firmware's ~70 kB of bytecode lives in RAM, MicroPython's heap grows into
# the memory Wi-Fi and TCP need, and the web server stalls. See
# tools/build_firmware.sh.
#
# main.py and www/ stay on the filesystem (tools/deploy.py --frozen), so
# autostart can still be disabled by deleting main.py.

include("$(PORT_DIR)/boards/manifest.py")    # stock ESP32 contents (ntptime, asyncio, ...)

freeze(".", (                                 # relative to this manifest
    "panelbus.py",
    "geometry.py",
    "effects.py",
    "config.py",
    "wifi.py",
    "web.py",
    "ddp.py",
    "e131.py",
    "anim.py",
    "app.py",
))
