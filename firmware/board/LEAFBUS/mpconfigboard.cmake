# LeafBus board: the stock ESP32_GENERIC board with a fixed split of the RAM
# between MicroPython and Wi-Fi / lwIP. Built by tools/build_firmware.sh (or
# .ps1) with -D MICROPY_BOARD_DIR=<this directory>; paths without a directory
# are relative to micropython/ports/esp32. The C side is in
# tools/patch_micropython.py.
include(boards/ESP32_GENERIC/mpconfigboard.cmake)

list(APPEND SDKCONFIG_DEFAULTS
    ${MICROPY_BOARD_DIR}/sdkconfig.leafbus
)

# Measured with 26 panels (plain ESP32, no PSRAM): stock MicroPython grows its
# heap into the ESP-IDF heap and gives empty areas back after collections;
# Wi-Fi and lwIP fill those with queued packets at once, Python takes the
# memory back, and the network starves (sends block, select() fails with EIO,
# the station can drop). With these two settings the Python heap grows once,
# at start-up, to 112 kB and keeps it; Wi-Fi and lwIP keep the rest.
#   heavy web load, 170 s: 0 failed requests (stock: 3-14); rescans 20/20
#   (stock: 11/20); all features exercised without an error.
list(APPEND MICROPY_DEF_BOARD
    MICROPY_HW_BOARD_NAME="LeafBus ESP32"
    LEAFBUS_IDF_RESERVE=32768     # never grow the Python heap below 32 kB of free ESP-IDF memory
    LEAFBUS_GC_KEEP_AREAS=1       # never give heap areas back
)

# For trying settings without editing these files:
#   -D LEAFBUS_SDKCONFIG_EXTRA=<file>    one more sdkconfig fragment, applied last
#                                        (e.g. CONFIG_ESP_WIFI_SOFTAP_SUPPORT=n)
#   -D LEAFBUS_DEFS_EXTRA="A=1;B=2"      more C defines (later ones win)
if(LEAFBUS_SDKCONFIG_EXTRA)
    list(APPEND SDKCONFIG_DEFAULTS ${LEAFBUS_SDKCONFIG_EXTRA})
endif()
if(LEAFBUS_DEFS_EXTRA)
    list(APPEND MICROPY_DEF_BOARD ${LEAFBUS_DEFS_EXTRA})
endif()
