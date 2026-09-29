#!/usr/bin/env python3
"""uinput_type.py [SECRETFILE] — types the first line of SECRETFILE (or nothing) — TEST TOOL: a temporary virtual keyboard that presses Escape (wakes the unlock dialog),
types TEXT (a-z, 0-9) and Enter, then disappears. Used only by the pass-A live test."""
import fcntl, os, struct, sys, time
UI_SET_EVBIT, UI_SET_KEYBIT, UI_DEV_CREATE, UI_DEV_DESTROY = 0x40045564, 0x40045565, 0x5501, 0x5502
EV_SYN, EV_KEY = 0, 1
KEYS = {c: k for c, k in zip("1234567890", range(2, 12))}
KEYS.update({c: k for c, k in zip("qwertyuiop", range(16, 26))})
KEYS.update({c: k for c, k in zip("asdfghjkl", range(30, 39))})
KEYS.update({c: k for c, k in zip("zxcvbnm", range(44, 51))})
ESC, ENTER = 1, 28
fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
for k in set(KEYS.values()) | {ESC, ENTER}:
    fcntl.ioctl(fd, UI_SET_KEYBIT, k)
name = b"fedora-test-keyboard"
# struct uinput_user_dev: name[80], input_id(4 x u16), ff_effects_max u32, absmax/min/fuzz/flat 4 x 64 s32
os.write(fd, name.ljust(80, b"\0") + struct.pack("<HHHHI", 3, 0x1234, 0x5678, 1, 0) + b"\0" * (4 * 64 * 4))
fcntl.ioctl(fd, UI_DEV_CREATE)
def ev(t, c, v):
    s = time.time(); os.write(fd, struct.pack("<qqHHi", int(s), int((s % 1) * 1e6), t, c, v))
def press(k):
    ev(EV_KEY, k, 1); ev(EV_SYN, 0, 0); time.sleep(0.03); ev(EV_KEY, k, 0); ev(EV_SYN, 0, 0); time.sleep(0.06)
EVIOCGRAB = 0x40044590
def grabbed_by_gnome():
    # our node must already be grabbed (by fake_logind for mutter): then Android cannot see these keys
    for d in os.listdir("/sys/class/input"):
        try:
            if d.startswith("event") and open(f"/sys/class/input/{d}/device/name").read().strip() == name.decode():
                f = os.open(f"/dev/input/{d}", os.O_RDONLY)
                try:
                    fcntl.ioctl(f, EVIOCGRAB, 1)
                except OSError:
                    return True          # EBUSY: someone (fake_logind) holds the grab
                fcntl.ioctl(f, EVIOCGRAB, 0)
                return False
        except OSError:
            pass
    return False
for _ in range(30):
    if grabbed_by_gnome():
        break
    time.sleep(0.5)
else:
    fcntl.ioctl(fd, UI_DEV_DESTROY); sys.exit("virtual keyboard never grabbed by GNOME - not typing")
press(ESC); time.sleep(2)
text = open(sys.argv[1]).readline().strip() if len(sys.argv) > 1 else ""
for c in text:
    press(KEYS[c])
press(ENTER); time.sleep(1)
fcntl.ioctl(fd, UI_DEV_DESTROY); os.close(fd)
