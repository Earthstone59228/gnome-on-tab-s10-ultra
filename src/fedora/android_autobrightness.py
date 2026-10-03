#!/usr/bin/env python3
"""android_autobrightness.py — Android/Samsung auto-brightness for the GNOME session (2026-09-28, doc 11 §AG).

WHY: GNOME's auto-brightness (gsd-power 50.1) maps lux linearly against a "normalised" lux with a ~1.6 s moving average and
re-targets up to 10x/s: it follows every small light change ("so annoying"). Android on this tablet feels calm because
Samsung's controller uses a very wide hysteresis (at 100 lux it only brightens above 400 lux and darkens below 40 lux),
debounces 1 s / 2 s, and ramps slowly. This is a port of AOSP AutomaticBrightnessController + Samsung SecHysteresisLevels
with the device's OWN numbers, read from `dumpsys display` on 2026-09-28 (the curve includes the user's learned
adjustments, "sbs" = Samsung's adaptive brightness).

HOW it plugs in (gnome-shell 50.4 / gnome-settings-daemon 50.1, read from those exact tags):
* gsd-power never touches the backlight: it sends a relative target to gnome-shell (org.gnome.Shell.Brightness
  SetAutoBrightnessTarget, no sender check), and the shell sets backlight = clamp(target + slider - 0.5) (dimming cap,
  our 250 ms fade overlay). gsd ignores LightLevel <= 0, and fake_sensorproxy (Android mode) keeps LightLevel at 0, so
  gsd stays silent while still claiming/releasing on the GNOME toggle (release -> it sends -1 = auto off).
* We get raw lux from fake_sensorproxy over loopback UDP (127.0.0.1:47821, one ASCII float per datagram) and send the
  shell our target while org.gnome.settings-daemon.plugins.power ambient-enabled (the GNOME toggle) is on.
* The slider keeps working as a bias on top of the target (shell behaviour), like adjusting in auto mode on Android.

Brightness scale: MAP (/usr/local/etc/android-brightness-map.json, measured on the device: Android manual brightness
setting 0..255 -> panel sysfs value + nits) converts nits <-> Android brightness float <-> sysfs. Ramps run in Android's
HLG "gamma" space at Android's slow ramp rate, like RampAnimator.
Opt-out: /usr/local/etc/no-android-autobrightness (fake_sensorproxy then also falls back to GNOME's own behaviour).
"""
import bisect
import json
import math
import os
import socket
import sys
import time

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

NO_FLAG = "/usr/local/etc/no-android-autobrightness"
MAP_PATH = os.environ.get("ANDROID_AB_MAP", "/usr/local/etc/android-brightness-map.json")
UDP_PORT = int(os.environ.get("ANDROID_AB_PORT", "47821"))
SYSFS = "/sys/class/backlight/panel"

# ---- device numbers (dumpsys display, 2026-09-28) ---------------------------------------------------------
# PhysicalMappingStrategy mBrightnessSpline: (lux, nits, tangent) — Android's own MonotoneCubicSpline tangents.
BRIGHTNESS_SPLINE = [
    (0.0, 10.872751, 4.419278), (1.0, 15.292029, 6.6211576), (2.0, 24.115067, 9.452496), (3.0, 34.19702, 4.853955),
    (4.0, 35.838917, 0.8375605), (5.0, 36.219997, 0.3498745), (6.0, 36.538666, 0.29647064), (7.0, 36.81294, 0.2504406),
    (9.0, 37.266155, 0.20935631), (10.0, 37.45826, 0.18000984), (12.0, 37.79409, 0.24515261),
    (15.0, 39.019604, 1.200743), (18.0, 46.2639, 1.9511127), (21.0, 50.72628, 0.9414941), (26.0, 52.703922, 0.2439106),
    (31.0, 53.165386, 0.08522415), (37.0, 53.63432, 0.07455921), (45.0, 54.202023, 0.06860638),
    (53.0, 54.73202, 0.06566048), (64.0, 55.447803, 0.065683216), (77.0, 56.309643, 0.056020975),
    (92.0, 56.995842, 0.042931184), (111.0, 57.75804, 0.025524477), (133.0, 58.56021, 0.10636715),
    (160.0, 66.191986, 0.346408), (192.0, 79.31703, 0.40798008), (230.0, 94.737526, 0.4010946),
    (276.0, 112.97131, 0.393902), (331.0, 134.49927, 0.36003256), (397.0, 156.19002, 0.3288281),
    (477.0, 182.5107, 0.30960184), (572.0, 210.07924, 0.118822075), (687.0, 214.70033, 0.020339241),
    (824.0, 216.94286, 0.011278062), (989.0, 218.95578, 0.034817588), (1187.0, 234.00308, 0.11430757),
    (1424.0, 270.1737, 0.16508722), (1709.0, 320.7771, 0.18258241), (2051.0, 384.9394, 0.13656114),
    (2461.0, 419.99982, 2.492354e-8), (2953.0, 420.0, 1.1162185e-6), (3000.0, 600.0, 0.5983442),
    (4000.0, 800.0, 0.04454411), (5000.0, 900.0, 0.0), (50000.0, 900.0, 0.0),
]
# SecHysteresisLevels: ambient lux -> ABSOLUTE brightening / darkening threshold lux.
BRIGHT_SPLINE = [
    (0.0, 10.0, 7.0), (10.0, 80.0, 7.5), (15.0, 120.0, 6.5714283), (50.0, 300.0, 3.5714285), (100.0, 400.0, 2.25),
    (300.0, 900.0, 2.25), (500.0, 1300.0, 1.7), (1000.0, 2000.0, 1.2), (2000.0, 3000.0, 1.0), (3000.0, 4000.0, 1.0),
    (4000.0, 5000.0, 1.0), (5000.0, 6000.0, 1.0), (6000.0, 7000.0, 1.0), (7000.0, 8000.0, 1.0),
    (8000.0, 9000.0, 0.5366563), (9000.0, 10000.0, 2.9516098), (10000.0, 20000.0, 6.5), (20000.0, 50000.0, 0.0),
    (40000.0, 50000.0, 0.0), (49999.0, 50000.0, 0.0),
]
DARK_SPLINE = [
    (0.0, -1.0, 0.2), (5.0, 0.0, 0.0), (9.0, 0.0, 0.0), (10.0, 3.0, 0.39775556), (25.0, 5.0, 0.04231442),
    (50.0, 10.0, 0.4), (100.0, 40.0, 0.14825575), (300.0, 50.0, 0.020539753), (500.0, 60.0, 0.14858706),
    (1000.0, 200.0, 0.34), (2000.0, 600.0, 0.9), (3000.0, 2000.0, 1.2), (4000.0, 3000.0, 1.0), (5000.0, 4000.0, 1.0),
    (6000.0, 5000.0, 1.0), (7000.0, 6000.0, 0.6666667), (10000.0, 7000.0, 0.31666666),
    (20000.0, 10000.0, 0.31666666), (50000.0, 20000.0, 0.33333334),
]
BRIGHTEN_DEBOUNCE_MS = 1000          # mBrighteningLightDebounceConfig
DARKEN_DEBOUNCE_MS = 2000            # mDarkeningLightDebounceConfig
HORIZON_SHORT_MS = 600               # mAmbientLightHorizonShort
HORIZON_LONG_MS = 2500               # mAmbientLightHorizonLong
WEIGHTING_INTERCEPT_MS = 2500        # mWeightingIntercept
RAMP_RATE = 0.2352941                # mBrightnessRampSlowIncrease/Decrease (HLG units per second)
TICK_S = 0.1


def log(msg):
    print("%s [android-ab] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def spline(points, x):
    """android.util.Spline.MonotoneCubicSpline.interpolate with the dumped (x, y, tangent) triples."""
    if x != x:
        return x
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    xs = [p[0] for p in points]
    i = bisect.bisect_right(xs, x) - 1
    if points[i][0] == x:
        return points[i][1]
    (x0, y0, m0), (x1, y1, m1) = points[i], points[i + 1]
    h = x1 - x0
    t = (x - x0) / h
    return ((y0 * (1 + 2 * t) + h * m0 * t) * (1 - t) * (1 - t)
            + (y1 * (3 - 2 * t) + h * m1 * (t - 1)) * t * t)


# AOSP BrightnessUtils (HLG OETF) — RampAnimator ramps in this space.
_R, _A, _B, _C = 0.5, 0.17883277, 0.28466892, 0.55991073


def to_gamma(v):
    n = max(v, 0.0) * 12.0
    return _R * math.sqrt(n) if n <= 1.0 else _A * math.log(n - _B) + _C


def from_gamma(g):
    n = (g / _R) ** 2 if g <= _R else math.exp((g - _C) / _A) + _B
    return n / 12.0


class Map:
    """Measured table: Android brightness float (setting/255) <-> sysfs value <-> nits (monotonic, linear interp)."""

    def __init__(self, path):
        with open(path) as f:
            rows = json.load(f)["points"]          # [{"setting":..,"sysfs":..,"nits":..}, ...]
        rows = sorted(rows, key=lambda r: r["setting"])
        self.f = [r["setting"] / 255.0 for r in rows]
        self.sysfs = [float(r["sysfs"]) for r in rows]
        self.nits = [float(r["nits"]) for r in rows]

    @staticmethod
    def _interp(xs, ys, x):
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            # beyond the manual range (HBM): extend the last segment's slope
            if xs[-1] == xs[-2]:
                return ys[-1]
            return ys[-1] + (x - xs[-1]) * (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
        i = bisect.bisect_right(xs, x) - 1
        return ys[i] + (x - xs[i]) * (ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i])

    def float_for_nits(self, nits):
        return self._interp(self.nits, self.f, nits)

    def sysfs_for_float(self, fl):
        return self._interp(self.f, self.sysfs, fl)


class Controller:
    """AOSP AutomaticBrightnessController ambient-lux logic (ring buffer, weighted averages, debounced hysteresis)."""

    def __init__(self):
        self.ring = []              # (t_ms, lux)
        self.ambient = None
        self.bright_thr = self.dark_thr = None

    def add(self, t_ms, lux):
        self.ring.append((t_ms, lux))
        cutoff = t_ms - HORIZON_LONG_MS
        # keep one sample older than the horizon (AOSP prune keeps the value in effect at the horizon start)
        while len(self.ring) > 1 and self.ring[1][0] <= cutoff:
            self.ring.pop(0)

    def _weighted(self, now, horizon):
        """AOSP calculateAmbientLux: time-weighted average, weight grows linearly with recency."""
        start = now - horizon
        total_w = total = 0.0
        end_t = now
        for t, lux in reversed(self.ring):
            s = max(t, start)
            # weight = integral over [s-now, end_t-now] of (x + WEIGHTING_INTERCEPT)
            a, b = s - now, end_t - now
            w = (b * b / 2 + WEIGHTING_INTERCEPT_MS * b) - (a * a / 2 + WEIGHTING_INTERCEPT_MS * a)
            total_w += w
            total += w * lux
            end_t = s
            if t <= start:
                break
        return total / total_w if total_w > 0 else (self.ring[-1][1] if self.ring else 0.0)

    def _set_ambient(self, lux):
        self.ambient = lux
        self.bright_thr = spline(BRIGHT_SPLINE, lux)
        self.dark_thr = spline(DARK_SPLINE, lux)

    def _next_transition(self, now, thr, above, debounce):
        earliest = now
        for t, lux in reversed(self.ring):
            if (lux <= thr) if above else (lux >= thr):
                break
            earliest = t
        return earliest + debounce

    def update(self, now):
        """Returns the new ambient lux when it changes, else None."""
        if not self.ring:
            return None
        fast = self._weighted(now, HORIZON_SHORT_MS)
        slow = self._weighted(now, HORIZON_LONG_MS)
        if self.ambient is None:                   # first valid reading: adopt immediately (AOSP: on sensor enable)
            self._set_ambient(fast)
            return self.ambient
        up = (slow >= self.bright_thr and fast >= self.bright_thr
              and self._next_transition(now, self.bright_thr, True, BRIGHTEN_DEBOUNCE_MS) <= now)
        down = (slow <= self.dark_thr and fast <= self.dark_thr
                and self._next_transition(now, self.dark_thr, False, DARKEN_DEBOUNCE_MS) <= now)
        if up or down:
            self._set_ambient(fast)
            return self.ambient
        return None


class Daemon:
    def __init__(self):
        self.map = Map(MAP_PATH)
        with open(SYSFS + "/max_brightness") as f:
            self.max_sysfs = float(f.read())
        self.ctl = Controller()
        self.enabled = False
        self.current = None         # Android brightness float currently applied
        self.target = None
        self.last_sent = None
        self.shell = None
        self.retry_at = 0.0         # back-off after a failed shell call (shell not up yet / restarting)
        self.failing = False
        self.settings = Gio.Settings.new("org.gnome.settings-daemon.plugins.power")
        self.settings.connect("changed::ambient-enabled", self._on_toggle)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", UDP_PORT))
        self.sock.setblocking(False)
        GLib.io_add_watch(self.sock.fileno(), GLib.IO_IN, self._on_lux)
        GLib.timeout_add(int(TICK_S * 1000), self._tick)
        self._on_toggle()

    def _on_toggle(self, *_a):
        en = self.settings.get_boolean("ambient-enabled")
        if en != self.enabled:
            self.enabled = en
            self.last_sent = None
            self.current = None         # re-enable: jump to the right level (like Android's initial adjustment)
            log("GNOME automatic brightness %s" % ("ON — Android controller active" if en else "OFF"))
            if not en:
                # audit 2026-09-28: a ramp tick can land AFTER gsd-power's own -1 (toggle off) and leave the shell in
                # auto mode with our last target. Send -1 once ourselves shortly after (gnome-shell 50.4
                # brightnessManager: _abTarget < 0 = auto inactive, the slider alone sets the level).
                GLib.timeout_add(500, self._send_off)

    def _send_off(self):
        if not self.enabled:
            p = self._shell_proxy()
            if p is not None:
                try:
                    p.call_sync("SetAutoBrightnessTarget", GLib.Variant("(d)", (-1.0,)), Gio.DBusCallFlags.NONE, 1000, None)
                except GLib.Error as e:
                    log("SetAutoBrightnessTarget(-1) failed: %s" % e.message)
                    self.shell = None
        return False

    def _on_lux(self, *_a):
        while True:
            try:
                data = self.sock.recv(64)
            except BlockingIOError:
                break
            try:
                lux = float(data.decode())
            except ValueError:
                continue
            if math.isfinite(lux) and 0.0 <= lux < 1e7:
                self.ctl.add(time.monotonic() * 1000.0, lux)
        return True

    def _shell_proxy(self):
        if self.shell is None:
            try:
                self.shell = Gio.DBusProxy.new_for_bus_sync(
                    Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_LOAD_PROPERTIES, None,
                    "org.gnome.Shell.Brightness", "/org/gnome/Shell/Brightness", "org.gnome.Shell.Brightness", None)
            except GLib.Error as e:
                log("shell brightness proxy: %s" % e.message)
        return self.shell

    def _send(self, fl):
        t = min(max(self.map.sysfs_for_float(fl) / self.max_sysfs, 0.0), 1.0)
        if self.last_sent is not None and abs(t - self.last_sent) < 0.0005:
            return
        if time.monotonic() < self.retry_at:
            return
        p = self._shell_proxy()
        if p is None:
            self.retry_at = time.monotonic() + 2.0
            return
        try:
            p.call_sync("SetAutoBrightnessTarget", GLib.Variant("(d)", (t,)), Gio.DBusCallFlags.NONE, 1000, None)
            self.last_sent = t
            if self.failing:
                self.failing = False
                log("gnome-shell brightness reachable again")
        except GLib.Error as e:
            if not self.failing:
                self.failing = True
                log("SetAutoBrightnessTarget failed (retrying every 2 s, logged once): %s" % e.message)
            self.shell = None
            self.retry_at = time.monotonic() + 2.0

    def _tick(self):
        try:
            now = time.monotonic() * 1000.0
            amb = self.ctl.update(now)
            if amb is not None:
                nits = spline(BRIGHTNESS_SPLINE, amb)
                self.target = self.map.float_for_nits(nits)
                log("ambient %.1f lux (next: brighten > %.0f, darken < %.0f) -> %.1f nits" % (
                    amb, self.ctl.bright_thr, self.ctl.dark_thr, nits))
            if not self.enabled or self.target is None:
                return True
            if self.current is None:
                self.current = self.target
            elif self.current != self.target:
                g, gt = to_gamma(self.current), to_gamma(self.target)
                step = RAMP_RATE * TICK_S
                g = min(gt, g + step) if gt > g else max(gt, g - step)
                self.current = self.target if abs(g - gt) < 1e-6 else from_gamma(g)
            self._send(self.current)
        except Exception as e:      # never let an exception remove the GLib timeout
            log("tick error: %r" % (e,))
        return True


def main():
    if os.path.exists(NO_FLAG):
        log("disabled (%s present)" % NO_FLAG)
        return
    try:
        Daemon()
    except Exception as e:
        log("startup failed: %r" % (e,))
        sys.exit(1)
    log("running (UDP 127.0.0.1:%d, map %s)" % (UDP_PORT, MAP_PATH))
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
