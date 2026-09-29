#!/usr/bin/env python3
"""fake_gdm.py — the part of GDM that gnome-shell's lock screen needs, and nothing else (2026-09-27).

gnome-shell 50.4 unlocks only through GDM: js/gdm/util.js ShellUserVerifier calls libgdm 50.3
gdm_client_open_reauthentication_channel() -> system bus org.gnome.DisplayManager.Manager
.OpenReauthenticationChannel(username) -> private D-Bus address -> org.gnome.DisplayManager.UserVerifier at
/org/gnome/DisplayManager/Session. The shell calls BeginVerificationForUser("gdm-password", user), waits for
SecretInfoQuery, answers with AnswerQuery, unlocks on VerificationComplete; a failure is Problem +
ConversationStopped, after which the shell retries (the lock screen retries forever).
There is no GDM daemon in this chroot, so this stand-in serves exactly that, checking the password against the
chroot's /etc/shadow entry for the user (root) through libcrypt — the same check pam_unix would do.

Safety:
* Only uid 0 may open a channel, only uid 0 may connect to the private server (the shell runs as root).
* Only "gdm-password"; other services are reported unavailable (no fingerprint/smartcard here).
* 2 s delay after every failed attempt (pam_faildelay-like); after 5 consecutive failures the next prompt waits
  30 s, doubling up to 5 min. Attempts are logged without the secret.
* Recovery (owner request): `fake-gdm-recovery set` prints a random 32-digit code ONCE and stores only its scrypt
  hash in /etc/fake-gdm/recovery (root 0600). Writing the code to /run/fake-gdm/recover (root-only dir) completes
  the next/current unlock conversation; wrong codes count against the same throttle.
* The runner enables the lock screen only when this service owns its name AND root has a real password hash, so a
  lock can never be armed without a way to answer it. Panic chord / session end still return to Android, whose own
  lock screen appears after the framework restart.
"""
import ctypes
import ctypes.util
import hashlib
import hmac
import os
import secrets
import sys
import time

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS_NAME = "org.gnome.DisplayManager"
MANAGER_PATH = "/org/gnome/DisplayManager/Manager"
SESSION_PATH = "/org/gnome/DisplayManager/Session"
VERIFIER_IFACE = "org.gnome.DisplayManager.UserVerifier"
VERSION = "50.3"                       # the libgdm version installed in the chroot
SERVICE = "gdm-password"
PROMPT = "Password:"
SOCKET_DIR = "/run/fake-gdm"
SHADOW = "/etc/shadow"
FAIL_DELAY_S = 2
LOCKOUT_AFTER = 5
LOCKOUT_START_S = 30
LOCKOUT_MAX_S = 300

MANAGER_XML = """
<node>
  <interface name="org.gnome.DisplayManager.Manager">
    <method name="RegisterDisplay"/>
    <method name="RegisterSession"/>
    <method name="OpenSession"><arg name="address" direction="out" type="s"/></method>
    <method name="OpenReauthenticationChannel">
      <arg name="username" direction="in" type="s"/>
      <arg name="address" direction="out" type="s"/>
    </method>
    <property name="Version" type="s" access="read"/>
  </interface>
</node>
"""

VERIFIER_XML = """
<node>
  <interface name="org.gnome.DisplayManager.UserVerifier">
    <method name="EnableExtensions"><arg name="extensions" direction="in" type="as"/></method>
    <method name="BeginVerification"><arg name="service_name" direction="in" type="s"/></method>
    <method name="BeginVerificationForUser">
      <arg name="service_name" direction="in" type="s"/>
      <arg name="username" direction="in" type="s"/>
    </method>
    <method name="AnswerQuery">
      <arg name="service_name" direction="in" type="s"/>
      <arg name="answer" direction="in" type="s"/>
    </method>
    <method name="Cancel"/>
    <signal name="ConversationStarted"><arg name="service_name" type="s"/></signal>
    <signal name="ConversationStopped"><arg name="service_name" type="s"/></signal>
    <signal name="ReauthenticationStarted"><arg name="pid_of_caller" type="i"/></signal>
    <signal name="Info"><arg name="service_name" type="s"/><arg name="info" type="s"/></signal>
    <signal name="Problem"><arg name="service_name" type="s"/><arg name="problem" type="s"/></signal>
    <signal name="InfoQuery"><arg name="service_name" type="s"/><arg name="query" type="s"/></signal>
    <signal name="SecretInfoQuery"><arg name="service_name" type="s"/><arg name="query" type="s"/></signal>
    <signal name="Reset"/>
    <signal name="ServiceUnavailable"><arg name="service_name" type="s"/><arg name="message" type="s"/></signal>
    <signal name="VerificationFailed"><arg name="service_name" type="s"/></signal>
    <signal name="VerificationComplete"><arg name="service_name" type="s"/></signal>
  </interface>
</node>
"""


def log(msg):
    print(f"[fake-gdm] {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


_libcrypt = ctypes.CDLL(ctypes.util.find_library("crypt") or "libcrypt.so.2")
_libcrypt.crypt.restype = ctypes.c_char_p
_libcrypt.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]


def shadow_hash(user):
    """The user's password hash, or None if the account has no usable password (locked/empty/missing)."""
    try:
        with open(SHADOW) as f:
            for line in f:
                fields = line.rstrip("\n").split(":")
                if len(fields) > 1 and fields[0] == user:
                    h = fields[1]
                    return h if h.startswith("$") else None
    except OSError as e:
        log(f"cannot read {SHADOW}: {e}")
    return None


def password_ok(user, answer):
    h = shadow_hash(user)
    if h is None or not answer:
        return False
    out = _libcrypt.crypt(answer.encode(), h.encode())   # the main loop is single-threaded: crypt() is safe here
    return out is not None and hmac.compare_digest(out, h.encode())


class Throttle:
    """Consecutive-failure counter shared by every channel (the lock screen reconnects between attempts)."""

    def __init__(self):
        self.failures = 0
        self.locked_until = 0.0

    def wait_seconds(self):
        return max(0.0, self.locked_until - time.monotonic())

    def failed(self):
        self.failures += 1
        if self.failures >= LOCKOUT_AFTER:
            n = self.failures - LOCKOUT_AFTER
            self.locked_until = time.monotonic() + min(LOCKOUT_START_S * (2 ** n), LOCKOUT_MAX_S)

    def succeeded(self):
        self.failures = 0
        self.locked_until = 0.0


THROTTLE = Throttle()
RECOVERY_HASH = "/etc/fake-gdm/recovery"
RECOVER_FILE = os.path.join(SOCKET_DIR, "recover")
_channels = []          # live conversations (for recovery)


def shell_unlock(how):
    """Real GDM finishes an unlock through logind (Manager.UnlockSession -> Session 'Unlock' signal), which does
    not exist here (the shell keeps LoginManagerDummy). Do what the shell's Unlock handler does:
    org.gnome.ScreenSaver.SetActive(false) on the shell (screenShield.deactivate). Audit pass A, F1.
    ASYNC and slightly delayed: the shell answers VerificationComplete with synchronous calls back into this
    process (e.g. UserVerifier.Cancel via call_cancel_sync), so a blocking call here deadlocked both sides until
    the 5 s timeout (live session 2026-09-27 17:12)."""
    def done(bus, res):
        try:
            bus.call_finish(res)
            log(f"screen unlocked ({how})")
        except GLib.Error as e:
            log(f"SetActive(false) failed ({how}): {e.message}")

    def go():
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.call("org.gnome.Shell", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver", "SetActive",
                     GLib.Variant("(b)", (False,)), None, Gio.DBusCallFlags.NONE, 10000, None, done)
        except GLib.Error as e:
            log(f"session bus unavailable ({how}): {e.message}")
        return GLib.SOURCE_REMOVE
    GLib.timeout_add(300, go)


def recovery_ok(code):
    try:
        with open(RECOVERY_HASH) as f:
            kind, n, r, p, salt, want = f.read().strip().split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(code.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(want) // 2).hex()
        return hmac.compare_digest(got, want)
    except (OSError, ValueError) as e:
        log(f"recovery check unavailable: {e}")
        return False


def on_recover_file(*_):
    try:
        with open(RECOVER_FILE) as f:
            code = "".join(ch for ch in f.read() if ch.isdigit())
    except OSError:
        return
    if not code:
        return                      # half-written file: wait for the next event
    try:
        os.unlink(RECOVER_FILE)
    except OSError:
        pass
    if THROTTLE.wait_seconds() > 0:
        log("recovery code ignored: throttled")
        return
    if len(code) != 32 or not recovery_ok(code):
        THROTTLE.failed()
        log(f"wrong recovery code (consecutive failures {THROTTLE.failures})")
        return
    THROTTLE.succeeded()
    for c in [c for c in _channels if c.active]:
        c.cancel_timers()
        c.active = False
        c.emit("VerificationComplete")
    shell_unlock("recovery code")

VERIFIER_INFO = Gio.DBusNodeInfo.new_for_xml(VERIFIER_XML).interfaces[0]
_servers = []   # keep channel servers alive


class Channel:
    """One reauthentication conversation on one private connection."""

    def __init__(self, conn, username):
        self.conn = conn
        self.username = username
        self.active = False
        self.timers = []
        conn.register_object(SESSION_PATH, VERIFIER_INFO, self.method_call, None, None)
        conn.connect("closed", lambda *_: self.closed())
        _channels.append(self)

    def closed(self):
        self.cancel_timers()
        if self in _channels:
            _channels.remove(self)

    def complete(self, how):
        self.cancel_timers()
        self.active = False
        log(f"unlock OK for {self.username} ({how})")
        self.emit("VerificationComplete")
        shell_unlock(how)

    def emit(self, name, sig="(s)", args=(SERVICE,)):
        try:
            self.conn.emit_signal(None, SESSION_PATH, VERIFIER_IFACE, name,
                                  GLib.Variant(sig, args) if sig else None)
        except GLib.Error as e:
            log(f"emit {name} failed: {e.message}")

    def later(self, seconds, fn):
        tid = None

        def run():
            if tid in self.timers:
                self.timers.remove(tid)
            fn()
            return GLib.SOURCE_REMOVE
        tid = GLib.timeout_add(int(seconds * 1000), run)
        self.timers.append(tid)

    def cancel_timers(self):
        for tid in self.timers:
            GLib.source_remove(tid)
        self.timers = []
        self.active = False

    def ask(self):
        wait = THROTTLE.wait_seconds()
        if wait > 0:
            self.emit("Info", "(ss)", (SERVICE, f"Too many failed attempts. Try again in {int(wait) + 1} seconds."))
            self.later(wait, self.ask)
            return
        self.emit("SecretInfoQuery", "(ss)", (SERVICE, PROMPT))

    def method_call(self, conn, sender, path, iface, method, params, inv):
        try:
            if method == "EnableExtensions":
                # libgdm continues without extensions on any error (gdm-client.c on_user_verifier_extensions_enabled)
                inv.return_dbus_error("org.freedesktop.DBus.Error.NotSupported", "no extensions")
            elif method in ("BeginVerification", "BeginVerificationForUser"):
                service = params.unpack()[0]
                if method == "BeginVerificationForUser":
                    self.username = params.unpack()[1]
                if service != SERVICE:
                    inv.return_dbus_error("org.gnome.DisplayManager.Session.Error.ServiceUnavailable",
                                          f"{service} is not available")
                    return
                inv.return_value(None)
                self.cancel_timers()
                self.active = True
                self.emit("ConversationStarted")
                self.later(0, self.ask)
            elif method == "AnswerQuery":
                service, answer = params.unpack()
                inv.return_value(None)
                if service != SERVICE or not self.active:
                    return
                if THROTTLE.wait_seconds() > 0:     # answer that raced a lockout: ask again later
                    self.later(0, self.ask)
                    return
                if password_ok(self.username, answer):
                    THROTTLE.succeeded()
                    self.complete("password")
                    return
                THROTTLE.failed()
                log(f"wrong password for {self.username} (consecutive failures {THROTTLE.failures})")

                def fail():
                    self.active = False
                    self.emit("Problem", "(ss)", (SERVICE, "Sorry, that didn’t work. Please try again."))
                    self.emit("ConversationStopped")
                self.later(FAIL_DELAY_S, fail)
            elif method == "Cancel":
                inv.return_value(None)
                if self.active:
                    self.cancel_timers()
                    self.emit("ConversationStopped")
            else:
                inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
        except Exception as e:   # never leave the shell waiting for a reply
            log(f"error in {method}: {e!r}")
            try:
                inv.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(e))
            except Exception:
                pass


def root_only_observer():
    obs = Gio.DBusAuthObserver.new()

    def authorize(_obs, _stream, creds):
        return creds is not None and creds.get_unix_user() == 0
    obs.connect("authorize-authenticated-peer", authorize)
    return obs


def open_channel(username):
    os.makedirs(SOCKET_DIR, mode=0o700, exist_ok=True)
    os.chmod(SOCKET_DIR, 0o700)
    path = os.path.join(SOCKET_DIR, "reauth-" + secrets.token_hex(8))
    server = Gio.DBusServer.new_sync(f"unix:path={path}", Gio.DBusServerFlags.NONE,
                                     Gio.dbus_generate_guid(), root_only_observer(), None)

    used = []

    def on_new_connection(_srv, conn):
        used.append(1)
        Channel(conn, username)
        conn.connect("closed", lambda *_: stop())
        return True

    def stop():
        server.stop()
        if server in _servers:
            _servers.remove(server)
        try:
            os.unlink(path)
        except OSError:
            pass

    server.connect("new-connection", on_new_connection)
    server.start()
    _servers.append(server)
    def expire():   # drop a channel nobody connected to within 60 s
        if not used and server in _servers:
            stop()
        return GLib.SOURCE_REMOVE
    GLib.timeout_add_seconds(60, expire)
    return server.get_client_address()


def sender_uid(conn, sender):
    reply = conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                           "GetConnectionUnixUser", GLib.Variant("(s)", (sender,)),
                           GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, -1, None)
    return reply.unpack()[0]


def manager_call(conn, sender, path, iface, method, params, inv):
    try:
        if method in ("RegisterDisplay", "RegisterSession"):
            inv.return_value(None)
        elif method == "OpenSession":
            inv.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "no login sessions here")
        elif method == "OpenReauthenticationChannel":
            (username,) = params.unpack()
            if sender_uid(conn, sender) != 0:
                inv.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied", "root only")
                return
            if shadow_hash(username) is None:   # still open the channel: the recovery code must keep working
                log(f"warning: {username} has no password set - only the recovery code can unlock")
            addr = open_channel(username)
            log(f"reauthentication channel for {username}")
            inv.return_value(GLib.Variant("(s)", (addr,)))
        else:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
    except Exception as e:
        log(f"error in {method}: {e!r}")
        try:
            inv.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(e))
        except Exception:
            pass


def manager_prop(conn, sender, path, iface, name):
    return GLib.Variant("s", VERSION) if name == "Version" else None


def main():
    info = Gio.DBusNodeInfo.new_for_xml(MANAGER_XML).interfaces[0]

    def on_bus(conn, _name):
        conn.register_object(MANAGER_PATH, info, manager_call, manager_prop, None)

    def on_name(_conn, name):
        log(f"owns {name}")

    def on_lost(_conn, name):
        log(f"could not own {name} — exiting")
        sys.exit(1)

    os.makedirs(SOCKET_DIR, mode=0o700, exist_ok=True)
    os.chmod(SOCKET_DIR, 0o700)
    mon = Gio.File.new_for_path(SOCKET_DIR).monitor_directory(Gio.FileMonitorFlags.NONE, None)
    def on_changed(_m, f, _o, ev):
        # CREATED = the CLI renamed a complete file in; CHANGES_DONE_HINT = a hand-written file was closed.
        # An empty/half-written read is ignored (not counted), audit pass A F9.
        if f.get_basename() == "recover" and ev in (Gio.FileMonitorEvent.CREATED,
                                                    Gio.FileMonitorEvent.CHANGES_DONE_HINT):
            on_recover_file()
    mon.connect("changed", on_changed)
    main.monitor = mon   # keep a reference
    Gio.bus_own_name(Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_bus, on_name, on_lost)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
