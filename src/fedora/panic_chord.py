#!/usr/bin/env python3
"""panic_chord.py — hardware escape hatch for the Fedora GNOME session (2026-09-26, audit #3).

Hold VOLUME UP + VOLUME DOWN together for HOLD_S seconds  ->  writes FLAG_PATH. The Android-side session
supervisor (fedora-session-gnome-shell.sh) polls that file and ends the session exactly like its time-box does
(kill the runner group -> normal restore: HWC, SurfaceFlinger, zygote, Wi-Fi). Works when gnome-shell is frozen,
when the touchscreen is dead and without a laptop/adb.

How it reads the buttons: EVIOCGKEY (the device's current key-state bitmap) polled every POLL_S. It never read()s
events and never grabs, so it cannot steal input from mutter or Android, and it keeps working while fake_logind
holds EVIOCGRAB on these devices (a grab only redirects event delivery; key state stays readable).
Devices are found by name (gpio-keys = volume up, mtk-pmic-keys = power + volume down on the SM-X926B).

Run by the supervisor as:  chroot /data/fedora /usr/bin/python3 /usr/local/bin/panic_chord.py
(the chroot's /dev is the real Android /dev, so /dev/input/eventN are the real nodes here).
"""
import array
import fcntl
import glob
import os
import sys
import time

KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115
EVIOCGKEY_96 = 0x80604518          # _IOC(READ, 'E', 0x18, 96): KEY_MAX 0x2ff -> 96 bytes
NAMES = ("gpio-keys", "mtk-pmic-keys")
HOLD_S = float(os.environ.get("PANIC_HOLD_S", "3.0"))
POLL_S = 0.25
FLAG_PATH = os.environ.get("PANIC_FLAG", "/tmp/.fedora-panic")


def log(msg):
    print("%s [panic-chord] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


VIBRATOR = "/sys/class/timed_output/vibrator/enable"


def buzz(ms):
    try:
        with open(VIBRATOR, "w") as f:
            f.write("%d" % ms)
    except OSError as e:
        log("vibrator: %s" % e)


def open_buttons():
    fds = []
    for d in sorted(glob.glob("/sys/class/input/event*")):
        try:
            with open(d + "/device/name") as f:
                name = f.read().strip()
        except OSError:
            continue
        if name in NAMES:
            node = "/dev/input/" + os.path.basename(d)
            try:
                fds.append((os.open(node, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK), node, name))
            except OSError as e:
                log("open %s (%s) failed: %s" % (node, name, e))
    return fds


def held(fds):
    down = set()
    for fd, _node, _name in fds:
        buf = array.array("B", bytes(96))
        try:
            fcntl.ioctl(fd, EVIOCGKEY_96, buf, True)
        except OSError:
            continue
        for k in (KEY_VOLUMEUP, KEY_VOLUMEDOWN):
            if buf[k // 8] & (1 << (k % 8)):
                down.add(k)
    return down


def main():
    fds = open_buttons()
    if not fds:
        log("no button devices found — panic chord unavailable")
        return 1
    log("watching %s; hold VOL UP + VOL DOWN for %.1f s to end the session" %
        (", ".join("%s(%s)" % (n, nm) for _f, n, nm in fds), HOLD_S))
    since = None
    while True:
        if held(fds) == {KEY_VOLUMEUP, KEY_VOLUMEDOWN}:
            since = since or time.monotonic()
            if time.monotonic() - since >= HOLD_S:
                with open(FLAG_PATH, "w") as f:
                    f.write("%d\n" % time.time())
                log("chord held %.1f s -> %s written, supervisor will end the session" % (HOLD_S, FLAG_PATH))
                buzz(150)              # 2026-09-27: physical confirmation that the chord was accepted
                since = None
                time.sleep(5)          # one trigger per hold
        else:
            since = None
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
