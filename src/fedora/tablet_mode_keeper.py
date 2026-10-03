#!/usr/bin/env python3
"""tablet_mode_keeper.py — virtual SW_TABLET_MODE switch so mutter auto-rotation works (2026-09-27, UN-AUDITED).

Why (mutter main, src/backends/native/meta-seat-impl.c update_touch_mode):
  no touchscreen            -> touch mode OFF
  has a tablet-mode switch  -> touch mode = the switch state (win over everything else)
  no tablet-mode switch     -> touch mode = "no pointer present"
This device has NO SW_TABLET_MODE switch (checked all /sys/class/input event caps), and the S Pen is
classified as a pointer (src=mouse) that is ALWAYS present -> touch mode permanently FALSE ->
meta-monitor-manager.c update_panel_orientation_managed() requires touch_mode && has_accelerometer &&
builtin_monitor -> panel_orientation_managed FALSE -> orientation_changed() returns early -> every
AccelerometerOrientation change is IGNORED. That is why auto-rotation "just doesn't work".

What this does: presents a uinput device named "fedora-tablet-mode" carrying ONLY SW_TABLET_MODE — the same
mechanism convertible laptops use — with the state driven by the pogo keyboard:
  keyboard NOT connected -> SW_TABLET_MODE=1  (touch mode: rotation follows the sensor)
  keyboard connected     -> SW_TABLET_MODE=0  (no auto-rotation while typing)
Mutter re-gates on notify::touch-mode (meta-monitor-manager.c), so flips apply live. GNOME's rotation-lock
quick setting still works on top. Pen/touch mapping under rotation is already handled (pen_rotate.py §U).

Interaction with input_janitor.py: the janitor skips devices whose name starts with "fedora-" (its skip list),
so mutter can claim the switch; once mutter takes it via fake_logind TakeDevice the EVIOCGRAB stops Android
from reading it too. A switch-only device sets no KEYBOARD capability -> no Android CONFIG_KEYBOARD change.

Started by the runner next to pen_rotate.py (after the shell is up, so mutter hotplug is live — proven by the
§U rot-test virtual clones). Killed with the session; UI_DEV_DESTROY removes the device (mutter recomputes
touch mode from device removal). Opt-out: /usr/local/etc/no-tablet-mode
"""
import fcntl
import os
import struct
import sys
import time

UI_SET_EVBIT, UI_SET_SWBIT, UI_SET_PROPBIT = 0x40045564, 0x4004556d, 0x4004556e
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
EV_SYN, EV_SW = 0x00, 0x05
SW_TABLET_MODE = 0x01
INPUT_PROP_DIRECT_UNUSED = 0  # keep the import surface minimal; props not needed for switches

UINPUT = "/dev/uinput"
DEV_NAME = b"fedora-tablet-mode"
VENDOR, PRODUCT, VERSION = 0x1235, 0x5680, 1
BUS_VIRTUAL = 0x06

KEYBOARD_NAME = "Book Cover Keyboard Slim (EF-DX920)"
KEYBOARD_CONNECTED_NODE = "/sys/class/sec/sec_keypad/keyboard_connected"
POLL_S = 2.0
WAIT_TAKE_S = 30.0
NO_FLAG = "/usr/local/etc/no-tablet-mode"


def log(msg):
    print("%s [tablet-mode] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def keyboard_connected():
    try:
        with open(KEYBOARD_CONNECTED_NODE) as f:
            v = f.read().strip()
        if v in ("1", "0"):
            return v == "1"
        log("keyboard_connected node returned %r — falling back to evdev name check" % v)
    except OSError as e:
        log("cannot read %s (%s) — falling back to evdev name check" % (KEYBOARD_CONNECTED_NODE, e))
    # Fallback: is the keyboard evdev device present by name?
    try:
        for d in sorted(os.listdir("/sys/class/input")):
            if not d.startswith("event"):
                continue
            try:
                with open("/sys/class/input/%s/device/name" % d) as f:
                    if f.read().strip() == KEYBOARD_NAME:
                        return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def make_switch():
    fd = os.open(UINPUT, os.O_WRONLY | os.O_NONBLOCK)
    fcntl.ioctl(fd, UI_SET_EVBIT, EV_SW)
    fcntl.ioctl(fd, UI_SET_SWBIT, SW_TABLET_MODE)
    # uinput_user_dev: name[80] + input_id(4xu16) + ff_effects_max(u32) + 4x64 abs extents
    os.write(fd, DEV_NAME.ljust(80, b"\0") + struct.pack("<HHHHI", BUS_VIRTUAL, VENDOR, PRODUCT, VERSION, 0)
             + struct.pack("<64i", *([0] * 64)) * 4)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd


def emit_switch(fd, on):
    s = time.time()
    ev = struct.pack("<qqHHi", int(s), int((s % 1) * 1e6), EV_SW, SW_TABLET_MODE, 1 if on else 0)
    syn = struct.pack("<qqHHi", int(s), int((s % 1) * 1e6), EV_SYN, 0, 0)
    os.write(fd, ev + syn)


def taken_by_mutter():
    """True when the switch node is EVIOCGRABbed by someone (mutter/fake_logind TakeDevice path)."""
    try:
        for d in sorted(os.listdir("/sys/class/input")):
            if not d.startswith("event"):
                continue
            try:
                with open("/sys/class/input/%s/device/name" % d) as f:
                    if f.read().strip() != DEV_NAME.decode():
                        continue
            except OSError:
                continue
            try:
                fd = os.open("/dev/input/" + d, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                return False
            try:
                fcntl.ioctl(fd, 0x40044590, 1)   # EVIOCGRAB
                fcntl.ioctl(fd, 0x40044590, 0)
                os.close(fd)
                return False                      # we could grab it -> nobody else holds it yet
            except OSError:
                os.close(fd)
                return True                       # EBUSY -> a grabber already owns it (mutter)
    except OSError:
        pass
    return False


def main():
    if os.path.exists(NO_FLAG):
        log("disabled (%s present)" % NO_FLAG)
        return
    fd = make_switch()
    log("virtual switch created (SW_TABLET_MODE)")
    on = not keyboard_connected()
    emit_switch(fd, on)
    log("initial SW_TABLET_MODE=%d (keyboard %s)" % (1 if on else 0, "connected" if not on else "not connected"))

    waited = 0.0
    while waited < WAIT_TAKE_S and not taken_by_mutter():
        time.sleep(1.0)
        waited += 1.0
    log("mutter %s the switch after %.0fs" % ("took" if waited < WAIT_TAKE_S else "has NOT taken", waited))

    import signal
    state = on
    def _term(_s=None, _f=None):
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(fd)
        log("switch destroyed, exiting")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    while True:
        time.sleep(POLL_S)
        now_on = not keyboard_connected()
        if now_on != state:
            state = now_on
            emit_switch(fd, state)
            log("keyboard %s -> SW_TABLET_MODE=%d" % ("attached" if not state else "detached", 1 if state else 0))


if __name__ == "__main__":
    main()