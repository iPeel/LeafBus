# LeafBus board for the ESP32-S3 (e.g. Wemos/LOLIN S3 Mini: ESP32-S3FH4R2,
# 4 MB flash, 2 MB quad PSRAM, native USB). The stock ESP32_GENERIC_S3 board
# plus the LeafBus settings of firmware/board/LEAFBUS. Built by
# tools/build_firmware.ps1 -Chip esp32s3 (or build_firmware.sh esp32s3).
#
# With PSRAM the Python heap grows into the external RAM (allocations above
# CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL go there first) and the internal RAM
# stays with Wi-Fi and lwIP: the memory squeeze of the plain ESP32 does not
# arise. The S3 keeps a second core for Wi-Fi, as the ESP32 does.
include(boards/ESP32_GENERIC_S3/mpconfigboard.cmake)

list(APPEND SDKCONFIG_DEFAULTS
    ${MICROPY_BOARD_DIR}/../LEAFBUS/sdkconfig.leafbus
)

list(APPEND MICROPY_DEF_BOARD
    MICROPY_HW_BOARD_NAME="LeafBus ESP32-S3"
    LEAFBUS_GC_KEEP_AREAS=1       # never give heap areas back (see LEAFBUS)
)

if(LEAFBUS_SDKCONFIG_EXTRA)
    list(APPEND SDKCONFIG_DEFAULTS ${LEAFBUS_SDKCONFIG_EXTRA})
endif()
if(LEAFBUS_DEFS_EXTRA)
    list(APPEND MICROPY_DEF_BOARD ${LEAFBUS_DEFS_EXTRA})
endif()
