#!/usr/bin/env python3
"""pogo_ghost.py — keep Android's view of the Book Cover Keyboard CONSTANT for a whole Fedora session (2026-09-27).

WHY (doc 11 §W open item, root cause read from this build's services.jar): the pogo driver unregisters / re-registers
the keyboard input device on every physical detach / attach (input65 = 65th registration). Android's
InputManagerService.deliverInputDevicesChanged sets mPogoKeyboardConnected = "some input device has vendor 0x04e8
product 0xa035", and the keyboard's presence feeds Configuration.keyboard / keyboardHidden / navigation. With
SurfaceFlinger stopped, the resulting config change starts a WM transition that can never finish -> BLASTSync timeout ->
DEAD_OBJECT -> system_server dies -> session over.

WHAT: a uinput clone of the keyboard — same name, bus 0x18, vendor 0x04e8, product 0xa035, version, same EV/KEY/LED
bitmaps (read from /proc/bus/input/devices at start) — so Android classifies it exactly like the real one
(KEYBOARD|ALPHAKEY|DPAD|EXTERNAL via the same Vendor_04e8_Product_a035 .idc/.kl). While it exists, the real
keyboard coming and going changes neither the pogo status nor the keyboard config. It never emits a key event.
Start it BEFORE SurfaceFlinger stops and stop it AFTER SurfaceFlinger is back, so the add/remove config changes (if
the real keyboard is absent) happen while Android can still render them.

SAFETY: only /dev/uinput; reads /proc/bus/input/devices (plain text, no driver ioctls, no sec_keypad sysfs — some of
those attributes issue I2C commands to the keyboard MCU). SIGTERM/SIGINT -> UI_DEV_DESTROY; on SIGKILL the kernel
destroys the device when the fd closes. If the real keyboard's capabilities can't be read, falls back to a built-in
copy of the EF-DX920 bitmaps (read 2026-09-27). Opt-out: /data/local/tmp/no-pogo-ghost (checked by the supervisor).
Lifetime: the supervisor starts it as `pogo-ghost-py` (a python3 symlink, so the teardown python3 sweep does not kill it
before SurfaceFlinger is back) with its own pid as argv[1], and kills it after the zygote restart. If that pid dies
first, the ghost removes itself within 2 s — a leftover ghost would make Android believe a keyboard is attached forever.
"""
import errno
import fcntl
import glob
import os
import select
import signal
import struct
import sys
import time

UINPUT = "/dev/uinput"
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_LEDBIT = 0x40045564, 0x40045565, 0x40045569   # linux/uinput.h _IOW('U',100/101/105,int)
UI_DEV_CREATE, UI_DEV_DESTROY = 0x5501, 0x5502
EV_SYN, EV_KEY, EV_LED = 0x00, 0x01, 0x11
VENDOR, PRODUCT = 0x04E8, 0xA035
# /proc/bus/input/devices of the real keyboard, 2026-09-27 (fallback only)
FALLBACK = {
    "name": "Book Cover Keyboard Slim (EF-DX920)", "bus": 0x18, "version": 0,
    "EV": "20003",
    "KEY": "7fffffffffffffff ffffffffffffffff ffffffffffffffff ffffffffffffffff ffffffffffffffff ffffffffffffffff "
           "ffffffff00000000 0 ffffffffffffffff ffffffffffffffff ffffffffffffffff fffffffffffffffe",
    "LED": "2",
}


def log(msg):
    print("%s [pogo-ghost] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def bits(hexwords):
    """/proc/bus/input/devices bitmap (space-separated hex words, most significant word FIRST, 64 bit per word
    on arm64) -> sorted list of set bit numbers."""
    out = []
    for i, w in enumerate(reversed(hexwords.split())):
        v = int(w, 16)
        for b in range(64):
            if v >> b & 1:
                out.append(i * 64 + b)
    return out


def read_real():
    """The real keyboard's block from /proc/bus/input/devices, or None when it is not attached."""
    try:
        with open("/proc/bus/input/devices") as f:
            blocks = f.read().split("\n\n")
    except OSError:
        return None
    for blk in blocks:
        if "Vendor=%04x Product=%04x" % (VENDOR, PRODUCT) not in blk:
            continue
        d = {}
        for line in blk.splitlines():
            if line.startswith("I: "):
                kv = dict(p.split("=") for p in line[3:].split())
                d["bus"], d["version"] = int(kv["Bus"], 16), int(kv["Version"], 16)
            elif line.startswith('N: Name="'):
                d["name"] = line[9:-1]
            elif line.startswith("B: ") and "=" in line:
                k, v = line[3:].split("=", 1)
                d[k] = v
        if {"name", "bus", "EV", "KEY"} <= d.keys():
            return d
    return None


def create(spec):
    fd = os.open(UINPUT, os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    ev = bits(spec["EV"])
    for e in ev:
        fcntl.ioctl(fd, UI_SET_EVBIT, e)
    keys = bits(spec["KEY"])
    for k in keys:
        fcntl.ioctl(fd, UI_SET_KEYBIT, k)
    if EV_LED in ev:
        for led in bits(spec.get("LED", "0")):
            fcntl.ioctl(fd, UI_SET_LEDBIT, led)
    name = spec["name"].encode()[:79]
    # uinput_user_dev: name[80] + input_id(bustype, vendor, product, version: u16) + ff_effects_max(u32) + 4x64 abs
    os.write(fd, name.ljust(80, b"\0") + struct.pack("<HHHHI", spec["bus"], VENDOR, PRODUCT, spec["version"], 0)
             + struct.pack("<64i", *([0] * 64)) * 4)
    fcntl.ioctl(fd, UI_DEV_CREATE)
    return fd, len(keys)


EVIOCGRAB = 0x40044590                      # _IOW('E', 0x90, int) — same constant as fake_logind.py / input_janitor.py
EVENT = struct.Struct("<qqHHi")             # struct input_event on arm64: timeval(2x long), type, code, value


def real_keyboards():
    """{inputN: /dev/input/eventM} for every REAL (non-virtual) 04e8:a035 keyboard currently registered."""
    out = {}
    for d in glob.glob("/sys/class/input/event*"):
        try:
            dev = os.path.realpath(d + "/device")
            with open(dev + "/id/vendor") as f:
                vid = int(f.read(), 16)
            with open(dev + "/id/product") as f:
                pid = int(f.read(), 16)
        except (OSError, ValueError):
            continue
        if vid == VENDOR and pid == PRODUCT and "/virtual/" not in dev:
            out[os.path.basename(dev)] = "/dev/input/" + os.path.basename(d)
    return out


class Relay:
    """2026-09-28 (doc 11 §AC): a keyboard RE-attached mid-session is a new input device that mutter never opens (no
    hotplug here) and Android must never see (input-hide). The ghost is already open + EVIOCGRABbed by mutter through
    fake_logind TakeDevice, so we grab the new real keyboard and replay its EV_KEY/EV_SYN into the ghost: keys
    reach GNOME only. Keyboards registered BEFORE the ghost started are never touched (mutter owns those).
    EBUSY = someone else (mutter/janitor) holds it -> leave it. Keys still down when the keyboard goes are released."""

    def __init__(self, ghost_fd):
        self.ghost = ghost_fd
        self.initial = set(real_keyboards())    # inputN ids present at session start: never relayed
        self.held = {}                          # fd -> (inputN, node)
        self.refused = set()                    # inputN we could not grab (logged once)
        self.down = set()                       # key codes currently pressed through the relay

    def ghost_owned_elsewhere(self):
        """SAFETY: relayed keys are only safe if something else (mutter via fake_logind TakeDevice) holds an EVIOCGRAB on
        the ghost — otherwise Android would read them (the §AB crash). Probe: our own EVIOCGRAB on the ghost node gives
        EBUSY exactly when someone else holds it; if it succeeds, release at once and report False."""
        node = None
        for d in glob.glob("/sys/class/input/event*"):
            dev = os.path.realpath(d + "/device")
            if "/virtual/" not in dev:
                continue
            try:
                with open(dev + "/id/vendor") as f, open(dev + "/id/product") as g:
                    if int(f.read(), 16) == VENDOR and int(g.read(), 16) == PRODUCT:
                        node = "/dev/input/" + os.path.basename(d)
                        break
            except (OSError, ValueError):
                continue
        if node is None:
            return False
        try:
            fd = os.open(node, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError:
            return False
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        except OSError as e:
            return e.errno == errno.EBUSY
        finally:
            os.close(fd)            # closing also drops our grab if the probe succeeded
        return False

    def scan(self):
        mine = {v[0] for v in self.held.values()}
        for inp, node in real_keyboards().items():
            if inp in self.initial or inp in mine or inp in self.refused:
                continue
            if not self.ghost_owned_elsewhere():
                self.refused.add(inp)
                log("relay: ghost is NOT grabbed by the session — refusing to relay %s (%s): keys would reach Android" % (node, inp))
                continue
            try:
                fd = os.open(node, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
            except OSError as e:
                log("relay: open %s (%s) failed: %s" % (node, inp, e))
                continue
            try:
                fcntl.ioctl(fd, EVIOCGRAB, 1)
            except OSError as e:
                os.close(fd)
                self.refused.add(inp)
                log("relay: %s (%s) not grabbed (%s) — not relaying it" % (node, inp, e))
                continue
            self.held[fd] = (inp, node)
            log("relay: re-attached keyboard %s (%s) grabbed — its keys now go to GNOME through the ghost" % (node, inp))

    def _emit(self, typ, code, value):
        os.write(self.ghost, EVENT.pack(0, 0, typ, code, value))

    def release_all(self):
        if self.down and not self.ghost_owned_elsewhere():   # key-ups would reach Android too: drop them silently
            log("relay: ghost not grabbed — NOT releasing %d key(s)" % len(self.down))
            self.down.clear()
            return
        for code in sorted(self.down):
            self._emit(EV_KEY, code, 0)
        if self.down:
            self._emit(EV_SYN, 0, 0)
            log("relay: released %d stuck key(s)" % len(self.down))
        self.down.clear()

    def drop(self, fd, why):
        inp, node = self.held.pop(fd)
        try:
            os.close(fd)
        except OSError:
            pass
        self.release_all()
        log("relay: %s (%s) gone (%s)" % (node, inp, why))

    def abandon(self):
        """Ghost lost its session grab: stop relaying everything at once, emitting NOTHING (audit 2026-09-28)."""
        for fd, (inp, node) in list(self.held.items()):
            try:
                os.close(fd)
            except OSError:
                pass
            self.refused.add(inp)
            log("relay: ghost is no longer grabbed by the session — dropped %s (%s) without relaying" % (node, inp))
        self.held.clear()
        self.down.clear()

    def pump(self, fd):
        try:
            data = os.read(fd, EVENT.size * 64)
        except BlockingIOError:
            return
        except OSError as e:                    # ENODEV = keyboard detached
            self.drop(fd, errno.errorcode.get(e.errno, str(e)))
            return
        if not data:
            self.drop(fd, "EOF")
            return
        for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
            _s, _us, typ, code, value = EVENT.unpack_from(data, off)
            if typ == EV_KEY:
                if value == 2:                  # kernel autorepeat: libinput repeats on its own
                    continue
                (self.down.add if value else self.down.discard)(code)
            elif typ != EV_SYN:                 # the ghost has no EV_MSC (EV bitmap 0x20003): nothing else to relay
                continue
            try:
                self._emit(typ, code, value)
            except OSError as e:
                log("relay: write to ghost failed: %s" % e)
                return

    def close(self):
        for fd in list(self.held):
            self.drop(fd, "shutdown")


def main():
    real = read_real()
    spec = real or FALLBACK
    try:
        fd, nkeys = create(spec)
    except (OSError, ValueError) as e:   # audit NIT: never leave a traceback-only failure; the session still runs
        log("could not create the ghost: %r — keyboard detach mid-session stays a crash trigger" % (e,))
        sys.exit(1)
    log("ghost '%s' %04x:%04x created (%d keys, caps from %s)" % (spec["name"], VENDOR, PRODUCT, nkeys,
                                                                  "the attached keyboard" if real else "built-in copy"))

    relay = Relay(fd)
    log("relay: ready (keyboards present at start, never relayed: %s)" % (", ".join(sorted(relay.initial)) or "none"))

    def _term(_s=None, _f=None):
        try:
            relay.close()
        except Exception as e:  # never let the relay block the ghost's own teardown
            log("relay: close failed: %r" % (e,))
        try:
            fcntl.ioctl(fd, UI_DEV_DESTROY)
            log("ghost destroyed")
        except OSError:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    owner = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    last_owner_check = 0.0
    while True:
        try:
            readable, _, _ = select.select(list(relay.held), [], [], 0.5)
            # SAFETY (audit 2026-09-28): re-check the session's grab on the ghost before EVERY batch we relay, not
            # only when the keyboard was first grabbed — a relayed key on an ungrabbed ghost reaches Android.
            if readable and not relay.ghost_owned_elsewhere():
                relay.abandon()
                readable = []
            for rfd in readable:
                if rfd in relay.held:
                    relay.pump(rfd)
            relay.scan()
        except Exception as e:   # the relay is a convenience; the ghost itself must stay up regardless
            log("relay: error %r" % (e,))
            time.sleep(1)
        if time.monotonic() - last_owner_check < 2:
            continue
        last_owner_check = time.monotonic()
        if owner is not None:
            try:
                os.kill(owner, 0)
            except ProcessLookupError:
                log("owner pid %d gone" % owner)
                _term()
            except PermissionError:
                pass


if __name__ == "__main__":
    main()
