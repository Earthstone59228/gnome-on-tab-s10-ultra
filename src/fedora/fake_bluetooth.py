#!/usr/bin/env python3
"""fake_bluetooth.py — GNOME's Bluetooth UI (quick-settings toggle, Settings > Bluetooth, top-bar indicator) backed
by ANDROID's Bluetooth, on the Galaxy Tab S10 Ultra Fedora session (2026-09-27).

WHY: Android owns the Bluetooth controller; BlueZ must never run here (kernel-panic hazard, doc 00 hard rules), and
gsd-rfkill is deliberately not started (it writes radio state; Android airplane mode flipped once). Without both,
gnome-shell hides its Bluetooth toggle (bluetooth.js `available` = Rfkill BluetoothHasAirplaneMode) and lists no
devices. This daemon serves the two D-Bus APIs GNOME reads, with Android as the only source of truth:

  system bus  org.bluez                       (interfaces pinned to gnome-bluetooth 47.2 lib/bluetooth-client.xml)
      /                        org.freedesktop.DBus.ObjectManager
      /org/bluez               org.bluez.AgentManager1   (accepted, no-op: pairing happens in Android)
      /org/bluez/hci0          org.bluez.Adapter1        Powered <-> Android Bluetooth on/off
      /org/bluez/hci0/dev_*    org.bluez.Device1         Android's bonded devices, Paired=True, live Connected
  session bus org.gnome.SettingsDaemon.Rfkill (interface pinned to gnome-shell 50.4 data/dbus-interfaces)
      BluetoothHasAirplaneMode=True, BluetoothAirplaneMode = !Android-BT-on; airplane mode itself is NOT offered
      (HasAirplaneMode=False) and never touched.

Android side = android-bridge.sh whitelisted requests only: bt-list (read-only dumpsys), bt-on / bt-off (the same
`cmd bluetooth_manager enable|disable` the android-bt app already uses). Nothing here opens /dev/rfkill, starts
bluetoothd, or touches Android settings.
LIMITS: Connect/Disconnect/Pair of a specific device are NOT possible from Android's shell (`cmd bluetooth_manager` has
only enable/disable) -> org.bluez.Error.NotSupported; connect/pair in Android, GNOME shows the live state. Samsung's
dumpsys masks the first 4 MAC bytes, so addresses show as XX:XX:XX:XX:ab:cd.
Opt-out: /usr/local/etc/no-fake-bluetooth (checked by the runner). Log: /fake-bluetooth.log
Env (tests only): FAKE_BT_BUS=session puts org.bluez on the session bus; FAKE_BT_BRIDGE=<file.py> replaces android-bt.
"""
import importlib.machinery
import importlib.util
import os
import queue
import sys
import threading

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

POLL_S = 10             # steady-state poll (one dumpsys, ~140 ms Android-side)
FAST_POLL_S = 1.5       # after a toggle, until the state settles or FAST_WINDOW_S passes
FAST_WINDOW_S = 15
HIDDEN_NAMES = ("SPEN",)   # Samsung's internal S Pen LE link: always "connected", not a user device
ADAPTER = "/org/bluez/hci0"

BLUEZ_XML = """
<node>
  <interface name="org.freedesktop.DBus.ObjectManager">
    <method name="GetManagedObjects"><arg name="objects" type="a{oa{sa{sv}}}" direction="out"/></method>
    <signal name="InterfacesAdded"><arg name="object" type="o"/><arg name="interfaces" type="a{sa{sv}}"/></signal>
    <signal name="InterfacesRemoved"><arg name="object" type="o"/><arg name="interfaces" type="as"/></signal>
  </interface>
  <interface name="org.bluez.AgentManager1">
    <method name="RegisterAgent"><arg name="agent" type="o" direction="in"/><arg name="capability" type="s" direction="in"/></method>
    <method name="UnregisterAgent"><arg name="agent" type="o" direction="in"/></method>
    <method name="RequestDefaultAgent"><arg name="agent" type="o" direction="in"/></method>
  </interface>
  <interface name="org.bluez.Adapter1">
    <method name="StartDiscovery"/>
    <method name="StopDiscovery"/>
    <method name="RemoveDevice"><arg name="device" type="o" direction="in"/></method>
    <method name="SetDiscoveryFilter"><arg name="properties" type="a{sv}" direction="in"/></method>
    <property name="Address" type="s" access="read"/>
    <property name="AddressType" type="s" access="read"/>
    <property name="Name" type="s" access="read"/>
    <property name="Alias" type="s" access="readwrite"/>
    <property name="Class" type="u" access="read"/>
    <property name="Powered" type="b" access="readwrite"/>
    <property name="PowerState" type="s" access="read"/>
    <property name="Discoverable" type="b" access="readwrite"/>
    <property name="DiscoverableTimeout" type="u" access="readwrite"/>
    <property name="Pairable" type="b" access="readwrite"/>
    <property name="PairableTimeout" type="u" access="readwrite"/>
    <property name="Discovering" type="b" access="read"/>
    <property name="UUIDs" type="as" access="read"/>
    <property name="Modalias" type="s" access="read"/>
  </interface>
  <interface name="org.bluez.Device1">
    <method name="Disconnect"/>
    <method name="Connect"/>
    <method name="ConnectProfile"><arg name="UUID" type="s" direction="in"/></method>
    <method name="DisconnectProfile"><arg name="UUID" type="s" direction="in"/></method>
    <method name="Pair"/>
    <method name="CancelPairing"/>
    <property name="Address" type="s" access="read"/>
    <property name="AddressType" type="s" access="read"/>
    <property name="Name" type="s" access="read"/>
    <property name="Alias" type="s" access="readwrite"/>
    <property name="Class" type="u" access="read"/>
    <property name="Appearance" type="q" access="read"/>
    <property name="Icon" type="s" access="read"/>
    <property name="Paired" type="b" access="read"/>
    <property name="Bonded" type="b" access="read"/>
    <property name="Trusted" type="b" access="readwrite"/>
    <property name="Blocked" type="b" access="readwrite"/>
    <property name="LegacyPairing" type="b" access="read"/>
    <property name="Connected" type="b" access="read"/>
    <property name="ServicesResolved" type="b" access="read"/>
    <property name="UUIDs" type="as" access="read"/>
    <property name="Modalias" type="s" access="read"/>
    <property name="Adapter" type="o" access="read"/>
  </interface>
</node>
"""

RFKILL_XML = """
<node>
  <interface name="org.gnome.SettingsDaemon.Rfkill">
    <property name="AirplaneMode" type="b" access="readwrite"/>
    <property name="HasAirplaneMode" type="b" access="read"/>
    <property name="HardwareAirplaneMode" type="b" access="read"/>
    <property name="BluetoothAirplaneMode" type="b" access="readwrite"/>
    <property name="BluetoothHasAirplaneMode" type="b" access="read"/>
    <property name="BluetoothHardwareAirplaneMode" type="b" access="readwrite"/>
    <property name="ShouldShowAirplaneMode" type="b" access="read"/>
  </interface>
</node>
"""
RFKILL_PATH = "/org/gnome/SettingsDaemon/Rfkill"
RFKILL_IFACE = "org.gnome.SettingsDaemon.Rfkill"


def log(msg):
    print("[fake-bluetooth] %s" % msg, flush=True)


def load_bridge():
    """android-bt's request() (file-drop RPC to android-bridge.sh), loaded the same way android-camera does."""
    ldr = importlib.machinery.SourceFileLoader("android_bt", os.environ.get("FAKE_BT_BRIDGE", "/usr/local/bin/android-bt"))
    spec = importlib.util.spec_from_loader("android_bt", ldr)
    mod = importlib.util.module_from_spec(spec)
    ldr.exec_module(mod)
    return mod


def icon_for_class(cod):
    """Bluetooth Class of Device -> freedesktop icon name (major class bits 8-12, minor bits 2-7)."""
    major = (cod >> 8) & 0x1F
    minor = (cod >> 2) & 0x3F
    if major == 1:
        return "computer"
    if major == 2:
        return "phone"
    if major == 4:
        return "audio-headphones" if minor in (1, 2, 6) else "audio-card"
    if major == 5:
        if minor & 0x10:
            return "input-keyboard"
        if minor & 0x20:
            return "input-mouse"
        if (minor & 0x0F) in (1, 2):
            return "input-gaming"
        return "input-tablet"
    return "bluetooth"


def parse_list(text):
    """bt-list reply -> (state, adapter_addr, adapter_name, {path: dev}). Same-named entries (e.g. a dual-mode
    headset bonded as LE + BR/EDR) are merged; Connected is OR-ed."""
    state, addr, name, by_name = None, "00:00:00:00:00:00", "Android", {}
    for line in text.splitlines():
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        key, rest = parts
        if key == "state":
            state = rest.strip()
        elif key == "adapter":
            addr = rest.strip()
        elif key == "adaptername":
            name = rest.strip()
        elif key == "dev":
            f = rest.split(" ", 3)
            if len(f) != 4 or len(f[0]) != 17:
                continue
            daddr, conn, cod_s, dname = f[0], f[1] == "1", f[2], f[3].strip()
            if not dname or dname.startswith(HIDDEN_NAMES):
                continue
            try:
                cod = int(cod_s, 16)
            except ValueError:
                cod = 0
            if dname in by_name:
                by_name[dname]["connected"] = by_name[dname]["connected"] or conn
                continue
            by_name[dname] = {"address": daddr, "name": dname, "connected": conn, "class": cod}
    devs, used = {}, set()
    for d in by_name.values():
        base = ADAPTER + "/dev_" + d["address"].replace(":", "_")
        path, i = base, 2
        while path in used:
            path, i = "%s_%d" % (base, i), i + 1
        used.add(path)
        devs[path] = d
    return state, addr, name, devs


class Service:
    def __init__(self, bridge):
        self.bridge = bridge
        self.sys_conn = None
        self.ses_conn = None
        self.state = None           # Android adapter state string; None = unknown (bridge not answering yet)
        self.addr = "00:00:00:00:00:00"
        self.name = "Android"
        self.devs = {}              # object path -> dict
        self.dev_reg = {}           # object path -> registration id
        self.fast_until = 0.0       # poll every FAST_POLL_S until then (set after an on/off request)
        self.last_poll = 0.0
        self.pending = None         # last on/off target sent, cleared when Android reports it or it fails
        self.jobs = queue.Queue()
        self.busy = False
        threading.Thread(target=self.worker, daemon=True).start()

    # ---- Android side (worker thread; results back on the GLib main loop) ---------------------------------
    def worker(self):
        while True:
            word = self.jobs.get()
            try:
                out = self.bridge.request(word)
                ok = True
            except Exception as e:      # RuntimeError from the bridge, or anything unexpected
                out, ok = str(e), False
            GLib.idle_add(self.on_reply, word, ok, out)

    def submit(self, word):
        self.busy = True
        self.jobs.put(word)

    def on_reply(self, word, ok, out):
        self.busy = False
        if word in ("bt-on", "bt-off") and not ok:
            self.pending = None
        if not ok:
            log("bridge %s failed: %s" % (word, out.strip()))
        elif word == "bt-list":
            self.apply(*parse_list(out))
        else:
            log("bridge %s -> %s" % (word, out.strip()))
            self.fast_until = now_s() + FAST_WINDOW_S
            self.submit("bt-list")
        return False

    def tick(self):
        now = now_s()
        fast = now < self.fast_until or self.state is None   # also until the bridge first answers
        if not self.busy and (fast or now - self.last_poll >= POLL_S):
            self.last_poll = now
            self.submit("bt-list")
        return True

    def set_android_power(self, on):
        target = "ON" if on else "OFF"
        if self.state == target or self.pending == target:
            return
        self.pending = target       # audit fix: repeated clicks must not queue duplicate bt-on/bt-off jobs
        log("user request: Android Bluetooth %s" % target.lower())
        self.submit("bt-on" if on else "bt-off")

    # ---- state model -------------------------------------------------------------------------------------------
    @property
    def powered(self):
        return self.state in ("ON", "TURNING_OFF")

    @property
    def power_state(self):
        return {"ON": "on", "TURNING_ON": "off-enabling", "TURNING_OFF": "on-disabling"}.get(self.state, "off")

    def apply(self, state, addr, name, devs):
        # audit fix: a bt-list reply can beat the bus_acquired callback; drop it (the 1.5 s unknown-state poll
        # retries) instead of recording devices whose D-Bus objects could not be registered yet
        if state is None or self.sys_conn is None:
            return
        old_powered, old_ps = self.powered, self.power_state
        first = self.state is None
        self.state, self.addr, self.name = state, addr, name
        if self.pending == state or (self.pending and now_s() >= self.fast_until):
            self.pending = None
        if first:
            log("Android Bluetooth state %s, %d paired device(s)" % (state, len(devs)))
        if not first and (self.powered != old_powered or self.power_state != old_ps):
            log("Android Bluetooth state -> %s" % state)
            self.props_changed(self.sys_conn, ADAPTER, "org.bluez.Adapter1",
                               {"Powered": GLib.Variant("b", self.powered),
                                "PowerState": GLib.Variant("s", self.power_state)})
            self.props_changed(self.ses_conn, RFKILL_PATH, RFKILL_IFACE,
                               {"BluetoothAirplaneMode": GLib.Variant("b", not self.powered)})
        for path in [p for p in self.devs if p not in devs]:
            self.remove_device(path)
        for path, d in devs.items():
            old = self.devs.get(path)
            if old is None:
                self.add_device(path, d)
            elif old["connected"] != d["connected"]:
                old["connected"] = d["connected"]
                log("%s %s" % (d["name"], "connected" if d["connected"] else "disconnected"))
                self.props_changed(self.sys_conn, path, "org.bluez.Device1",
                                   {"Connected": GLib.Variant("b", d["connected"]),
                                    "ServicesResolved": GLib.Variant("b", d["connected"])})

    # ---- D-Bus: properties --------------------------------------------------------------------------------------
    def adapter_props(self):
        return {
            "Address": GLib.Variant("s", self.addr),
            "AddressType": GLib.Variant("s", "public"),
            "Name": GLib.Variant("s", self.name),
            "Alias": GLib.Variant("s", self.name),
            "Class": GLib.Variant("u", 0),
            "Powered": GLib.Variant("b", self.powered),
            "PowerState": GLib.Variant("s", self.power_state),
            "Discoverable": GLib.Variant("b", False),
            "DiscoverableTimeout": GLib.Variant("u", 180),
            "Pairable": GLib.Variant("b", False),
            "PairableTimeout": GLib.Variant("u", 0),
            "Discovering": GLib.Variant("b", False),
            "UUIDs": GLib.Variant("as", []),
            "Modalias": GLib.Variant("s", ""),
        }

    def device_props(self, d):
        return {
            "Address": GLib.Variant("s", d["address"]),
            "AddressType": GLib.Variant("s", "public"),
            "Name": GLib.Variant("s", d["name"]),
            "Alias": GLib.Variant("s", d["name"]),
            "Class": GLib.Variant("u", d["class"]),
            "Appearance": GLib.Variant("q", 0),
            "Icon": GLib.Variant("s", icon_for_class(d["class"])),
            "Paired": GLib.Variant("b", True),
            "Bonded": GLib.Variant("b", True),
            "Trusted": GLib.Variant("b", True),
            "Blocked": GLib.Variant("b", False),
            "LegacyPairing": GLib.Variant("b", False),
            "Connected": GLib.Variant("b", d["connected"]),
            "ServicesResolved": GLib.Variant("b", d["connected"]),
            "UUIDs": GLib.Variant("as", []),
            "Modalias": GLib.Variant("s", ""),
            "Adapter": GLib.Variant("o", ADAPTER),
        }

    def rfkill_props(self):
        known = self.state is not None
        return {
            "AirplaneMode": GLib.Variant("b", False),
            "HasAirplaneMode": GLib.Variant("b", False),
            "HardwareAirplaneMode": GLib.Variant("b", False),
            "BluetoothAirplaneMode": GLib.Variant("b", known and not self.powered),
            "BluetoothHasAirplaneMode": GLib.Variant("b", True),
            "BluetoothHardwareAirplaneMode": GLib.Variant("b", False),
            "ShouldShowAirplaneMode": GLib.Variant("b", False),
        }

    def get_property(self, conn, sender, path, iface, name):
        if iface == "org.bluez.Adapter1":
            return self.adapter_props().get(name)
        if iface == "org.bluez.Device1" and path in self.devs:
            return self.device_props(self.devs[path]).get(name)
        if iface == RFKILL_IFACE:
            return self.rfkill_props().get(name)
        return None

    def set_property(self, conn, sender, path, iface, name, value):
        v = value.unpack()
        if iface == "org.bluez.Adapter1" and name == "Powered":
            self.set_android_power(bool(v))
            return True
        if iface == RFKILL_IFACE and name == "BluetoothAirplaneMode":
            self.set_android_power(not v)
            return True
        # Alias / Discoverable / Pairable / Trusted / Blocked / AirplaneMode: not controllable from here
        log("ignored write %s.%s=%r" % (iface, name, v))
        return False

    def props_changed(self, conn, path, iface, changed):
        if conn is None or not changed:
            return
        try:
            conn.emit_signal(None, path, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                             GLib.Variant("(sa{sv}as)", (iface, changed, [])))
        except GLib.Error as e:
            log("emit failed: %s" % e.message)

    # ---- D-Bus: objects ----------------------------------------------------------------------------------------
    def managed_objects(self):
        objs = {
            "/org/bluez": {"org.bluez.AgentManager1": {}},
            ADAPTER: {"org.bluez.Adapter1": self.adapter_props()},
        }
        for path, d in self.devs.items():
            objs[path] = {"org.bluez.Device1": self.device_props(d)}
        return objs

    def add_device(self, path, d):
        self.devs[path] = d
        self.dev_reg[path] = self.sys_conn.register_object(path, self.dev_info, self.method_call,
                                                           self.get_property, self.set_property)
        self.sys_conn.emit_signal(None, "/", "org.freedesktop.DBus.ObjectManager", "InterfacesAdded",
                                  GLib.Variant("(oa{sa{sv}})", (path, {"org.bluez.Device1": self.device_props(d)})))

    def remove_device(self, path):
        d = self.devs.pop(path)
        reg = self.dev_reg.pop(path, None)
        if reg:
            self.sys_conn.unregister_object(reg)
        self.sys_conn.emit_signal(None, "/", "org.freedesktop.DBus.ObjectManager", "InterfacesRemoved",
                                  GLib.Variant("(oas)", (path, ["org.bluez.Device1"])))
        log("device gone: %s" % d["name"])

    def method_call(self, conn, sender, path, iface, method, params, invocation):
        if iface == "org.freedesktop.DBus.ObjectManager" and method == "GetManagedObjects":
            invocation.return_value(GLib.Variant("(a{oa{sa{sv}}})", (self.managed_objects(),)))
        elif iface == "org.bluez.AgentManager1":
            invocation.return_value(None)       # pairing happens in Android; GNOME's agent is never called
        elif iface == "org.bluez.Adapter1" and method in ("StopDiscovery", "SetDiscoveryFilter"):
            invocation.return_value(None)
        elif iface == "org.bluez.Device1" and method in ("Connect", "Disconnect", "ConnectProfile", "DisconnectProfile"):
            invocation.return_dbus_error("org.bluez.Error.NotSupported",
                                         "connect or disconnect this device in Android (Bluetooth is Android-owned)")
        else:
            invocation.return_dbus_error("org.bluez.Error.NotSupported",
                                         "%s: pairing and discovery happen in Android" % method)

    # ---- bus setup ---------------------------------------------------------------------------------------------
    def on_system_bus(self, conn, name):
        self.sys_conn = conn
        node = Gio.DBusNodeInfo.new_for_xml(BLUEZ_XML)
        infos = {i.name: i for i in node.interfaces}
        self.dev_info = infos["org.bluez.Device1"]
        conn.register_object("/", infos["org.freedesktop.DBus.ObjectManager"], self.method_call, None, None)
        conn.register_object("/org/bluez", infos["org.bluez.AgentManager1"], self.method_call, None, None)
        conn.register_object(ADAPTER, infos["org.bluez.Adapter1"], self.method_call,
                             self.get_property, self.set_property)

    def on_session_bus(self, conn, name):
        self.ses_conn = conn
        info = Gio.DBusNodeInfo.new_for_xml(RFKILL_XML).interfaces[0]
        conn.register_object(RFKILL_PATH, info, None, self.get_property, self.set_property)


def now_s():
    return GLib.get_monotonic_time() / 1e6


def main():
    try:
        bridge = load_bridge()
    except Exception as e:
        log("cannot load /usr/local/bin/android-bt: %r — exiting" % (e,))
        sys.exit(1)
    svc = Service(bridge)

    def lost(conn, name):
        log("lost/could not own %s — exiting (a real bluetoothd must never run here)" % name)
        sys.exit(1)

    sys_bus = Gio.BusType.SESSION if os.environ.get("FAKE_BT_BUS") == "session" else Gio.BusType.SYSTEM
    Gio.bus_own_name(sys_bus, "org.bluez", Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                     svc.on_system_bus, lambda c, n: log("owned org.bluez"), lost)
    Gio.bus_own_name(Gio.BusType.SESSION, "org.gnome.SettingsDaemon.Rfkill", Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                     svc.on_session_bus, lambda c, n: log("owned org.gnome.SettingsDaemon.Rfkill"), lost)
    svc.submit("bt-list")
    GLib.timeout_add(int(FAST_POLL_S * 1000), svc.tick)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
