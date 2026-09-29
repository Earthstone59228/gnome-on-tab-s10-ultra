#!/usr/bin/env python3
"""switcher_keys.py — reads the volume buttons (gpio-keys + mtk-pmic-keys) WITHOUT grabbing them and prints
"TRIGGER" (flushed) when VOL UP, DOWN, UP, DOWN are pressed within WINDOW_S seconds. Used by gnome-switcher.sh
(2026-09-27; replaces getevent, which takes one device only and block-buffers into a pipe — audit pass A, F3)."""
import os, select, struct, sys, time
WINDOW_S = 2.0
KEY_VOLUMEDOWN, KEY_VOLUMEUP, EV_KEY = 114, 115, 1
EVFMT = "llHHi"                      # struct input_event on aarch64: timeval(16) type code value
EVSZ = struct.calcsize(EVFMT)
fds = []
for d in sorted(os.listdir("/sys/class/input")):
    if not d.startswith("event"):
        continue
    try:
        name = open(f"/sys/class/input/{d}/device/name").read().strip()
    except OSError:
        continue
    if name in ("gpio-keys", "mtk-pmic-keys"):
        fds.append(os.open(f"/dev/input/{d}", os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC))
if not fds:
    print("NODEVICES", flush=True); sys.exit(1)
presses = []                         # (monotonic time, "U"/"D")
while True:
    ready, _, _ = select.select(fds, [], [])
    for fd in ready:
        try:
            data = os.read(fd, EVSZ * 64)
        except BlockingIOError:
            continue
        for off in range(0, len(data) - EVSZ + 1, EVSZ):
            _s, _u, typ, code, val = struct.unpack_from(EVFMT, data, off)
            if typ != EV_KEY or val != 1:
                continue
            if code not in (KEY_VOLUMEUP, KEY_VOLUMEDOWN):
                presses.clear(); continue
            presses.append((time.monotonic(), "U" if code == KEY_VOLUMEUP else "D"))
            presses[:] = presses[-4:]
            if len(presses) == 4 and "".join(k for _t, k in presses) == "UDUD" and \
                    presses[3][0] - presses[0][0] <= WINDOW_S:
                print("TRIGGER", flush=True); presses.clear()
