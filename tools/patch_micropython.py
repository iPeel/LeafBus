#!/usr/bin/env python3
"""Patch a MicroPython v1.29 checkout for the LeafBus image.

    python3 tools/patch_micropython.py <micropython dir> [--windows]

Also lets ESP-IDF be built without SoftAP support (see patch_softap).

--windows (default on a Windows host): also make the build run under
Windows without a POSIX shell. MicroPython's QSTR rules (py/mkrules.cmake)
pass ~50 kB of compiler flags on one command line - Windows allows 8 kB in
cmd and 32 kB per process - and pipe through cat and sed. With the patch the
arguments go through response files, the pipeline is a Python helper
(py/leafbus_qstrpp.py) and touch is cmake -E touch. The output is the same.

Heap reserve. MicroPython's heap on the ESP32 grows on demand: when a
collection cannot satisfy an allocation it adds an area as large as the heap
already is, taken from the ESP-IDF heap, as long as the largest free block
there allows (ports/esp32/gccollect.c, gc_get_max_new_split()). Nothing is
kept back for Wi-Fi and lwIP, which allocate from the same heap while they
run. After this patch the Python heap may only grow into what lies beyond
the last LEAFBUS_IDF_RESERVE bytes of free ESP-IDF memory (mpconfigboard.h
of firmware/board/LEAFBUS sets it; 0 keeps the stock behaviour).

Idempotent; exits with an error if the code to patch is not found.
"""
import os
import sys

MARK = 'LEAFBUS_IDF_RESERVE'
OLD = '''size_t gc_get_max_new_split(void) {
    return heap_caps_get_largest_free_block(MALLOC_CAP_DEFAULT);
}'''
NEW = '''#ifndef LEAFBUS_IDF_RESERVE
#define LEAFBUS_IDF_RESERVE (0)
#endif

// LeafBus: never grow the Python heap into the last LEAFBUS_IDF_RESERVE
// bytes of free ESP-IDF memory; Wi-Fi and lwIP allocate from it at run time.
size_t gc_get_max_new_split(void) {
    size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_DEFAULT);
    size_t free = heap_caps_get_free_size(MALLOC_CAP_DEFAULT);
    if (free <= LEAFBUS_IDF_RESERVE) {
        return 0;
    }
    free -= LEAFBUS_IDF_RESERVE;
    return largest < free ? largest : free;
}'''


# Keep heap areas. After a collection MicroPython gives every empty heap area
# back to ESP-IDF (py/gc.c, gc_sweep_free_blocks). On the ESP32 Wi-Fi and lwIP
# fill that memory with queued packets at once, and when Python needs it
# again it takes the largest free block - what the network had left - so the
# two keep starving each other (measured: Wi-Fi heap 20 kB -> 1.4 kB during a
# rescan, sends blocked, the station dropped). With LEAFBUS_GC_KEEP_AREAS=1
# the Python heap only ever grows to its peak and stays there.
KEEP_MARK = 'LEAFBUS_GC_KEEP_AREAS'
KEEP_OLD = '''        // Free any empty area, aside from the first one
        if (last_used_block == 0 && prev_area != NULL) {'''
KEEP_NEW = '''        // Free any empty area, aside from the first one
        // (LeafBus: unless LEAFBUS_GC_KEEP_AREAS, tools/patch_micropython.py)
        #ifndef LEAFBUS_GC_KEEP_AREAS
        #define LEAFBUS_GC_KEEP_AREAS (0)
        #endif
        if (!LEAFBUS_GC_KEEP_AREAS && last_used_block == 0 && prev_area != NULL) {'''


def patch_keep_areas(mp):
    path = os.path.join(mp, 'py', 'gc.c')
    with open(path, encoding='utf-8', newline='') as f:
        src = f.read()
    if KEEP_MARK in src:
        return
    crlf = '\r\n' in src
    old = KEEP_OLD.replace('\n', '\r\n') if crlf else KEEP_OLD
    new = KEEP_NEW.replace('\n', '\r\n') if crlf else KEEP_NEW
    if src.count(old) != 1:
        sys.exit('patch_micropython: empty-area freeing not found as expected in %s; '
                 'MicroPython version changed?' % path)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(src.replace(old, new))
    print('patch_micropython: heap areas can be kept in %s' % path)


# No SoftAP. ESP-IDF built with CONFIG_ESP_WIFI_SOFTAP_SUPPORT=n has no
# esp_netif_create_default_wifi_ap(); MicroPython calls it unconditionally.
# Without it the AP interface has no netif, and WLAN(AP_IF).active(True)
# raises OSError (wifi.py then carries on without the access point).
AP_MARK = 'LeafBus: no SoftAP'
AP_OLD = '''        wlan_ap_obj.netif = esp_netif_create_default_wifi_ap();'''
AP_NEW = '''        #if CONFIG_ESP_WIFI_SOFTAP_SUPPORT  // LeafBus: no SoftAP
        wlan_ap_obj.netif = esp_netif_create_default_wifi_ap();
        #else
        wlan_ap_obj.netif = NULL;
        #endif'''


def patch_softap(mp):
    path = os.path.join(mp, 'ports', 'esp32', 'network_wlan.c')
    with open(path, encoding='utf-8', newline='') as f:
        src = f.read()
    if AP_MARK in src:
        return
    crlf = '\r\n' in src
    old = AP_OLD.replace('\n', '\r\n') if crlf else AP_OLD
    new = AP_NEW.replace('\n', '\r\n') if crlf else AP_NEW
    if src.count(old) != 1:
        sys.exit('patch_micropython: esp_netif_create_default_wifi_ap() not found as expected '
                 'in %s; MicroPython version changed?' % path)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(src.replace(old, new))
    print('patch_micropython: SoftAP made optional in %s' % path)


# --- Windows build host ------------------------------------------------------------

WIN_MARK = 'LeafBus: Windows'

# makeqstrdefs.py: expand @file arguments (one argument per line)
ARGV_OLD = '''if __name__ == "__main__":
    if len(sys.argv) < 6:'''
ARGV_NEW = '''if __name__ == "__main__":
    # LeafBus: Windows command lines are too short for the build's arguments;
    # @file stands for the lines of that file, one argument each.
    _argv = []
    for _a in sys.argv[1:]:
        if _a.startswith("@") and os.path.isfile(_a[1:]):
            with open(_a[1:], encoding="utf-8") as _f:
                for _l in _f.read().splitlines():
                    _argv.extend(x for x in _l.split(";") if x)
        else:
            _argv.append(_a)
    sys.argv = sys.argv[:1] + _argv
    if len(sys.argv) < 6:'''

# makeqstrdefs.py: compiler flags through a gcc response file
PP_OLD = '''                cmd = args.pp + flags + files'''
PP_NEW = '''                cmd = args.pp + _gcc_rsp(flags) + files  # LeafBus: Windows'''
GCC_RSP = '''

def _gcc_rsp(flags):
    """LeafBus: Windows. The flags as one gcc @file argument (gcc quoting)."""
    if os.name != "nt" or not flags:
        return flags
    path = "%s.%d.rsp" % (args.output[0], len(flags))
    with open(path, "w", encoding="utf-8") as f:
        for a in flags:
            f.write('"%s"\\n' % a.replace("\\\\", "\\\\\\\\").replace('"', '\\\\"'))
    return ["@" + path]
'''
GCC_RSP_AFTER = '''def write_out(fname, output):'''

# mkrules.cmake: argument files instead of long command lines
RULE_PP_OLD = '''    COMMAND ${Python3_EXECUTABLE} ${MICROPY_PY_DIR}/makeqstrdefs.py pp ${CMAKE_C_COMPILER} -E output ${MICROPY_GENHDR_DIR}/qstr.i.last cflags ${MICROPY_CPP_FLAGS} -DNO_QSTR cxxflags ${MICROPY_CPP_FLAGS} -DNO_QSTR sources ${MICROPY_SOURCE_QSTR}'''
RULE_PP_NEW = '''    COMMAND ${Python3_EXECUTABLE} ${MICROPY_PY_DIR}/makeqstrdefs.py @${MICROPY_GENHDR_DIR}/qstr_pp.rsp'''
RULE_PP_BEFORE = '''add_custom_command(
    OUTPUT ${MICROPY_QSTRDEFS_LAST}'''
RULE_RSP = '''# LeafBus: Windows. Arguments of the two QSTR preprocessor steps in files, one
# per line: the command lines would exceed what Windows accepts.
set(_lb_pp pp ${CMAKE_C_COMPILER} -E output ${MICROPY_GENHDR_DIR}/qstr.i.last cflags ${MICROPY_CPP_FLAGS} -DNO_QSTR cxxflags ${MICROPY_CPP_FLAGS} -DNO_QSTR sources ${MICROPY_SOURCE_QSTR})
string(REPLACE ";" "\\n" _lb_pp "${_lb_pp}")
file(GENERATE OUTPUT ${MICROPY_GENHDR_DIR}/qstr_pp.rsp CONTENT "${_lb_pp}\\n")
string(REPLACE ";" "\\n" _lb_cpp "${MICROPY_CPP_FLAGS}")
file(GENERATE OUTPUT ${MICROPY_GENHDR_DIR}/qstr_cpp.rsp CONTENT "${_lb_cpp}\\n")

'''
RULE_CAT_OLD = '''    COMMAND cat ${MICROPY_QSTRDEFS_PY} ${MICROPY_QSTRDEFS_PORT} ${MICROPY_QSTRDEFS_COLLECTED} | sed "s/^Q(.*)/\\"&\\"/" | ${CMAKE_C_COMPILER} -E ${MICROPY_CPP_FLAGS} - | sed "s/^\\\\\\"\\\\(Q(.*)\\\\)\\\\\\"/\\\\1/" > ${MICROPY_QSTRDEFS_PREPROCESSED}'''
RULE_CAT_NEW = '''    COMMAND ${Python3_EXECUTABLE} ${MICROPY_PY_DIR}/leafbus_qstrpp.py ${CMAKE_C_COMPILER} ${MICROPY_GENHDR_DIR}/qstr_cpp.rsp ${MICROPY_QSTRDEFS_PREPROCESSED} ${MICROPY_QSTRDEFS_PY} ${MICROPY_QSTRDEFS_PORT} ${MICROPY_QSTRDEFS_COLLECTED}'''
TOUCH_OLD = '''    COMMAND touch '''
TOUCH_NEW = '''    COMMAND ${CMAKE_COMMAND} -E touch '''

QSTRPP = '''"""LeafBus: Windows. What py/mkrules.cmake does with a shell pipeline:

    cat <qstrdefs...> | sed 's/^Q(.*)/"&"/' | cc -E <flags> - | sed 's/^"\\\\(Q(.*)\\\\)"/\\\\1/' > out

usage: leafbus_qstrpp.py <cc> <flags file, one per line> <out> <qstrdefs files...>
"""
import re
import subprocess
import sys

cc, flags_file, out = sys.argv[1:4]
text = ""
for path in sys.argv[4:]:
    with open(path, encoding="utf-8") as f:
        text += f.read()
text = re.sub(r"^Q\\(.*\\)", lambda m: '"%s"' % m.group(0), text, flags=re.M)
rsp = out + ".rsp"
with open(flags_file, encoding="utf-8") as f, open(rsp, "w", encoding="utf-8") as g:
    for line in f.read().splitlines():
        for a in line.split(";"):
            if a:
                g.write('"%s"\\n' % a.replace("\\\\", "\\\\\\\\").replace('"', '\\\\"'))
r = subprocess.run([cc, "-E", "@" + rsp, "-"], input=text.encode("utf-8"), stdout=subprocess.PIPE)
if r.returncode:
    sys.exit(r.returncode)
result = re.sub(r'^"(Q\\(.*\\))"', r"\\1", r.stdout.decode("utf-8"), flags=re.M)
with open(out, "w", encoding="utf-8", newline="\\n") as f:
    f.write(result.replace("\\r\\n", "\\n"))
'''


def _edit(path, edits):
    """Apply (old, new) replacements, each of which must match exactly once."""
    with open(path, encoding='utf-8', newline='') as f:
        src = f.read()
    if WIN_MARK in src:
        return False
    crlf = '\r\n' in src
    for old, new, count in edits:
        o = old.replace('\n', '\r\n') if crlf else old
        n = new.replace('\n', '\r\n') if crlf else new
        found = src.count(o)
        if found != count and not (count == -1 and found):
            sys.exit('patch_micropython: %r... not found as expected in %s (%d times); '
                     'MicroPython version changed?' % (old[:60], path, found))
        src = src.replace(o, n)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(src)
    return True


def patch_windows(mp):
    py = os.path.join(mp, 'py')
    if _edit(os.path.join(py, 'makeqstrdefs.py'), [
            (ARGV_OLD, ARGV_NEW, 1),
            (PP_OLD, PP_NEW, 1),
            (GCC_RSP_AFTER, GCC_RSP.lstrip('\n') + '\n\n' + GCC_RSP_AFTER, 1)]):
        print('patch_micropython: Windows: response files in py/makeqstrdefs.py')
    if _edit(os.path.join(py, 'mkrules.cmake'), [
            ('# Generate qstrs\n', '# Generate qstrs\n# ' + WIN_MARK + ' (tools/patch_micropython.py)\n', 1),
            (RULE_PP_BEFORE, RULE_RSP + RULE_PP_BEFORE, 1),
            (RULE_PP_OLD, RULE_PP_NEW, 1),
            (RULE_CAT_OLD, RULE_CAT_NEW, 1),
            (TOUCH_OLD, TOUCH_NEW, -1)]):
        print('patch_micropython: Windows: response files and no shell in py/mkrules.cmake')
    with open(os.path.join(py, 'leafbus_qstrpp.py'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(QSTRPP)


def main():
    argv = [a for a in sys.argv[1:] if not a.startswith('--')]
    if len(argv) != 1:
        sys.exit(__doc__)
    mp = argv[0]
    path = os.path.join(mp, 'ports', 'esp32', 'gccollect.c')
    with open(path, encoding='utf-8', newline='') as f:
        src = f.read()
    if MARK in src:
        print('patch_micropython: %s already patched' % path)
    else:
        crlf = '\r\n' in src
        old = OLD.replace('\n', '\r\n') if crlf else OLD
        new = NEW.replace('\n', '\r\n') if crlf else NEW
        if src.count(old) != 1:
            sys.exit('patch_micropython: gc_get_max_new_split() not found as expected in %s; '
                     'MicroPython version changed?' % path)
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(src.replace(old, new))
        print('patch_micropython: heap reserve added to %s' % path)
    patch_keep_areas(mp)
    patch_softap(mp)
    if '--windows' in sys.argv or os.name == 'nt':
        patch_windows(mp)


if __name__ == '__main__':
    main()
