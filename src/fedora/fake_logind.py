#!/usr/bin/env python3
"""fake_logind.py — minimal org.freedesktop.login1 D-Bus shim so GNOME Shell/mutter can
acquire a session and take DRM/input device fds on a device with no systemd PID1 at all
(driver-build/02-display-session-stack.md's "Item D").

Scope pinned down 2026-09-18 night by reading mutter's actual source
(GNOME/mutter main branch, src/backends/meta-launcher.c + src/backends/native/
meta-device-pool.c) instead of guessing the D-Bus surface: mutter needs exactly
Manager.GetSessionByPID, Session.{Id,Seat,Active} (read-only properties) +
Session.{TakeControl,ReleaseControl,TakeDevice,ReleaseDevice}, and Seat.Id +
Seat.SwitchTo. Everything else in the real org.freedesktop.login1 XML (power
management, multi-session bookkeeping, PrepareForSleep, ...) is never touched by
mutter's actual calls and is intentionally NOT implemented here. One fixed,
always-active session ("c1") and seat ("seat0") — this device has exactly one
user, one seat, no VT concept, and no suspend/resume path in scope.

Confirmed via `ldd /usr/bin/mutter` (2026-09-18): mutter links libsystemd.so.0
(for the sd_session_* FILE-based checks in meta-launcher.c's other two session-
lookup fallback paths) but NOT libseat — mutter never uses libseat at all, unlike
labwc/wlroots. This daemon's GetSessionByPID path is mutter's second fallback
(meta-launcher.c's get_session_proxy(), tried after the XDG_SESSION_ID env-var
path, which we deliberately leave unset so mutter skips straight to this D-Bus
path without needing any /run/systemd/sessions/* files to exist at all).

TakeDevice resolves major:minor -> /dev path via /sys/dev/char/<maj>:<min>/uevent
(standard kernel sysfs mechanism, real device nodes untouched — no udev
namespace trickery needed here since this is a plain read + open(), not a
udev ACTION==add trigger), opens it directly (we run as root throughout this
whole project, so there's no privilege gap to bridge the way real logind
bridges one for unprivileged desktop users), and for the DRM primary node
specifically calls DRM_IOCTL_SET_MASTER — mirroring exactly what the
already-proven, already-patched seatd (Item A, driver-build/02) does for the
labwc/wlroots path. Fail-closed: if SET_MASTER fails (HWC still holds master),
this returns a real D-Bus error instead of handing back an fd we don't actually
have scanout control over — same philosophy as the seatd patch.

Standing precondition, unchanged from every prior session: HWC (and hence real
DRM master) must already be released by the session harness's existing
stop-vendor.hwcomposer-3-2 step BEFORE gnome-shell (and this daemon's first
TakeDevice call) runs. This daemon does not stop HWC itself.

STATUS 2026-09-18 night: written. Syntax/API-checked against the actual
python3-gobject-3.56.3 installed in the chroot (not guessed) before being
declared done — see this session's notes for the exact verification steps.
Chroot-internal D-Bus mechanics smoke test done (register objects, call methods
from a second process, verify property/method/fd-passing behavior) with ZERO
live-device risk (no SurfaceFlinger/HWC stop, no real DRM master ever taken
during that smoke test — see the session notes for what was and wasn't
exercised). NOT YET run as part of an actual gnome-shell session — that first
run is the real test of the DRM TakeDevice path and needs the standing live-
session safety process (user present, dmesg tailed, time-boxed).
"""
import fcntl
import os
import struct
import sys

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS_NAME = "org.freedesktop.login1"
SESSION_ID = "c1"
SEAT_ID = "seat0"
MANAGER_PATH = "/org/freedesktop/login1"
SESSION_PATH = f"/org/freedesktop/login1/session/{SESSION_ID}"
SEAT_PATH = f"/org/freedesktop/login1/seat/{SEAT_ID}"

DRM_IOCTL_SET_MASTER = 0x641E
DRM_IOCTL_DROP_MASTER = 0x641F

# 2026-09-26 audit #3: hardware buttons belong to GNOME while the session runs.
# Android's InputReader reads the same evdev nodes, so a power press also put ANDROID to
# sleep (DisplayPowerController -> backlight off -> black panel until the next wake nudge)
# and the volume rocker changed Android's stream volume on top of GNOME's. EVIOCGRAB on the
# fd we hand to mutter (SCM_RIGHTS shares the open file, so mutter keeps receiving) makes the
# kernel deliver these two devices' events to that file only. The grab ends automatically
# when the last copy of the fd closes (session end, crash, kill). Key STATE (EVIOCGKEY)
# stays readable by anyone, which is what the Android-side panic-chord watcher polls.
# Opt out: touch /usr/local/etc/no-button-grab (chroot).
EVIOCGRAB = 0x40044590                      # _IOW('E', 0x90, int)
EVIOCGNAME_256 = 0x81004506                 # _IOC(READ, 'E', 0x06, 256)
# 2026-09-27 live finding: touches on GNOME ALSO reached Android — a swipe from the bottom edge opened
# Android Recents, whose window transition waits for SurfaceFlinger (stopped) -> BLASTSync timeout ->
# SurfaceControl DEAD_OBJECT -> system_server crash, repeatedly (framework crash loop, session ended
# early). So Android gets NO user input during a session: every device mutter takes is grabbed EXCEPT
# the grip sensors (Android's Wi-Fi/radio SAR power back-off must keep seeing them) and meta_event.
# Hall sensors: libinput takes and immediately RELEASES `hall` (cover) and `hall_wacom` (pen garage), so
# a grab through TakeDevice would only last milliseconds (independent audit, 2026-09-27: Android still saw
# cover close = screen off, and pen detach = pen UI transitions). Those two are held by fake_logind itself
# for its whole lifetime (hold_switch_devices); the kernel drops the grab when the process exits.
NO_GRAB_PREFIXES = ("grip_", "meta_event")
HELD_SWITCH_DEVICES = ("hall", "hall_wacom", "hall_logical")
held_switch_fds = []

# 2026-09-27 (screen-off pass): `hall` and `hall_logical` both advertise EV_SW SW_MACHINE_COVER (code
# 0x10) for the book-cover switch (verified live via `getevent -i`: event7 "hall" -> "SW (0005):
# SW_MACHINE_COVER"; event3 "hall_logical" -> "SW (0005): SW_KEYPAD_SLIDE SW_LINEIN_INSERT
# SW_MACHINE_COVER"). First deployment watched only `hall` and never saw a real transition despite the
# user physically closing/opening the cover during a live test (2026-09-27 ~18:39) -- `hall_logical`
# was already exclusively grabbed by mutter/libinput at that point (normal TakeDevice path, since it
# wasn't yet in HELD_SWITCH_DEVICES) so it couldn't be independently probed to confirm which node
# actually fires. Now holding + watching both until one is confirmed live; harmless if both fire since
# set_screen_blank() is idempotent. Codes are the stable uapi input-event-codes.h ABI, not
# device-specific. EVIOCGSW_4 is derived with the exact same _IOC(READ,'E',nr,size) formula as
# EVIOCGNAME_256 above (0x1b/4 in place of 0x06/256; reproduces that already-verified constant when the
# formula is run backwards), used to read the switch's state once at startup so a session that begins
# with the cover already closed still blanks.
COVER_SWITCH_DEVICES = ("hall", "hall_logical")
EV_SW = 0x05
SW_MACHINE_COVER = 0x10
EVIOCGSW_4 = 0x8004451B                     # _IOC(READ, 'E', 0x1b, 4) -- 4 bytes covers every currently defined SW_* code
INPUT_EVENT = struct.Struct("qqHHi")        # 64-bit struct input_event: tv_sec, tv_usec, type, code, value

# 2026-09-27 (screenblank): the cover no longer writes /sys/class/backlight/panel/bl_power. A real panel power-down
# makes the pogo driver detach the Book Cover Keyboard (LCD_OFF on sec_input_notifier) -> Android keyboard config
# change -> WindowManager transition against the stopped SurfaceFlinger -> system_server crash loop (session #14).
# Instead: the screen-blank@fedora-tab gnome-shell extension on the SESSION bus (address inherited from the runner's
# dbus-launch) draws pure black (OLED pixels off; panel, touch and keyboard stay powered).
BLANK_RETRY_S = 2
BLANK_RETRIES = 60                           # shell/extension may not be up yet when the session starts cover-closed
_cover = {"want": None, "tries": 0, "bus": None}


NO_COVER_BLANK_FLAG = "/usr/local/etc/no-cover-blank"   # opt-out: cover events are logged but do nothing


def set_screen_blank(blank):
    # never let this take fake_logind down (it holds DRM master + input grabs; audit 2026-09-27 #5)
    try:
        if os.path.exists(NO_COVER_BLANK_FLAG):
            log(f"screen-blank: cover {'Blank' if blank else 'Unblank'} skipped ({NO_COVER_BLANK_FLAG})")
            return
        _cover["want"] = blank
        _cover["tries"] = 0
        _send_screen_blank()
    except Exception as e:
        log(f"screen-blank: {e!r}")


def _send_screen_blank():
    want = _cover["want"]
    try:
        if _cover["bus"] is None:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.set_exit_on_close(False)    # shared connection defaults to SIGTERM-on-close (audit #4)
            _cover["bus"] = bus
    except GLib.Error as e:
        log(f"screen-blank: no session bus: {e.message}")
        return

    def done(conn, res):
        try:
            conn.call_finish(res)
            log(f"screen {'BLANKED' if want else 'ON'} (book cover)")
        except GLib.Error as e:
            if _cover["want"] == want and _cover["tries"] < BLANK_RETRIES:
                _cover["tries"] += 1
                GLib.timeout_add_seconds(BLANK_RETRY_S, _retry_screen_blank, want)
            else:
                log(f"screen-blank {'Blank' if want else 'Unblank'} failed: {e.message}")
    _cover["bus"].call("org.gnome.Shell", "/org/fedoratab/ScreenBlank", "org.fedoratab.ScreenBlank",
                       "Blank" if want else "Unblank", None, None, Gio.DBusCallFlags.NONE, 3000, None, done)


def _retry_screen_blank(want):
    try:
        if _cover["want"] == want:      # a newer cover event supersedes a pending retry
            _send_screen_blank()
    except Exception as e:
        log(f"screen-blank retry: {e!r}")
    return False


def on_cover_event(fd, _condition):
    try:
        data = os.read(fd, INPUT_EVENT.size * 16)
    except OSError:
        return True
    for off in range(0, len(data) - len(data) % INPUT_EVENT.size, INPUT_EVENT.size):
        _sec, _usec, ev_type, code, value = INPUT_EVENT.unpack_from(data, off)
        if ev_type == EV_SW and code == SW_MACHINE_COVER:
            log(f"book cover {'closed' if value else 'opened'}")
            set_screen_blank(bool(value))
    return True


def hold_switch_devices():
    if os.path.exists(NO_GRAB_FLAG):
        return
    import glob
    for d in sorted(glob.glob("/sys/class/input/event*")):
        try:
            with open(d + "/device/name") as f:
                name = f.read().strip()
        except OSError:
            continue
        if name not in HELD_SWITCH_DEVICES:
            continue
        node = "/dev/input/" + os.path.basename(d)
        try:
            fd = os.open(node, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError as e:
            log(f"hold {node} ({name}): open failed: {e}")
            continue
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        except OSError as e:
            log(f"hold {node} ({name}): EVIOCGRAB failed: {e}")
            os.close(fd)
            continue
        held_switch_fds.append(fd)
        log(f"held {node} ({name}) for the session (Android no longer sees it)")
        if name in COVER_SWITCH_DEVICES:
            try:
                state = bytearray(4)
                fcntl.ioctl(fd, EVIOCGSW_4, state, True)
                closed = bool(state[SW_MACHINE_COVER // 8] & (1 << (SW_MACHINE_COVER % 8)))
                log(f"initial cover state: {'closed' if closed else 'open'}")
                if closed:
                    set_screen_blank(True)
            except OSError as e:
                log(f"EVIOCGSW {node} failed: {e}")
            GLib.io_add_watch(fd, GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_cover_event)
NO_GRAB_FLAG = "/usr/local/etc/no-button-grab"

MANAGER_XML = """
<node>
  <interface name="org.freedesktop.login1.Manager">
    <method name="GetSessionByPID">
      <arg type="u" name="pid" direction="in"/>
      <arg type="o" name="object_path" direction="out"/>
    </method>
  </interface>
</node>
"""

SESSION_XML = """
<node>
  <interface name="org.freedesktop.login1.Session">
    <property name="Id" type="s" access="read"/>
    <property name="Seat" type="(so)" access="read"/>
    <property name="Active" type="b" access="read"/>
    <method name="TakeControl">
      <arg type="b" name="force" direction="in"/>
    </method>
    <method name="ReleaseControl"/>
    <method name="TakeDevice">
      <arg type="u" name="major" direction="in"/>
      <arg type="u" name="minor" direction="in"/>
      <arg type="h" name="fd" direction="out"/>
      <arg type="b" name="inactive" direction="out"/>
    </method>
    <method name="ReleaseDevice">
      <arg type="u" name="major" direction="in"/>
      <arg type="u" name="minor" direction="in"/>
    </method>
    <method name="SetBrightness">
      <arg type="s" name="subsystem" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="u" name="brightness" direction="in"/>
    </method>
  </interface>
</node>
"""

SEAT_XML = """
<node>
  <interface name="org.freedesktop.login1.Seat">
    <property name="Id" type="s" access="read"/>
    <method name="SwitchTo">
      <arg type="u" name="vtnr" direction="in"/>
    </method>
  </interface>
</node>
"""


def log(msg):
    print(f"[fake-logind] {msg}", file=sys.stderr, flush=True)


# (major, minor) -> (fd, is_drm_master, grabbed)
open_devices = {}


def evdev_name(fd):
    import array
    buf = array.array("B", bytes(256))
    try:
        fcntl.ioctl(fd, EVIOCGNAME_256, buf, True)
    except OSError:
        return None
    return buf.tobytes().split(b"\0", 1)[0].decode("utf-8", "replace")


def maybe_grab_buttons(fd, dev_path):
    if not dev_path.startswith("/dev/input/event") or os.path.exists(NO_GRAB_FLAG):
        return False
    name = evdev_name(fd)
    if name is None or name.startswith(NO_GRAB_PREFIXES) or name in HELD_SWITCH_DEVICES:
        return False   # HELD_SWITCH_DEVICES are already grabbed by hold_switch_devices()
    try:
        fcntl.ioctl(fd, EVIOCGRAB, 1)
    except OSError as e:
        log(f"TakeDevice: EVIOCGRAB {dev_path} ({name}) failed: {e} — Android keeps seeing it")
        return False
    log(f"TakeDevice: {dev_path} ({name}) grabbed for GNOME (Android no longer sees these keys)")
    return True


def close_device(fd, is_master, grabbed):
    if grabbed:
        try:
            fcntl.ioctl(fd, EVIOCGRAB, 0)   # releases the grab even if mutter still holds a copy
        except OSError as e:
            log(f"EVIOCGRAB release failed: {e}")
    if is_master:
        try:
            fcntl.ioctl(fd, DRM_IOCTL_DROP_MASTER)
        except OSError as e:
            log(f"DRM_IOCTL_DROP_MASTER failed: {e}")
    os.close(fd)


def resolve_device_path(major, minor):
    uevent_path = f"/sys/dev/char/{major}:{minor}/uevent"
    try:
        with open(uevent_path) as f:
            for line in f:
                if line.startswith("DEVNAME="):
                    return "/dev/" + line.strip().split("=", 1)[1]
    except OSError as e:
        log(f"resolve_device_path({major}:{minor}): {e}")
    return None


def is_drm_primary_node(path):
    return path is not None and path.startswith("/dev/dri/card")


# 2026-09-26 audit #3: Session.SetBrightness (systemd-logind API, used by mutter 50.4
# meta-backlight-sysfs.c when the session proxy answers anything but UnknownMethod to its
# SetBrightness("", "", 0) probe). Without it mutter spawned `pkexec mutter-backlight-helper`
# for every applied step (auto-brightness sends up to 10 targets/s), and the slow helper's
# late sysfs change events fed gnome-shell's slider back its own stale values (P.1).
# Only the "backlight" subsystem, only a real /sys/class/backlight/<name> entry, value
# clamped to max_brightness. Each write is logged (counted, first 20 + every 200th) so a
# live test can tell GNOME's writes from Android's.
BACKLIGHT_CLASS = "/sys/class/backlight"
brightness_writes = 0


def set_brightness(subsystem, name, value):
    global brightness_writes
    if subsystem != "backlight":
        raise ValueError(f"unsupported subsystem {subsystem!r}")
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"bad device name {name!r}")
    base = os.path.join(BACKLIGHT_CLASS, name)
    if not os.path.isdir(base):
        raise ValueError(f"no backlight device {name!r}")
    with open(os.path.join(base, "max_brightness")) as f:
        maxb = int(f.read().strip())
    v = max(0, min(int(value), maxb))
    with open(os.path.join(base, "brightness"), "w") as f:
        f.write(str(v))
    brightness_writes += 1
    if brightness_writes <= 20 or brightness_writes % 200 == 0:
        log(f"SetBrightness {name}={v} (requested {value}, max {maxb}, write #{brightness_writes})")


def dbus_error(invocation, message):
    invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", message)


def manager_method_call(_conn, _sender, _path, _iface, method, params, invocation):
    if method == "GetSessionByPID":
        invocation.return_value(GLib.Variant("(o)", (SESSION_PATH,)))
        return
    invocation.return_dbus_error(
        "org.freedesktop.DBus.Error.UnknownMethod", f"Unknown method {method}"
    )


def session_get_property(_conn, _sender, _path, _iface, name):
    if name == "Id":
        return GLib.Variant("s", SESSION_ID)
    if name == "Seat":
        return GLib.Variant("(so)", (SEAT_ID, SEAT_PATH))
    if name == "Active":
        return GLib.Variant("b", True)
    return None


# 2026-09-27 audit (pass 1, B1): the system bus policy lets every user call these, and the socket is reachable
# from every chroot daemon. Only root (gnome-shell/mutter run as uid 0 here) may use the Session methods, and
# TakeDevice only hands out DRM (226) and evdev input (13) nodes.
TAKE_DEVICE_MAJORS = (13, 226)


def sender_uid(conn, sender):
    reply = conn.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "GetConnectionUnixUser", GLib.Variant("(s)", (sender,)),
        GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, -1, None,
    )
    return reply.unpack()[0]


def session_method_call(conn, sender, _path, _iface, method, params, invocation):
    try:
        uid = sender_uid(conn, sender)
    except GLib.Error as e:
        invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", f"cannot identify caller: {e}")
        return
    if uid != 0:
        log(f"refused Session.{method} from uid {uid} ({sender})")
        invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "only root may use this session")
        return

    if method == "TakeControl":
        invocation.return_value(None)
        return

    if method == "ReleaseControl":
        invocation.return_value(None)
        return

    if method == "TakeDevice":
        major, minor = params.unpack()
        if major not in TAKE_DEVICE_MAJORS:
            dbus_error(invocation, f"device {major}:{minor} is not a DRM or input device")
            return
        dev_path = resolve_device_path(major, minor)
        if dev_path is None:
            dbus_error(invocation, f"no device for {major}:{minor}")
            return

        try:
            # O_NONBLOCK matters a lot here: verified against real systemd
            # (logind-session-device.c: "open(sd->node, O_RDWR|O_CLOEXEC|
            # O_NOCTTY|O_NONBLOCK)") after mutter's own input thread was
            # found live-blocked in a plain read() on a real evdev fd
            # returned from this exact call (2026-09-19 morning, strace).
            # SCM_RIGHTS-transferred fds share the sender's file status
            # flags, so omitting O_NONBLOCK here means every fd this method
            # hands out is blocking on the receiving side too, regardless of
            # anything the caller (mutter) does afterward.
            fd = os.open(dev_path, os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            dbus_error(invocation, f"open({dev_path}) failed: {e}")
            return

        # A repeat TakeDevice for the same device (re-add after hotplug) replaces the entry. Release the old
        # one FIRST: its grab would make EVIOCGRAB on the new fd fail with EBUSY, and its DRM master would
        # make SET_MASTER below fail (audit pass 1, B4).
        old = open_devices.pop((major, minor), None)
        if old is not None:
            close_device(*old)

        is_master = False
        if is_drm_primary_node(dev_path):
            try:
                fcntl.ioctl(fd, DRM_IOCTL_SET_MASTER)
                is_master = True
                log(f"TakeDevice: {dev_path} DRM_IOCTL_SET_MASTER OK")
            except OSError as e:
                os.close(fd)
                dbus_error(
                    invocation,
                    f"DRM_IOCTL_SET_MASTER failed on {dev_path} "
                    f"(is HWC still holding master?): {e}",
                )
                return

        # We keep our own fd open (server-side, matches real logind's own
        # session-device.c behavior) — Gio.UnixFDList.append() dup()s it for
        # the outgoing message, so ours stays valid for ReleaseDevice later.
        grabbed = maybe_grab_buttons(fd, dev_path)
        open_devices[(major, minor)] = (fd, is_master, grabbed)

        fd_list = Gio.UnixFDList.new()
        idx = fd_list.append(fd)
        log(f"TakeDevice: {dev_path} major={major} minor={minor} fd={fd} "
            f"-> handle_index={idx} master={is_master}")
        invocation.return_value_with_unix_fd_list(
            GLib.Variant("(hb)", (idx, False)), fd_list
        )
        return

    if method == "SetBrightness":
        subsystem, name, value = params.unpack()
        try:
            set_brightness(subsystem, name, value)
        except (ValueError, OSError) as e:
            # NOT UnknownMethod: mutter treats only UnknownMethod as "unsupported"
            invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs", str(e))
            return
        invocation.return_value(None)
        return

    if method == "ReleaseDevice":
        major, minor = params.unpack()
        entry = open_devices.pop((major, minor), None)
        if entry is None:
            invocation.return_value(None)
            return
        fd, is_master, grabbed = entry
        close_device(fd, is_master, grabbed)
        log(f"ReleaseDevice: major={major} minor={minor} released")
        invocation.return_value(None)
        return

    invocation.return_dbus_error(
        "org.freedesktop.DBus.Error.UnknownMethod", f"Unknown method {method}"
    )


def seat_get_property(_conn, _sender, _path, _iface, name):
    if name == "Id":
        return GLib.Variant("s", SEAT_ID)
    return None


def seat_method_call(_conn, _sender, _path, _iface, method, params, invocation):
    if method == "SwitchTo":
        # No VT concept on this device (no framebuffer console) — no-op success.
        invocation.return_value(None)
        return
    invocation.return_dbus_error(
        "org.freedesktop.DBus.Error.UnknownMethod", f"Unknown method {method}"
    )


# ---------------- org.freedesktop.RealtimeKit1 (added 2026-09-25) ----------------
# /data is mounted nosuid, so the system bus can never activate the real rtkit-daemon
# ("The permission of the setuid helper is not correct", 303x in one session). Without it
# mutter's KMS thread runs at nice 0 next to llvmpipe/Firefox render threads while mutter
# still arms its vblank deadline timer (meta-kms-impl-device.c) -> late commits, judder.
# We run as root, so apply the same bounded requests rtkit would: nice >= MIN_NICE, and
# SCHED_RR|SCHED_RESET_ON_FORK <= RT_MAX_PRIO only for processes that set RLIMIT_RTTIME
# (rtkit's own precondition: the kernel then kills a runaway RT thread).
RTKIT_BUS_NAME = "org.freedesktop.RealtimeKit1"
RTKIT_PATH = "/org/freedesktop/RealtimeKit1"
RT_MAX_PRIO = 10          # below rtkit's 20: Android's own RT threads keep headroom
MIN_NICE = -15            # rtkit default; mutter asks for exactly this
RTTIME_USEC_MAX = 200000  # rtkit default

RTKIT_XML = """
<node>
  <interface name="org.freedesktop.RealtimeKit1">
    <method name="MakeThreadRealtime">
      <arg name="thread" type="t" direction="in"/>
      <arg name="priority" type="u" direction="in"/>
    </method>
    <method name="MakeThreadRealtimeWithPID">
      <arg name="process" type="t" direction="in"/>
      <arg name="thread" type="t" direction="in"/>
      <arg name="priority" type="u" direction="in"/>
    </method>
    <method name="MakeThreadHighPriority">
      <arg name="thread" type="t" direction="in"/>
      <arg name="priority" type="i" direction="in"/>
    </method>
    <method name="MakeThreadHighPriorityWithPID">
      <arg name="process" type="t" direction="in"/>
      <arg name="thread" type="t" direction="in"/>
      <arg name="priority" type="i" direction="in"/>
    </method>
    <method name="ResetKnown"/>
    <method name="ResetAll"/>
    <method name="Exit"/>
    <property name="RTTimeUSecMax" type="x" access="read"/>
    <property name="MaxRealtimePriority" type="i" access="read"/>
    <property name="MinNiceLevel" type="i" access="read"/>
  </interface>
</node>
"""


def sender_pid(conn, sender):
    reply = conn.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        "GetConnectionUnixProcessID", GLib.Variant("(s)", (sender,)),
        GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, -1, None,
    )
    return reply.unpack()[0]


def rttime_limited(pid):
    """True if the process's soft RLIMIT_RTTIME is set and <= RTTIME_USEC_MAX."""
    try:
        with open(f"/proc/{pid}/limits") as f:
            for line in f:
                if line.startswith("Max realtime timeout"):
                    soft = line.split()[3]
                    return soft != "unlimited" and int(soft) <= RTTIME_USEC_MAX
    except (OSError, ValueError, IndexError):
        pass
    return False


def same_root(pid):
    """True if pid lives in this chroot (its root directory is our root directory)."""
    try:
        a = os.stat(f"/proc/{pid}/root/")
        b = os.stat("/")
        return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    except OSError:
        return False


def rtkit_method_call(conn, sender, _path, _iface, method, params, invocation):
    try:
        if method in ("ResetKnown", "ResetAll", "Exit"):
            invocation.return_value(None)
            return
        args = params.unpack()
        if method.endswith("WithPID"):
            pid, tid, prio = args
        else:
            pid = sender_pid(conn, sender)
            tid, prio = args
        if not os.path.isdir(f"/proc/{pid}/task/{tid}"):
            dbus_error(invocation, f"thread {tid} is not part of process {pid}")
            return
        # 2026-09-27 audit (pass 1, B2): like rtkit, only for the caller's own processes (root may ask for
        # any), and never for anything outside this chroot (Android system_server etc.).
        uid = sender_uid(conn, sender)
        if uid != 0 and os.stat(f"/proc/{pid}").st_uid != uid:
            dbus_error(invocation, f"process {pid} does not belong to uid {uid}")
            return
        if not same_root(pid):
            dbus_error(invocation, f"process {pid} is not part of the Fedora session")
            return
        if method.startswith("MakeThreadHighPriority"):
            nice = min(max(int(prio), MIN_NICE), 0)
            os.setpriority(os.PRIO_PROCESS, tid, nice)
            log(f"rtkit: pid {pid} tid {tid} nice {nice}")
        else:
            if not rttime_limited(pid):
                dbus_error(invocation, "RLIMIT_RTTIME not set (required for realtime)")
                return
            rt = min(max(int(prio), 1), RT_MAX_PRIO)
            os.sched_setscheduler(tid, os.SCHED_RR | os.SCHED_RESET_ON_FORK, os.sched_param(rt))
            log(f"rtkit: pid {pid} tid {tid} SCHED_RR {rt}")
        invocation.return_value(None)
    except Exception as e:  # never let a bad request take down logind
        dbus_error(invocation, f"{method} failed: {e}")


def rtkit_get_property(_conn, _sender, _path, _iface, name):
    if name == "RTTimeUSecMax":
        return GLib.Variant("x", RTTIME_USEC_MAX)
    if name == "MaxRealtimePriority":
        return GLib.Variant("i", RT_MAX_PRIO)
    if name == "MinNiceLevel":
        return GLib.Variant("i", MIN_NICE)
    return None


def on_rtkit_bus_acquired(connection, _name):
    info = Gio.DBusNodeInfo.new_for_xml(RTKIT_XML).interfaces[0]
    connection.register_object(RTKIT_PATH, info, rtkit_method_call, rtkit_get_property, None)
    log(f"registered {RTKIT_PATH}")


def on_rtkit_name_lost(_connection, name):
    # Not fatal: logind is the essential service; mutter just stays at normal priority.
    log(f"could not own {name}; threads stay normally scheduled")


def on_bus_acquired(connection, _name):
    manager_info = Gio.DBusNodeInfo.new_for_xml(MANAGER_XML).interfaces[0]
    session_info = Gio.DBusNodeInfo.new_for_xml(SESSION_XML).interfaces[0]
    seat_info = Gio.DBusNodeInfo.new_for_xml(SEAT_XML).interfaces[0]

    connection.register_object(MANAGER_PATH, manager_info, manager_method_call, None, None)
    connection.register_object(
        SESSION_PATH, session_info, session_method_call, session_get_property, None
    )
    connection.register_object(SEAT_PATH, seat_info, seat_method_call, seat_get_property, None)
    log(f"registered Manager={MANAGER_PATH} Session={SESSION_PATH} Seat={SEAT_PATH}")


def on_name_acquired(_connection, name):
    log(f"acquired bus name {name}")


def on_name_lost(_connection, name):
    log(f"LOST bus name {name} (another org.freedesktop.login1 owner already running?) — exiting")
    sys.exit(1)


def main():
    hold_switch_devices()
    Gio.bus_own_name(
        Gio.BusType.SYSTEM,
        BUS_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_bus_acquired,
        on_name_acquired,
        on_name_lost,
    )
    Gio.bus_own_name(
        Gio.BusType.SYSTEM,
        RTKIT_BUS_NAME,
        Gio.BusNameOwnerFlags.NONE,
        on_rtkit_bus_acquired,
        None,
        on_rtkit_name_lost,
    )
    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
