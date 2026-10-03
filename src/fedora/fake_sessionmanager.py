#!/usr/bin/env python3
"""fake_sessionmanager.py — minimal stand-in for org.gnome.SessionManager (gnome-session) on the session bus.

WHY: this Fedora chroot runs gnome-shell directly (no gnome-session). gnome-settings-daemon plugins depend on the session manager:
gsd-power derives `session_is_active` ONLY from the SessionManager property `SessionIsActive` (gsd 50.1
plugins/power/gsd-power-manager.c is_session_active(), line ~1835). Without this service it stays FALSE forever, so
gsd-power never claims the light sensor => auto-brightness never runs, idle/dim logic is also gated. Every gsd plugin also
logs "Unable to register client" because RegisterClient has no answer.

Interface copied from gnome-session 50.1 (installed version) gnome-session/org.gnome.SessionManager.xml.

SAFETY: this service can NEVER power off, reboot or suspend the tablet: Reboot/Suspend/SetRebootToFirmwareSetup return a
D-Bus error, CanShutdown/CanReboot/CanSuspend report 0. Inhibit just hands out cookies (nothing is actually inhibited).

2026-09-26 audit #3 — "Log Out" returns to Android: Logout(0) and Shutdown() (GNOME's power-button "interactive" action,
gsd 50.1 gnome_session_shutdown) open gnome-shell's own end-session dialog (org.gnome.SessionManager.EndSessionDialog.Open,
type 0 = "Log Out", 60 s) exactly like gnome-session does. From the dialog only an EXPLICIT confirm ends the session:
gnome-shell 50.4 auto-confirms when the countdown runs out (endSessionDialog.js _startTimer), so a confirm arriving at/after
the countdown is treated as Cancel and the dialog is closed with EndSessionDialog.Close (it would otherwise stay faded out
with an input-blocking lightbox) — an accidental power press can never end the session. Dialog signals are accepted only
from org.gnome.Shell. Logout(1|2) is gnome-session's "no confirmation" mode and ends the session at once (nothing in GNOME
calls it from the power button or the menu; the menu's Log Out uses mode 0).
Ending = SIGTERM to gnome-shell (mutter exits cleanly, drops DRM master); the runner then exits and the Android-side
supervisor runs its normal restore (HWC, SurfaceFlinger, zygote, Wi-Fi). SIGKILL after 8 s if it is still alive.
Inhibitors/clients of a caller are dropped when that caller leaves the bus (no leak across app restarts).
Env (tests): SESSIONMANAGER_BUS=session is the default and only bus used.
"""
import itertools
import os
import signal
import sys
import time

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS_NAME = "org.gnome.SessionManager"
PATH = "/org/gnome/SessionManager"
PRESENCE_PATH = "/org/gnome/SessionManager/Presence"

XML = """
<node>
  <interface name="org.gnome.SessionManager">
    <method name="Setenv"><arg name="variable" type="s" direction="in"/><arg name="value" type="s" direction="in"/></method>
    <method name="GetLocale"><arg name="category" type="i" direction="in"/><arg name="value" type="s" direction="out"/></method>
    <method name="InitializationError"><arg name="message" type="s" direction="in"/><arg name="fatal" type="b" direction="in"/></method>
    <method name="Initialized"/>
    <method name="RegisterClient"><arg type="s" name="app_id" direction="in"/><arg type="s" name="ignored" direction="in"/><arg type="o" name="client_id" direction="out"/></method>
    <method name="UnregisterClient"><arg type="o" name="client_id" direction="in"/></method>
    <method name="Inhibit"><arg type="s" name="app_id" direction="in"/><arg type="u" name="ignored" direction="in"/><arg type="s" name="reason" direction="in"/><arg type="u" name="flags" direction="in"/><arg type="u" name="inhibit_cookie" direction="out"/></method>
    <method name="Uninhibit"><arg type="u" name="inhibit_cookie" direction="in"/></method>
    <method name="IsInhibited"><arg type="u" name="flags" direction="in"/><arg type="b" name="is_inhibited" direction="out"/></method>
    <method name="GetInhibitors"><arg name="inhibitors" direction="out" type="ao"/></method>
    <method name="Shutdown"/>
    <method name="Reboot"/>
    <method name="Suspend"/>
    <method name="CanShutdown"><arg name="availability" direction="out" type="u"/></method>
    <method name="CanReboot"><arg name="availability" direction="out" type="u"/></method>
    <method name="CanSuspend"><arg name="availability" direction="out" type="u"/></method>
    <method name="SetRebootToFirmwareSetup"><arg name="enable" direction="in" type="b"/></method>
    <method name="CanRebootToFirmwareSetup"><arg name="is_available" direction="out" type="b"/></method>
    <method name="Logout"><arg name="mode" type="u" direction="in"/></method>
    <method name="IsSessionRunning"><arg name="running" direction="out" type="b"/></method>
    <signal name="ClientAdded"><arg name="id" type="o"/></signal>
    <signal name="ClientRemoved"><arg name="id" type="o"/></signal>
    <signal name="InhibitorAdded"><arg name="id" type="o"/></signal>
    <signal name="InhibitorRemoved"><arg name="id" type="o"/></signal>
    <signal name="SessionRunning"/>
    <signal name="SessionOver"/>
    <property name="SessionName" type="s" access="read"/>
    <property name="Renderer" type="s" access="read"/>
    <property name="SessionIsActive" type="b" access="read"/>
    <property name="InhibitedActions" type="u" access="read"/>
    <property name="RestoreSupported" type="b" access="read"/>
  </interface>
</node>
"""
PRESENCE_XML = """
<node>
  <interface name="org.gnome.SessionManager.Presence">
    <method name="SetStatus"><arg type="u" name="status" direction="in"/></method>
    <method name="SetStatusText"><arg type="s" name="status_text" direction="in"/></method>
    <property name="status" type="u" access="readwrite"/>
    <property name="status-text" type="s" access="readwrite"/>
    <signal name="StatusChanged"><arg type="u" name="status"/></signal>
    <signal name="StatusTextChanged"><arg type="s" name="status_text"/></signal>
  </interface>
</node>
"""
CLIENT_XML = """
<node>
  <interface name="org.gnome.SessionManager.ClientPrivate">
    <method name="EndSessionResponse"><arg type="b" name="is_ok" direction="in"/><arg type="s" name="reason" direction="in"/></method>
    <signal name="Stop"/>
    <signal name="QueryEndSession"><arg type="u" name="flags"/></signal>
    <signal name="EndSession"><arg type="u" name="flags"/></signal>
    <signal name="CancelEndSession"/>
  </interface>
</node>
"""

_clients = itertools.count(1)
_cookies = itertools.count(1)
_inhibitors = {}            # cookie -> (sender, app_id, reason, flags)
_registered_clients = {}    # object path -> (registration id, sender)
presence = {"status": 0, "status-text": ""}
DIALOG_SECONDS = 60
_dialog = {"open_at": None}  # monotonic time the end-session dialog was opened
_conn = None


def log(msg):
    print("[fake-sessionmanager] %s" % msg, flush=True)


def gnome_shell_pids():
    pids = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % d) as f:
                if f.read().strip() != "gnome-shell":
                    continue
            with open("/proc/%s/stat" % d) as f:
                if f.read().rsplit(")", 1)[1].split()[0] == "Z":
                    continue  # already exited, just not reaped yet
            pids.append(int(d))
        except (OSError, IndexError):
            pass
    return pids


def end_session(reason):
    """Return to Android: stop gnome-shell; the supervisor's normal restore does the rest."""
    pids = gnome_shell_pids()
    log("ending session (%s): SIGTERM gnome-shell %s" % (reason, pids))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            log("SIGTERM %d failed: %r" % (pid, e))

    def force():
        alive = set(gnome_shell_pids())
        for pid in [p for p in pids if p in alive]:   # only the processes signalled above
            log("gnome-shell %d still alive after 8 s: SIGKILL" % pid)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return False
    GLib.timeout_add_seconds(8, force)


# 2026-09-27: with a lock screen armed (runner creates LOCK_READY only when fake_gdm runs and root has a password),
# the POWER BUTTON locks the screen instead of offering Log Out. The shell's own Power Off / Log Out entries (sender =
# org.gnome.Shell) still get the dialog.
LOCK_READY = "/run/fedora-lock-ready"

# 2026-09-27 (screen-off pass, user request): the lock screen still draws a clock + wallpaper -- "text" the user
# explicitly wants avoided on this OLED panel for a plain power-button tap. The hardware button now toggles the
# panel's own bl_power (real panel off, nothing composited/drawn) instead of locking or opening the Log Out dialog;
# LOCK_READY/lock_screen() are left in place for if/when a password lock is armed and wanted again on its own trigger.
# 2026-09-27 (screenblank): NOT bl_power any more. A real panel power-down makes the pogo driver detach the Book Cover
# Keyboard (LCD_OFF on sec_input_notifier) -> Android keyboard config change -> WindowManager transition against the
# stopped SurfaceFlinger -> system_server crash loop (session #14, 19:04:59). The screen-blank@fedora-tab shell
# extension draws pure black instead (OLED pixels off, panel stays powered). Debounced: one physical press = one toggle.
BLANK_DEBOUNCE_S = 0.5
_last_toggle = [0.0]


def toggle_screen_blank():
    now = time.monotonic()
    if now - _last_toggle[0] < BLANK_DEBOUNCE_S:
        log("power button: ignored (%.2f s after the last toggle)" % (now - _last_toggle[0]))
        return
    _last_toggle[0] = now

    def done(conn, res):
        try:
            blanked = conn.call_finish(res).unpack()[0]
            log("screen %s (power button)" % ("BLANKED" if blanked else "ON"))
        except GLib.Error as e:
            log("screen-blank Toggle failed (extension not loaded?): %s" % e.message)
    _conn.call("org.gnome.Shell", "/org/fedoratab/ScreenBlank", "org.fedoratab.ScreenBlank", "Toggle", None,
               GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 3000, None, done)


def sender_is_shell(sender):
    try:
        owner = _conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                "GetNameOwner", GLib.Variant("(s)", ("org.gnome.Shell",)),
                                GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
        return owner == sender
    except GLib.Error:
        return False


def lock_screen():
    def done(conn, res):
        try:
            conn.call_finish(res)
        except GLib.Error as e:
            log("ScreenSaver.Lock failed (%s): showing the Log Out dialog instead" % e.message)
            open_end_session_dialog()
    _conn.call("org.gnome.Shell", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver", "Lock", None, None,
               Gio.DBusCallFlags.NONE, 10000, None, done)
    log("power button: locking the screen")
    try:
        with open("/sys/class/timed_output/vibrator/enable", "w") as f:
            f.write("30")
    except OSError:
        pass


def open_end_session_dialog():
    if _dialog["open_at"] is not None and time.monotonic() - _dialog["open_at"] < DIALOG_SECONDS + 5:
        return  # already showing
    _dialog["open_at"] = time.monotonic()

    def done(conn, res):
        try:
            conn.call_finish(res)
        except GLib.Error as e:
            log("EndSessionDialog.Open failed: %s" % e.message)
            _dialog["open_at"] = None
    _conn.call("org.gnome.Shell", "/org/gnome/SessionManager/EndSessionDialog",
               "org.gnome.SessionManager.EndSessionDialog", "Open",
               GLib.Variant("(uuuao)", (0, 0, DIALOG_SECONDS, [])), None,
               Gio.DBusCallFlags.NONE, -1, None, done)
    log("end-session dialog opened (Log Out, %d s; countdown expiry = cancel)" % DIALOG_SECONDS)


def on_dialog_signal(conn, sender, path, iface, signal_name, params):
    opened = _dialog["open_at"]
    if signal_name in ("Canceled", "Closed"):
        if _dialog["open_at"] is not None:
            log("end-session dialog %s" % signal_name.lower())
        _dialog["open_at"] = None
        return
    if not signal_name.startswith("Confirmed"):
        return
    _dialog["open_at"] = None
    if opened is None:
        log("%s without an open dialog — ignored" % signal_name)
        return
    elapsed = time.monotonic() - opened
    if elapsed >= DIALOG_SECONDS - 1.5:
        log("%s after %.1f s = countdown expiry, treated as CANCEL (session kept)" % (signal_name, elapsed))
        # the auto-confirmed dialog is only faded out and still blocks input: close it
        _conn.call("org.gnome.Shell", "/org/gnome/SessionManager/EndSessionDialog",
                   "org.gnome.SessionManager.EndSessionDialog", "Close", None, None,
                   Gio.DBusCallFlags.NONE, -1, None, None)
        return
    end_session("%s by the user after %.1f s" % (signal_name, elapsed))


def on_name_owner_changed(conn, sender, path, iface, signal_name, params):
    name, old, new = params.unpack()
    if new or not name.startswith(":"):
        return
    gone = [c for c, v in _inhibitors.items() if v[0] == name]
    for c in gone:
        del _inhibitors[c]
    for cid in [c for c, v in _registered_clients.items() if v[1] == name]:
        reg, _ = _registered_clients.pop(cid)
        conn.unregister_object(reg)
    if gone:
        log("dropped %d inhibitor(s) of departed %s" % (len(gone), name))


def get_property(conn, sender, path, iface, name):
    if iface == "org.gnome.SessionManager":
        return {
            "SessionName": GLib.Variant("s", "gnome"),
            "Renderer": GLib.Variant("s", ""),
            "SessionIsActive": GLib.Variant("b", True),
            "InhibitedActions": GLib.Variant("u", 0),
            "RestoreSupported": GLib.Variant("b", False),
        }.get(name)
    if iface == "org.gnome.SessionManager.Presence":
        if name == "status":
            return GLib.Variant("u", presence["status"])
        if name == "status-text":
            return GLib.Variant("s", presence["status-text"])
    return None


def set_property(conn, sender, path, iface, name, value):
    if iface == "org.gnome.SessionManager.Presence" and name in presence:
        presence[name] = value.unpack()
        return True
    return False


def method_call(conn, sender, path, iface, method, params, inv):
    try:
        args = params.unpack() if params is not None else ()
        if method == "RegisterClient":
            cid = "/org/gnome/SessionManager/Client%d" % next(_clients)
            info = Gio.DBusNodeInfo.new_for_xml(CLIENT_XML).interfaces[0]
            reg = conn.register_object(cid, info, client_call, None, None)
            _registered_clients[cid] = (reg, sender)
            inv.return_value(GLib.Variant("(o)", (cid,)))
        elif method == "UnregisterClient":
            entry = _registered_clients.pop(args[0], None)
            if entry:
                conn.unregister_object(entry[0])
            inv.return_value(None)
        elif method == "Inhibit":
            cookie = next(_cookies)
            _inhibitors[cookie] = (sender, args[0], args[2], args[3])
            inv.return_value(GLib.Variant("(u)", (cookie,)))
        elif method == "Uninhibit":
            _inhibitors.pop(args[0], None)
            inv.return_value(None)
        elif method == "IsInhibited":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "GetInhibitors":
            inv.return_value(GLib.Variant("(ao)", ([],)))
        elif method == "GetLocale":
            inv.return_value(GLib.Variant("(s)", ("C.UTF-8",)))
        elif method in ("Setenv", "InitializationError", "Initialized", "DeletedInstanceIds", "SetStatus", "SetStatusText"):
            if method == "SetStatus":
                presence["status"] = args[0]
            elif method == "SetStatusText":
                presence["status-text"] = args[0]
            inv.return_value(None)
        elif method in ("CanShutdown", "CanReboot", "CanSuspend"):
            inv.return_value(GLib.Variant("(u)", (0,)))
        elif method == "CanRebootToFirmwareSetup":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "IsSessionRunning":
            inv.return_value(GLib.Variant("(b)", (True,)))
        elif method == "Logout":
            mode = args[0]
            inv.return_value(None)
            if mode == 0:
                open_end_session_dialog()
            else:  # 1 = no confirmation, 2 = force (gnome-session semantics)
                end_session("Logout(mode=%d) from %s" % (mode, sender))
        elif method == "Shutdown":
            # shell's own Power Off menu entry: still offers Log Out (deliberate UI action, dialog expected).
            # Hardware power button (sender != shell): black screen on/off (screen-blank extension), no dialog, no text.
            inv.return_value(None)
            if sender_is_shell(sender):
                open_end_session_dialog()
            else:
                toggle_screen_blank()
        elif method in ("Reboot", "Suspend", "SetRebootToFirmwareSetup"):
            log("refused %s from %s" % (method, sender))
            inv.return_dbus_error("org.gnome.SessionManager.Error.NotSupported", "%s is not supported here" % method)
        else:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
    except Exception as e:  # never leave a caller waiting for a reply
        log("error in %s: %r" % (method, e))
        try:
            inv.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(e))
        except Exception:
            pass


def client_call(conn, sender, path, iface, method, params, inv):
    inv.return_value(None)          # EndSessionResponse: nothing to do


def on_bus(connection, name):
    global _conn
    _conn = connection
    connection.signal_subscribe("org.gnome.Shell", "org.gnome.SessionManager.EndSessionDialog", None,
                                "/org/gnome/SessionManager/EndSessionDialog", None,
                                Gio.DBusSignalFlags.NONE, on_dialog_signal)
    connection.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                                "/org/freedesktop/DBus", None, Gio.DBusSignalFlags.NONE, on_name_owner_changed)
    main_info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
    pres_info = Gio.DBusNodeInfo.new_for_xml(PRESENCE_XML).interfaces[0]
    connection.register_object(PATH, main_info, method_call, get_property, None)
    connection.register_object(PRESENCE_PATH, pres_info, method_call, get_property, set_property)
    log("registered %s on the session bus (SessionIsActive=true)" % name)


def on_lost(connection, name):
    log("lost/could not own %s — exiting" % name)
    sys.exit(1)


# 2026-09-27 (audit pass A, F2): org.gnome.ScreenSaver stand-in. Normally a gjs D-Bus-activated service owns this
# name and forwards the shell's ActiveChanged; gsd-power reacts to ActiveChanged(true) by BLANKING the panel (KMS
# DPMS off — an untested path on this device). This stand-in is owned before any gsd daemon starts, forwards
# Lock / GetActive / SetActive(true) to the shell, never emits ActiveChanged, and does not forward SetActive(false)
# (unlocking goes only through fake_gdm).
SS_XML = """
<node>
  <interface name="org.gnome.ScreenSaver">
    <method name="Lock"/>
    <method name="GetActive"><arg type="b" direction="out"/></method>
    <method name="SetActive"><arg type="b" direction="in"/></method>
    <method name="GetActiveTime"><arg type="u" direction="out"/></method>
    <signal name="ActiveChanged"><arg type="b"/></signal>
    <signal name="WakeUpScreen"/>
  </interface>
</node>
"""


def ss_call(conn, sender, path, iface, method, params, inv):
    def fwd(name, args, rtype, done):
        def cb(c, res):
            try:
                done(c.call_finish(res))
            except GLib.Error as e:
                inv.return_dbus_error("org.freedesktop.DBus.Error.Failed", e.message)
        conn.call("org.gnome.Shell", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver", name, args,
                  rtype, Gio.DBusCallFlags.NONE, 10000, None, cb)
    if method == "Lock":
        fwd("Lock", None, None, lambda r: inv.return_value(None))
    elif method == "GetActive":
        fwd("GetActive", None, GLib.VariantType.new("(b)"), lambda r: inv.return_value(r))
    elif method == "SetActive":
        (active,) = params.unpack()
        if active:
            fwd("SetActive", GLib.Variant("(b)", (True,)), None, lambda r: inv.return_value(None))
        else:
            inv.return_value(None)       # never unlocks from here
    elif method == "GetActiveTime":
        inv.return_value(GLib.Variant("(u)", (0,)))
    else:
        inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)


def on_ss_bus(connection, name):
    info = Gio.DBusNodeInfo.new_for_xml(SS_XML).interfaces[0]
    connection.register_object("/org/gnome/ScreenSaver", info, ss_call, None, None)
    log("owns org.gnome.ScreenSaver (no ActiveChanged -> gsd-power never blanks the panel on lock)")


def main():
    Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_bus, None, on_lost)
    Gio.bus_own_name(Gio.BusType.SESSION, "org.gnome.ScreenSaver", Gio.BusNameOwnerFlags.NONE, on_ss_bus, None,
                     lambda c, n: log("could not own org.gnome.ScreenSaver (already owned) - gsd-power may blank on lock"))
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
