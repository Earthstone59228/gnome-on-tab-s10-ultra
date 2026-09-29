#!/usr/bin/env python3
"""input_janitor.py — session input guard (2026-09-27, UN-AUDITED — pending fresh-subagent review).

Why this exists (2026-09-27 sessions #17/#18 root cause, crash buffer + ANR traces):
a single MotionEvent that reaches Android mid-session kills the session. Android still reads
/dev/input for any node nobody grabbed (e.g. a BLE HID mouse whose uhid node was created BEFORE
the session started, so fake_logind/mutter never took it). Samsung freecess freezes SystemUI
with the event unacked -> 10 s input ANR on its gesture monitors -> system_server shows the
ANR error dialog -> addWindow -> SurfaceControl.nativeCreate -> DEAD_OBJECT (SurfaceFlinger is
stopped) -> FATAL in android.ui -> system_server dies -> every restart dies at boot
(DisplayManagerService "Timeout waiting for default display", phase 100) -> 30 s crash loop ->
the session guard ends the session. WiFi/BT look flaky in that window because the framework is
dying every ~30 s.

What this does: EVIOCGRAB every /dev/input/eventN node that is NOT already grabbed.
- EBUSY on EVIOCGRAB = mutter/fake_logind already own the device — exactly what we want, skip.
- Grabbed-but-never-read means events are black-holed (the evdev client buffer fills and drops).
- New nodes (BT HID reconnect creating a fresh uhid node, USB OTG HID) are grabbed on inotify
  IN_CREATE plus a short periodic rescan, so Android can never read them for long.
- The grab lives on our fd: when this process exits/killed, every grab releases automatically
  and Android sees its devices again (session end / restore unaffected).

Trade-off (documented, reversible): while a session runs, BT/USB HID input devices cannot reach
GNOME either — they were never wired into mutter hotplug yet (doc 11 §T: untested). Opt-out flag:
/usr/local/etc/no-input-janitor (created on the Android side at /data/local/tmp/no-input-janitor
and bind-visible? no — the supervisor checks $T/no-input-janitor before starting this script).

Run by the supervisor as:  chroot /data/fedora /usr/bin/python3 /usr/local/bin/input_janitor.py
(the chroot /dev is the real Android /dev — panic_chord.py precedent).
"""
import ctypes
import ctypes.util
import fcntl
import glob
import os
import select
import signal
import struct
import sys
import time

EVIOCGRAB = 0x40044590                      # _IOW('E', 0x90, int) — same constant as fake_logind.py
NO_FLAG = "/usr/local/etc/no-input-janitor"
RESCAN_S = float(os.environ.get("JANITOR_RESCAN_S", "2.0"))
# 0 = auto: wait for the gnome-shell process (input init / TakeDevice pass happens before its main
# loop starts), then settle, THEN do the initial grab. A fixed override (seconds) exists for smoke
# tests. Without this wait the janitor would start BEFORE mutter takes the touchscreen/pen/keyboard
# and would steal them (mutter TakeDevice -> EBUSY -> dead input in GNOME). Fixed 2026-09-27.
GRACE_S = float(os.environ.get("JANITOR_GRACE_S", "0"))
SETTLE_S = 10.0
WAIT_CAP_S = 60.0

# inotify constants (linux/inotify.h)
IN_CREATE = 0x00000100
IN_ATTRIB = 0x00000008
IN_DELETE = 0x00000200

held = {}   # node -> fd

# 2026-09-27 UN-AUDITED: our own virtual devices (tablet-mode switch etc.) must reach mutter, not be
# black-holed here — skip anything named fedora-* (mutter/fake_logind will take it and its grab then
# also hides it from Android).
SKIP_NAME_PREFIXES = ("fedora-",)


def log(msg):
    print("%s [input-janitor] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def dev_name(node):
    sysp = "/sys/class/input/%s/device/name" % os.path.basename(node)
    try:
        with open(sysp) as f:
            return f.read().strip()
    except OSError:
        return "?"


_ss_pid = [None]
_left_to_ghost = set()


def system_server_pid():
    pid = _ss_pid[0]
    if pid is not None:
        try:
            with open("/proc/%d/comm" % pid) as f:
                if f.read().strip() == "system_server":
                    return pid
        except OSError:
            pass
    _ss_pid[0] = None
    for p in os.listdir("/proc"):
        if p.isdigit():
            try:
                with open("/proc/%s/comm" % p) as f:
                    if f.read().strip() == "system_server":
                        _ss_pid[0] = int(p)
                        return _ss_pid[0]
            except OSError:
                continue
    return None


def hidden_from_android(node):
    """True only when system_server's own view of /dev/input is readable AND lacks this node (input-hide.sh, §AC).
    Any doubt (no system_server, unreadable) -> False, i.e. the node is treated as visible and grabbed at once."""
    pid = system_server_pid()
    if pid is None:
        return False
    try:
        return os.path.basename(node) not in os.listdir("/proc/%d/root/dev/input" % pid)
    except OSError:
        return False


def is_real_pogo_keyboard(node):
    dev = os.path.realpath("/sys/class/input/%s/device" % os.path.basename(node))
    try:
        with open(dev + "/id/vendor") as f:
            vid = int(f.read(), 16)
        with open(dev + "/id/product") as f:
            pid = int(f.read(), 16)
    except (OSError, ValueError):
        return False
    return vid == 0x04E8 and pid == 0xA035 and "/virtual/" not in dev


def grab(node):
    if node in held:
        return
    name = dev_name(node)
    if name.startswith(SKIP_NAME_PREFIXES):
        log("skipping %s (%s) — our own virtual device, mutter needs it" % (node, name))
        return
    # 2026-09-28 (doc 11 §AF): a node Android provably cannot see (created mid-session, hidden by input-hide.sh) needs
    # no grab from us — and grabbing it STOLE it from GNOME: mutter does hotplug new devices (fake_logind TakeDevice),
    # but our grab won the race -> EBUSY for mutter -> dead Bluetooth mouse in GNOME. So leave it: mutter takes it, or
    # (Book Cover Keyboard re-attach, §AD) pogo_ghost.py's relay does. Any doubt about visibility -> grab as before.
    if hidden_from_android(node):
        if node not in _left_to_ghost:
            _left_to_ghost.add(node)
            log("leaving %s (%s) to GNOME%s — hidden from Android" % (
                node, name, " / the pogo ghost relay" if is_real_pogo_keyboard(node) else ""))
        return
    _left_to_ghost.discard(node)
    try:
        fd = os.open(node, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as e:
        log("open %s failed: %s" % (node, e))
        return
    try:
        fcntl.ioctl(fd, EVIOCGRAB, 1)
    except OSError as e:
        os.close(fd)
        if e.errno == 16:   # EBUSY — mutter / fake_logind already hold it
            return
        log("grab %s (%s) failed: %s" % (node, dev_name(node), e))
        return
    held[node] = fd
    log("grabbed %s (%s) — Android no longer sees it" % (node, dev_name(node)))


def scan():
    for node in sorted(glob.glob("/dev/input/event*")):
        grab(node)


def release_all(_sig=None, _frm=None):
    n = len(held)
    for node, fd in list(held.items()):
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 0)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        del held[node]
    if _sig:
        log("signal %d — released %d grab(s), exiting" % (_sig, n))
        sys.exit(0)


class Inotify:
    """Minimal inotify wrapper via ctypes (python stdlib has no inotify binding)."""

    def __init__(self, path):
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so", use_errno=True)
        self.libc = libc
        self.fd = libc.inotify_init1(0x800)     # IN_NONBLOCK
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        wd = libc.inotify_add_watch(self.fd, path.encode(), IN_CREATE | IN_ATTRIB | IN_DELETE)
        if wd < 0:
            raise OSError(ctypes.get_errno(), "inotify_add_watch(%s) failed" % path)

    def read_raw(self):
        try:
            return os.read(self.fd, 4096)
        except (BlockingIOError, OSError):
            return b""


def read_events(data):
    """Best-effort (mask, name) pairs from a raw inotify read buffer — LOGGING ONLY.

    inotify names are 4-byte-aligned/padded by the kernel and parsing details differ between
    kernels, so this must never drive logic: the caller always rescans /dev/input on any
    inotify activity and uses this only to label the log line. Malformed buffer -> empty list.
    """
    out = []
    off = 0
    try:
        while off + 16 <= len(data):            # wd i, mask I, cookie I, len I
            _wd, mask, _cookie, ln = struct.unpack_from("iIII", data, off)
            off += 16
            if ln > len(data) - off:
                return out
            name = data[off:off + ln].split(b"\0")[0].decode("utf-8", "replace")
            out.append((mask, name))
            off += ((ln + 3) & ~3) or ln        # skip kernel 4-byte padding defensively
    except struct.error:
        return out
    return out


def _starttime(pid):
    """Process start time (clock ticks since boot, /proc/<pid>/stat field 22), or None."""
    try:
        with open("/proc/%s/stat" % pid) as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


# 2026-09-27 audit fix: only a gnome-shell started at/after THIS session's runner counts. A stale gnome-shell
# from an incompletely torn-down earlier session would otherwise end the wait at once and the initial grab
# could beat the new mutter's TakeDevice (EBUSY for mutter = dead touch/pen/keyboard all session).
# The supervisor passes the runner pid in JANITOR_SESSION_PID; without it the old behaviour is kept.
_SESSION_PID = os.environ.get("JANITOR_SESSION_PID", "")
_SESSION_T0 = _starttime(_SESSION_PID) if _SESSION_PID.isdigit() else None


def session_shell_alive():
    """True when a gnome-shell of THIS session exists (=/session input init has run)."""
    try:
        for p in os.listdir("/proc"):
            if not p.isdigit():
                continue
            try:
                with open("/proc/%s/comm" % p) as f:
                    if f.read().strip() != "gnome-shell":
                        continue
            except OSError:
                continue
            if _SESSION_T0 is None:
                return True
            st = _starttime(p)
            if st is not None and st >= _SESSION_T0:
                return True
    except OSError:
        return False
    return False


def wait_for_mutter():
    """Initial grab must run AFTER mutter has taken its devices (see GRACE_S comment)."""
    if GRACE_S > 0:
        log("fixed grace %.1fs (override), then initial grab" % GRACE_S)
        time.sleep(GRACE_S)
        return
    waited = 0.0
    while waited < WAIT_CAP_S:
        if session_shell_alive():
            log("gnome-shell seen after %.0fs — settling %.0fs before initial grab" % (waited, SETTLE_S))
            time.sleep(SETTLE_S)
            return
        time.sleep(1.0)
        waited += 1.0
    log("gnome-shell not seen in %.0fs — scanning anyway" % WAIT_CAP_S)


def main():
    if os.path.exists(NO_FLAG):
        log("disabled (%s present)" % NO_FLAG)
        return
    signal.signal(signal.SIGTERM, release_all)
    signal.signal(signal.SIGINT, release_all)

    wait_for_mutter()
    scan()
    log("initial: %d node(s) grabbed" % len(held))

    try:
        inot = Inotify("/dev/input")
    except OSError as e:
        inot = None
        log("inotify unavailable (%s) — relying on %.1fs rescan only" % (e, RESCAN_S))

    def drain():
        """Drain inotify; always rescan afterwards (parse is best-effort, for the log only)."""
        seen = []
        while True:
            raw = inot.read_raw()
            if not raw:
                break
            seen.extend(read_events(raw))
        for mask, name in seen:
            if mask & IN_DELETE:
                node = "/dev/input/" + name
                _left_to_ghost.discard(node)
                fd = held.pop(node, None)
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    log("node removed, grab released: %s" % node)
            else:
                log("input node event: %s — rescanning" % name)
        scan()

    last_scan = time.monotonic()
    while True:
        if inot:
            readable, _, _ = select.select([inot.fd], [], [], RESCAN_S)
            if readable:
                drain()
        else:
            time.sleep(RESCAN_S)
        if time.monotonic() - last_scan >= RESCAN_S:
            last_scan = time.monotonic()
            scan()


if __name__ == "__main__":
    main()