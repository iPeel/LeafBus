"""Autostart: the board comes up as an autonomous light controller.

Plays the startup effect, serves the web UI and listens for DDP and E1.31.

Stop:     Ctrl-C in the REPL, then keep typing within 10 s (mpremote does
          this by itself). A lone interrupt with nothing following it - a
          glitch on the USB serial line - restarts the board instead of
          leaving the panels dark at a prompt.
Disable:  import os; os.remove('main.py')

Unexpected errors are written to crash.log (shown in /api/info) and the
board restarts after 10 s.
"""
import sys
import time

import machine
import select

GRACE_MS = 10000


def someone_there(ms=GRACE_MS):
    """True when more input arrives on the console within ms. The input is
    not consumed: the REPL (or mpremote's raw REPL) gets it."""
    p = select.poll()
    p.register(sys.stdin, select.POLLIN)
    return bool(p.poll(ms))


print('main.py: starting LeafBus in 2 s (Ctrl-C to stop)')
time.sleep(2)

try:
    import app
    app.serve()
except KeyboardInterrupt:
    print('stopped; type anything within %d s to stay in the REPL' % (GRACE_MS // 1000))
    if not someone_there():
        print('nobody there: restarting')
        machine.reset()
    print('REPL is yours')
except Exception as e:
    sys.print_exception(e)
    try:
        with open('crash.log', 'w') as f:
            f.write('%d\n' % time.time())
            sys.print_exception(e, f)
    except OSError:
        pass
    print('crashed; restarting in %d s (type anything to stay in the REPL)' % (GRACE_MS // 1000))
    if not someone_there():
        machine.reset()
