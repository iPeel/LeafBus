// LeafBus board for the ESP32-S3: see mpconfigboard.cmake.

#ifndef MICROPY_HW_BOARD_NAME
#define MICROPY_HW_BOARD_NAME "LeafBus ESP32-S3"
#endif

// os.uname().machine ends in "with ESP32-S3"; panelbus.chip() reads it
#define MICROPY_HW_MCU_NAME "ESP32-S3"

// REPL on the native USB; the UART REPL too, for boards with a USB-UART chip
#define MICROPY_HW_ENABLE_UART_REPL (1)
