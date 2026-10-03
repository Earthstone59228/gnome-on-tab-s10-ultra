#!/usr/bin/env python3
"""pen_rotate.py — keeps the S Pen aligned with the display rotation (2026-09-27).
GNOME treats sec_e-pen as an absolute pointer that is NOT mapped to the built-in monitor, so mutter never rotates it
(the touchscreen IS mapped and rotates correctly — measured with virtual clones of both devices). The pen's raw->screen
mapping is libinput's LIBINPUT_CALIBRATION_MATRIX (udev), read only when the device is added. So on every rotation:
write the matching matrix into a RUNTIME rule (/run/udev/rules.d/92-spen-rotation.rules; the persistent landscape
rule 91-spen-calibration.rules is never touched), reload udev and re-add just the pen node (private /dev/input of the
session namespace; mutter/libinput and fake_logind see remove+add). Matrices derived from the touchscreen measurement."""
import os, subprocess, sys, time
from gi.repository import Gio, GLib
RULE = "/run/udev/rules.d/92-spen-rotation.rules"
UDEVADM = "/usr/local/eudev/bin/udevadm"   # the private eudev (Fedora udevadm refuses to act in a chroot)
MATRIX = {0: "0 1 0 -1 0 1", 1: "1 0 0 0 1 0", 2: "0 -1 1 1 0 0", 3: "-1 0 1 0 -1 1"}
current = [None]
def log(m): print(time.strftime("%H:%M:%S"), "[pen-rotate]", m, flush=True)
def pen_nodes():
    out = []
    for d in os.listdir("/sys/class/input"):
        try:
            if d.startswith("event") and open(f"/sys/class/input/{d}/device/name").read().strip() == "sec_e-pen":
                out.append(d)
        except OSError:
            pass
    return out
def apply(t):
    if t == current[0] or t not in MATRIX:
        return
    if t == 0:
        try: os.unlink(RULE)
        except OSError: pass
    else:
        os.makedirs(os.path.dirname(RULE), exist_ok=True)
        with open(RULE + ".tmp", "w") as f:
            f.write('ACTION=="add|change", KERNEL=="event*", ATTRS{name}=="sec_e-pen", '
                    f'ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{MATRIX[t]}"\n')
        os.replace(RULE + ".tmp", RULE)
    subprocess.run([UDEVADM, "control", "--reload"], check=False)
    for n in pen_nodes():
        subprocess.run([UDEVADM, "trigger", "--action=remove", f"--sysname-match={n}"], check=False)
        time.sleep(0.4)
        subprocess.run([UDEVADM, "trigger", "--action=add", f"--sysname-match={n}"], check=False)
    log(f"transform {t}: pen matrix {MATRIX[t]} (re-added {pen_nodes()})")
    current[0] = t
def builtin_transform(bus):
    st = bus.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                       "org.gnome.Mutter.DisplayConfig", "GetCurrentState", None, None, 0, 5000, None).unpack()
    for lm in st[2]:                     # (x, y, scale, transform, primary, [(connector,...)], props)
        if any(m[0] == "DSI-1" for m in lm[5]):
            return lm[3]
    return 0
def main():
    try: os.unlink(RULE)
    except OSError: pass
    current[0] = 0
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    def changed(*_):
        try: apply(builtin_transform(bus))
        except Exception as e: log(f"error: {e!r}")
    bus.signal_subscribe("org.gnome.Mutter.DisplayConfig", "org.gnome.Mutter.DisplayConfig", "MonitorsChanged",
                         "/org/gnome/Mutter/DisplayConfig", None, 0, changed)
    changed()
    log("watching display rotation")
    GLib.MainLoop().run()
if __name__ == "__main__":
    main()
