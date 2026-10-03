#!/usr/bin/env python3
"""fake_sensorproxy.py — stand-in for iio-sensor-proxy (net.hadess.SensorProxy) on the Galaxy Tab S10 Ultra.

WHY: the stock daemon can never work here — Samsung's sensorhub IIO devices expose no
in_accel_*/in_illuminance_* channels, only a packed timestamp record (verified 2026-09-26,
iio:device3 has scan_elements in_timestamp only). Android already owns the sensors, so the data
comes from Android through Termux:API:

    Termux (foreground service alive)          chroot (this daemon)
    termux-sensor -s ... | socat - TCP:127.0.0.1:47820  ->  listens on 127.0.0.1:47820
                                                          ->  net.hadess.SensorProxy on the system bus

Interface copied from iio-sensor-proxy 3.8 src/net.hadess.SensorProxy.xml (the version installed in the
chroot): HasAccelerometer / AccelerometerOrientation / AccelerometerTilt, HasAmbientLight /
LightLevelUnit / LightLevel, HasProximity / ProximityNear (always false), Claim*/Release* methods,
and net.hadess.SensorProxy.Compass (always HasCompass=false).

SAFETY / DEFAULTS
* Accelerometer (=> GNOME auto-rotate) is OFF unless the flag file /usr/local/etc/sensorproxy-accel exists:
  a rotated render through gnome_gbm_shim has never been tested. Light sensor is always offered.
* A capability is advertised only while fresh data keeps arriving (STALE_S); when Termux stops, Has* go
  false and GNOME falls back to "no sensor". Nothing here touches the kernel, /dev, DRM or Android state.
* Listens on loopback only. Input is untrusted JSON: bounded buffer, values type/range-checked.

Env (tests): SENSORPROXY_BUS=session uses the session bus; SENSORPROXY_PORT overrides 47820;
SENSORPROXY_ACCEL=1 forces the accelerometer on.
"""
import json
import math
import os
import socket
import sys
import time

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

BUS_NAME = "net.hadess.SensorProxy"
PATH = "/net/hadess/SensorProxy"
COMPASS_PATH = "/net/hadess/SensorProxy/Compass"
PORT = int(os.environ.get("SENSORPROXY_PORT", "47820"))
ACCEL_FLAG = "/usr/local/etc/sensorproxy-accel"
STALE_S = 10.0          # no data for this long => capability disappears
MAX_BUF = 64 * 1024     # drop the stream buffer if a peer sends garbage without closing braces

# 2026-09-27 UN-AUDITED — auto-brightness smoothing/hysteresis (user item 5: brightness chased raw sensor
# noise, "schizo" on the slightest light change). The Termux stream is raw single-shot STK31610 lux every
# 500 ms; each forwarded change pings gsd-power, which recalculates backlight immediately. Three layers:
#   EMA smoothing (LIGHT_EMA_ALPHA over the 500 ms stream) -> kills single-sample spikes,
#   deadband vs the last EMITTED value (max(LIGHT_DEADBAND_LUX, LIGHT_DEADBAND_PCT * emitted)),
#   minimum emit interval (LIGHT_MIN_EMIT_S) -> gsd-power is never re-pinged faster than this.
# Env-overridable for live tuning without redeploying; values chosen so a real room-light change still
# updates brightness within ~2-4 s. Zero-crossing (0 <-> lit) always emits immediately.
LIGHT_EMA_ALPHA = float(os.environ.get("SENSORPROXY_LIGHT_ALPHA", "0.25"))
LIGHT_DEADBAND_PCT = float(os.environ.get("SENSORPROXY_LIGHT_PCT", "0.08"))
LIGHT_DEADBAND_LUX = float(os.environ.get("SENSORPROXY_LIGHT_MIN_LUX", "2.0"))
LIGHT_MIN_EMIT_S = float(os.environ.get("SENSORPROXY_LIGHT_MIN_EMIT_S", "2.0"))
G = 9.80665
# 2026-09-28 (doc 11 §AG): Android/Samsung auto-brightness (android_autobrightness.py) replaces gsd-power's algorithm.
# In that mode every RAW lux sample goes to the daemon over loopback UDP and GNOME's LightLevel stays 0, which
# gsd-power 50.1 ignores ("LightLevel <= 0 -> return"), so it never sends its own target; HasAmbientLight is still
# published, so GNOME Settings keeps its Automatic Brightness toggle (the daemon follows that toggle).
# Opt-out (back to GNOME's algorithm + the smoothing below): /usr/local/etc/no-android-autobrightness
ANDROID_AB = not os.path.exists("/usr/local/etc/no-android-autobrightness")
ANDROID_AB_ADDR = ("127.0.0.1", int(os.environ.get("ANDROID_AB_PORT", "47821")))

XML = """
<node>
  <interface name="net.hadess.SensorProxy">
    <property name="HasAccelerometer" type="b" access="read"/>
    <property name="AccelerometerOrientation" type="s" access="read"/>
    <property name="AccelerometerTilt" type="s" access="read"/>
    <property name="HasAmbientLight" type="b" access="read"/>
    <property name="LightLevelUnit" type="s" access="read"/>
    <property name="LightLevel" type="d" access="read"/>
    <property name="HasProximity" type="b" access="read"/>
    <property name="ProximityNear" type="b" access="read"/>
    <method name="ClaimAccelerometer"/>
    <method name="ReleaseAccelerometer"/>
    <method name="ClaimLight"/>
    <method name="ReleaseLight"/>
    <method name="ClaimProximity"/>
    <method name="ReleaseProximity"/>
  </interface>
</node>
"""
COMPASS_XML = """
<node>
  <interface name="net.hadess.SensorProxy.Compass">
    <property name="HasCompass" type="b" access="read"/>
    <property name="CompassHeading" type="d" access="read"/>
    <method name="ClaimCompass"/>
    <method name="ReleaseCompass"/>
  </interface>
</node>
"""


def log(msg):
    print("[fake-sensorproxy] %s" % msg, flush=True)


def orientation_from_accel(x, y, z, previous):
    """Device is a natural-landscape tablet (Android sensor frame: x right, y up, z out of screen).
    Accelerometer reads +g along the axis pointing UP. Returns the iio-sensor-proxy strings:
    normal (top edge up), bottom-up, left-up, right-up. Flat on a table (|z| dominant) keeps the
    previous value; 20 degrees of hysteresis avoids flapping on the 45-degree diagonals."""
    mag = math.sqrt(x * x + y * y + z * z)
    if mag < 0.5 * G or mag > 1.6 * G:          # free-fall / shaking: ignore
        return previous
    if abs(z) > 0.8 * mag:                       # lying flat
        return previous
    ang = math.degrees(math.atan2(-x, y))        # 0 = normal, +90 = left-up? see mapping below
    # atan2(-x, y): y>0 (top up) -> 0; x>0 (right edge up) -> -90; x<0 (left edge up) -> +90; y<0 -> +-180
    table = (("normal", 0.0), ("left-up", 90.0), ("right-up", -90.0), ("bottom-up", 180.0))

    def dist(a, b):
        d = abs(a - b) % 360.0
        return min(d, 360.0 - d)

    best = min(table, key=lambda t: dist(ang, t[1]))
    if previous in dict(table):
        prev_angle = dict(table)[previous]
        if dist(ang, prev_angle) < 45.0 + 20.0:   # stay until clearly inside another quadrant
            return previous
    return best[0]


def tilt_from_accel(x, y, z, previous):
    mag = math.sqrt(x * x + y * y + z * z)
    if mag < 0.5 * G or mag > 1.6 * G:
        return previous
    # angle of the screen normal from horizontal; tilted-up = screen faces up-ish
    return "tilted-up" if z > 0 else "tilted-down"


class Proxy:
    def __init__(self, connection):
        self.conn = connection
        self.accel_allowed = bool(os.environ.get("SENSORPROXY_ACCEL")) or os.path.exists(ACCEL_FLAG)
        self.last_accel = 0.0
        self.last_light = 0.0
        self.light_ema = 0.0
        self.light_emit_at = 0.0
        self.has_accel = False
        self.has_light = False
        self.orientation = "undefined"
        self.tilt = "undefined"
        self.light = 0.0
        self.claims = {}
        self.ab_sock = None
        if ANDROID_AB:
            self.ab_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.ab_sock.setblocking(False)
            log("light: Android auto-brightness mode (raw lux -> udp %s:%d, GNOME LightLevel held at 0)" % ANDROID_AB_ADDR)
        self.buf = b""
        self.feeder = None
        self.decoder = json.JSONDecoder()
        log("accelerometer/auto-rotate %s (flag %s)" % ("ENABLED" if self.accel_allowed else "disabled", ACCEL_FLAG))

    # ---- D-Bus properties ------------------------------------------------
    def get_property(self, conn, sender, path, iface, name):
        v = {
            "HasAccelerometer": GLib.Variant("b", self.has_accel),
            "AccelerometerOrientation": GLib.Variant("s", self.orientation),
            "AccelerometerTilt": GLib.Variant("s", self.tilt),
            "HasAmbientLight": GLib.Variant("b", self.has_light),
            "LightLevelUnit": GLib.Variant("s", "lux"),
            "LightLevel": GLib.Variant("d", self.light),
            "HasProximity": GLib.Variant("b", False),
            "ProximityNear": GLib.Variant("b", False),
            "HasCompass": GLib.Variant("b", False),
            "CompassHeading": GLib.Variant("d", -1.0),
        }.get(name)
        return v

    def method_call(self, conn, sender, path, iface, method, params, invocation):
        # Claim/Release are accepted and ignored: the Termux feeder streams continuously.
        if method.startswith(("Claim", "Release")):
            invocation.return_value(None)
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def emit(self, changed):
        if not changed:
            return
        try:
            self.conn.emit_signal(
                None, PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", ("net.hadess.SensorProxy", changed, [])))
        except GLib.Error as e:  # noqa
            log("emit failed: %s" % e.message)

    # ---- data ingest --------------------------------------------------------
    def feed(self, chunk):
        self.buf += chunk
        if len(self.buf) > MAX_BUF:
            log("buffer overflow, dropping %d bytes" % len(self.buf))
            self.buf = b""
            return
        text = self.buf.decode("utf-8", "replace")
        pos = 0
        while True:
            while pos < len(text) and text[pos] in " \r\n\t":
                pos += 1
            if pos >= len(text):
                break
            if text[pos] != "{":            # resync: skip junk up to the next object start
                nxt = text.find("{", pos)
                if nxt < 0:
                    pos = len(text)
                    break
                pos = nxt
            try:
                obj, end = self.decoder.raw_decode(text, pos)
            except ValueError:
                if len(text) - pos > 4096:  # no complete object in 4 KiB: this one is junk, skip its brace
                    pos += 1
                    continue
                break                       # probably incomplete: wait for more bytes
            pos = end
            try:
                self.handle(obj)
            except Exception as e:  # never let one bad sample kill the connection
                log("bad sample ignored: %r" % (e,))
        self.buf = text[pos:].encode("utf-8")

    @staticmethod
    def vals(entry):
        if not isinstance(entry, dict):
            return None
        v = entry.get("values")
        if not isinstance(v, list) or not v:
            return None
        out = []
        for i in v:
            if isinstance(i, bool) or not isinstance(i, (int, float)):
                return None
            try:
                f = float(i)
            except (OverflowError, ValueError):   # e.g. a 400-digit integer
                return None
            if not math.isfinite(f):
                return None
            out.append(f)
        return out

    def handle(self, obj):
        if not isinstance(obj, dict):
            return
        now = time.monotonic()
        changed = {}
        for name, entry in obj.items():
            if not isinstance(name, str):
                continue
            v = self.vals(entry)
            if v is None:
                continue
            if name.endswith("Accelerometer") and "Uncal" not in name and len(v) >= 3 and self.accel_allowed:
                self.last_accel = now
                if not self.has_accel:
                    self.has_accel = True
                    changed["HasAccelerometer"] = GLib.Variant("b", True)
                    log("accelerometer data live")
                o = orientation_from_accel(v[0], v[1], v[2], self.orientation)
                if o != self.orientation:
                    self.orientation = o
                    changed["AccelerometerOrientation"] = GLib.Variant("s", o)
                    log("orientation -> %s" % o)
                t = tilt_from_accel(v[0], v[1], v[2], self.tilt)
                if t != self.tilt:
                    self.tilt = t
                    changed["AccelerometerTilt"] = GLib.Variant("s", t)
            elif name.endswith(" Light") and 0.0 <= v[0] < 1.0e7:
                self.last_light = now
                if not self.has_light:
                    self.has_light = True
                    changed["HasAmbientLight"] = GLib.Variant("b", True)
                    changed["LightLevelUnit"] = GLib.Variant("s", "lux")
                    self.light_ema = v[0]   # seed the EMA on the first sample
                    log("light sensor data live")
                if self.ab_sock is not None:
                    try:
                        self.ab_sock.sendto(repr(v[0]).encode(), ANDROID_AB_ADDR)
                    except OSError:
                        pass                # daemon not running: nothing listens, nothing blocks
                    continue
                # 2026-09-27 UN-AUDITED: smooth then hysteresis-gate the EMITTED value (see LIGHT_* knobs).
                # EMA first so a noise spike cannot jump the gate on its own.
                self.light_ema = LIGHT_EMA_ALPHA * v[0] + (1.0 - LIGHT_EMA_ALPHA) * self.light_ema
                if now - self.light_emit_at >= LIGHT_MIN_EMIT_S:
                    thr = max(LIGHT_DEADBAND_LUX, LIGHT_DEADBAND_PCT * self.light)
                    if abs(self.light_ema - self.light) >= thr or (self.light == 0.0 and self.light_ema != 0.0):
                        self.light = self.light_ema
                        self.light_emit_at = now
                        changed["LightLevel"] = GLib.Variant("d", self.light_ema)
        self.emit(changed)

    def watchdog(self):
        try:
            self._watchdog()
        except Exception as e:  # an exception would silently remove the GLib timeout source
            log("watchdog error: %r" % (e,))
        return True

    def _watchdog(self):
        now = time.monotonic()
        changed = {}
        if self.has_accel and now - self.last_accel > STALE_S:
            self.has_accel = False
            self.orientation = "undefined"
            self.tilt = "undefined"
            changed["HasAccelerometer"] = GLib.Variant("b", False)
            log("accelerometer stale, withdrawn")
        if self.has_light and now - self.last_light > STALE_S:
            self.has_light = False
            changed["HasAmbientLight"] = GLib.Variant("b", False)
            log("light sensor stale, withdrawn")
        self.emit(changed)

    # ---- TCP server ---------------------------------------------------------
    def drop_feeder(self):
        f = self.feeder
        self.feeder = None
        if f:
            for sid in f["ids"]:
                GLib.source_remove(sid)
            try:
                f["sock"].close()
            except OSError:
                pass

    def on_accept(self, source, cond, srv):
        try:
            c, addr = srv.accept()
        except OSError as e:
            log("accept failed: %r" % (e,))
            return True
        # Only ONE feeder at a time; the newest connection wins. Bounds fds/CPU: any local Android app
        # can reach loopback, so a flood only ever costs one socket (at worst it kicks the real feeder,
        # which reconnects within its retry interval).
        self.drop_feeder()
        c.setblocking(False)
        log("feeder connected from %s:%d" % addr)
        self.buf = b""
        f = {"sock": c, "ids": [], "last": time.monotonic()}
        self.feeder = f

        def on_data(src, cond2):
            try:
                d = c.recv(4096)
            except BlockingIOError:
                return True
            except OSError:
                d = b""
            if not d:
                log("feeder disconnected")
                if self.feeder is f:
                    self.feeder = None      # idle_check() ends its own timer on the next tick
                try:
                    c.close()
                except OSError:
                    pass
                return False
            f["last"] = time.monotonic()
            try:
                self.feed(d)
            except Exception as e:
                log("feed error: %r" % (e,))
                self.buf = b""
            return True

        def idle_check():
            if self.feeder is not f:
                return False
            if time.monotonic() - f["last"] > 30.0:
                log("feeder idle > 30 s, dropping")
                self.drop_feeder()
                return False
            return True

        f["ids"].append(GLib.io_add_watch(c.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, on_data))
        f["ids"].append(GLib.timeout_add_seconds(5, idle_check))
        return True


def main():
    bus_type = Gio.BusType.SESSION if os.environ.get("SENSORPROXY_BUS") == "session" else Gio.BusType.SYSTEM
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(2)
    srv.setblocking(False)
    holder = {}

    def on_bus(connection, name):
        p = Proxy(connection)
        holder["p"] = p
        main_info = Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0]
        comp_info = Gio.DBusNodeInfo.new_for_xml(COMPASS_XML).interfaces[0]
        connection.register_object(PATH, main_info, p.method_call, p.get_property, None)
        connection.register_object(COMPASS_PATH, comp_info, p.method_call, p.get_property, None)
        GLib.io_add_watch(srv.fileno(), GLib.IO_IN, p.on_accept, srv)
        GLib.timeout_add_seconds(2, p.watchdog)
        log("registered on bus (%s), feeder port 127.0.0.1:%d" % ("session" if bus_type == Gio.BusType.SESSION else "system", PORT))

    def on_lost(connection, name):
        log("lost bus name %s — exiting" % name)
        sys.exit(1)

    Gio.bus_own_name(bus_type, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_bus, None, on_lost)
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
