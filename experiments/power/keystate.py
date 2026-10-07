#!/usr/bin/env python3
"""keystate.py -- print which of VOL UP / VOL DOWN / POWER are held right now; exit 0 if any, 1 if none, 2 on error.
Same technique as panic_chord.py: EVIOCGKEY key-state ioctl, never read()s events, never grabs (safe next to fake_logind's
grab). Called once per cycle by fedora-suspend-dpms.sh so a button press can end sleep even when it lands in an awake window.
Run: chroot /data/fedora /usr/bin/python3 /usr/local/bin/keystate.py
"""
import array
import fcntl
import glob
import os
import sys

EVIOCGKEY_96 = 0x80604518
KEYS = {114: "VOLDOWN", 115: "VOLUP", 116: "POWER"}
NAMES = ("gpio-keys", "mtk-pmic-keys")


def main():
    pressed = set()
    seen = 0
    for d in sorted(glob.glob("/sys/class/input/event*")):
        try:
            with open(d + "/device/name") as f:
                name = f.read().strip()
        except OSError:
            continue
        if name not in NAMES:
            continue
        try:
            fd = os.open("/dev/input/" + os.path.basename(d), os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            buf = array.array("B", [0] * 96)
            fcntl.ioctl(fd, EVIOCGKEY_96, buf, True)
            seen += 1
            for code in KEYS:
                if buf[code // 8] & (1 << (code % 8)):
                    pressed.add(code)
        except OSError:
            pass
        finally:
            os.close(fd)
    if seen != len(NAMES):
        print("error: read %d of %d key devices" % (seen, len(NAMES)))
        return 2
    if pressed:
        print(" ".join(KEYS[c] for c in sorted(pressed)))
        return 0
    return 1


try:
    rc = main()
except BaseException as e:
    print("error: %r" % (e,))
    rc = 2
sys.exit(rc)
