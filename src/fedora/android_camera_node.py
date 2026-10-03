#!/usr/bin/env python3
"""android_camera_node.py — always-listed, on-demand PipeWire camera nodes for Android's front/back
cameras (Galaxy Tab S10 Ultra Fedora session, 2026-09-27).

WHY: today `android-camera on front|back` / `off` create a single PipeWire Video/Source node only while a
human runs the launcher — Snapshot/Firefox see "no camera" until then, and forget to run `off` and Android's
camera indicator stays lit. This daemon instead publishes TWO PipeWire nodes, "Android front camera" and
"Android back camera" (node.name android-camera-front / android-camera-back), for the whole session, each
always producing a fixed-caps I420 1280x720 30/1 stream (a black placeholder video/x-raw ! videotestsrc, or
the decoded Android camera) through an input-selector, so GNOME/the xdg-desktop-portal camera picker always
lists both and consumers never renegotiate caps. WirePlumber (Wp-0.5 GI) tells us, per node, how many PipeWire
links are attached to it — that count is the ONLY thing that turns Android's camera stack on or off. Android
still owns capture: on 0->1 links we ask android-bridge.sh (via android-bt's request(), same protocol
android-camera already uses) to start scrcpy's camera server on the existing abstract socket
"\\0scrcpy_6e6f7630", decode with openh264 and feed appsrc; on ->0 for GRACE_S we ask it to stop and fall
back to black.

POLICY (only one Android camera can run at a time, imposed by android-bridge.sh's cam_start, which always
cam_stops first): the facing whose link count most recently went 0->1 wins immediately (pre-empts whichever
facing was active — its node falls back to black at once, its own consumers are not disconnected, they just
see black until the winner's last consumer leaves). Losing a race like this is not silent: if the pre-empted
facing still has consumers linked when the winner's last consumer leaves (GRACE_S after), it reclaims the
camera automatically. This is a deliberate design choice beyond the literal ask ("switch is immediate; off
is on a grace timer") because without a reclaim step a pre-empted-but-still-open app would need to be
restarted by the user to get its camera back — worth stating since it's the one place this diverges from
"just wire up WirePlumber".

SAFETY
* Never touches BlueZ/rfkill/Android settings/ADB; the only Android-side interaction is the existing
  android-bridge.sh whitelist (cam-front/cam-back/cam-stop), same calls android-camera already makes.
* The leaky queue sits AFTER openh264dec, never before (doc 11 §Y: pipewiresink back-pressure on a slow/absent
  consumer stalls scrcpy's encoder input surface -> Android camera HAL capture failures; dropping raw H.264
  before the decoder would also corrupt its reference frames). Kept as a hard rule, not just copied.
* All PipeWire/WirePlumber/GStreamer object mutation happens on the GLib main thread only. The two things
  that block (the android-bt bridge request, up to BRIDGE_TIMEOUT_S; and the blocking socket connect/recv
  loop) run on their own throwaway threads and hand results back with GLib.idle_add — the main loop, and so
  WirePlumber's + GStreamer's bus dispatch, is never blocked.
* Robust to: the bridge being absent or down at daemon start (logged, retried on the next 0->1 link — see
  POLICY note above for why that is a deliberate limit, not an oversight); the scrcpy socket closing
  (EOF -> fall back to black, log, ready to retry on the next request); a GStreamer pipeline error (the
  bus watch tears the pipeline down and rebuilds it from scratch, then re-requests capture if a consumer is
  still attached).

LIMITS
* Only one physical Android camera can run at a time (Android/bridge limit, not this daemon's).
* Fixed 1280x720 30/1 output; camera-branch frames are converted/scaled/rate-matched to that caps, never the
  other way around, so downstream consumers see one unchanging format for the life of the session.
* No audio (android-camera doesn't route it either; scrcpy is started with audio=false as before).
* Every GI call this file makes (Wp.Core/Wp.ObjectManager/Wp.Link/Wp.Node signals and methods, the
  GstPipeWireSink "provide" mode, input-selector/appsrc/openh264dec/videorate) was confirmed to exist with
  this exact signature by read-only introspection against the chroot's own installed PipeWire 1.6.8 /
  WirePlumber 0.5.14 / GStreamer 1.28.7 (`gst-inspect-1.0 <element>`, and
  `python3 -c 'import gi; gi.require_version("Wp","0.5"); ...'` after `Wp.init()`), not assumed by analogy
  with other WirePlumber versions. One easy-to-miss finding from that introspection, handled below: Wp.Core
  and Wp.Node/Wp.Link/Wp.ObjectManager all define their OWN `connect` GI method (wp_core_connect() etc.),
  which SHADOWS the usual PyGObject `obj.connect("signal", cb)` signal hookup on that particular class — this
  file always calls `GObject.Object.connect(obj, "signal", cb)` (the unbound base-class method) for signals on
  Wp objects, and reserves the plain `.connect()` spelling for the real wp_core_connect().

Opt-out: /usr/local/etc/no-android-camera-node (checked here AND meant to be checked by the runner launch
line, like the other optional daemons in this project — see runner-launch-snippet.sh next to this file).
Log: stdout (the runner redirects it to a file, same convention as fake_bluetooth.py / fake_sensorproxy.py).

Env (tests only, mirrors fake_bluetooth.py's FAKE_BT_BRIDGE / fake_sensorproxy.py's SENSORPROXY_* knobs):
  ANDROID_CAMERA_NODE_BRIDGE=<path>       replace /usr/local/bin/android-bt (must expose request(word,
                                           timeout=N) -> str, raising RuntimeError on failure, same contract)
  ANDROID_CAMERA_NODE_SOCK=<name>         abstract/UNIX socket path to read raw H.264 from (default
                                           "\\0scrcpy_6e6f7630"); a normal filesystem path works too, so
                                           tests can feed a real UNIX socket without root.
  ANDROID_CAMERA_NODE_GRACE_S=<seconds>   idle time before Android's camera is actually stopped (default 5)
  ANDROID_CAMERA_NODE_BRIDGE_TIMEOUT=<s>  bridge request() timeout (default 25, matches android-bt/-camera)
  ANDROID_CAMERA_NODE_RETRY_DELAY_S=<s>   delay before auto-retrying after an UNEXPECTED stop (socket
                                           error/EOF or a GStreamer pipeline crash) while a consumer is
                                           still linked (default 2) — bounds the retry rate; see
                                           CaptureManager._reconcile_idle_after_unexpected_stop for why a
                                           user-triggered start does not use this delay.
  ANDROID_CAMERA_NODE_NO_OPTOUT_CHECK=1   skip the /usr/local/etc opt-out file check (tests only)
"""
import importlib.machinery
import importlib.util
import os
import socket
import sys
import threading
import time

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
gi.require_version("Wp", "0.5")
from gi.repository import GLib, GObject, Gst, GstApp, Wp  # noqa: E402

OPT_OUT = "/usr/local/etc/no-android-camera-node"
BRIDGE_PATH = os.environ.get("ANDROID_CAMERA_NODE_BRIDGE", "/usr/local/bin/android-bt")
SOCK_ADDR = os.environ.get("ANDROID_CAMERA_NODE_SOCK", "\0scrcpy_6e6f7630")
GRACE_S = float(os.environ.get("ANDROID_CAMERA_NODE_GRACE_S", "5"))
BRIDGE_TIMEOUT_S = float(os.environ.get("ANDROID_CAMERA_NODE_BRIDGE_TIMEOUT", "25"))
SOCK_CONNECT_RETRIES = 40          # matches android-camera's feed(): 40 * 0.25s = 10s to give up
SOCK_CONNECT_DELAY_S = 0.25
UNEXPECTED_STOP_RETRY_DELAY_S = float(os.environ.get("ANDROID_CAMERA_NODE_RETRY_DELAY_S", "2"))
MAX_UNEXPECTED_RETRIES = 5
APPSRC_MAX_BYTES = 4 << 20         # encoded input queued in front of the decoder (audit F13); the reader gates on enough-data
APPSRC_STALL_S = 3.0               # decoder not draining for this long -> end the capture so it restarts clean
MAX_PENDING_AU_BYTES = 4 << 20     # one pending access unit larger than this is malformed (audit F14)
FLIP_FLAG = "/usr/local/etc/android-camera-flip-%s"   # opt-in horizontal flip per facing (audit U02, default off)

WIDTH, HEIGHT, FPS_NUM, FPS_DEN = 1280, 720, 30, 1
CAPS_STR = "video/x-raw,format=I420,width=%d,height=%d,framerate=%d/%d" % (WIDTH, HEIGHT, FPS_NUM, FPS_DEN)

FACINGS = ("front", "back")
NODE_NAME = {"front": "android-camera-front", "back": "android-camera-back"}
DISPLAY_NAME = {"front": "Android front camera", "back": "Android back camera"}


def log(msg):
    print("[android-camera-node] %s" % msg, flush=True)


# ---- bridge import (same loader trick android-camera uses for android-bt; overridable for tests) ----
def load_bridge():
    if not os.path.exists(BRIDGE_PATH):
        log("bridge module %s not found — camera requests will fail until it appears "
            "(logged once here; retried on the next link, see module docstring POLICY)" % BRIDGE_PATH)
        return None
    try:
        loader = importlib.machinery.SourceFileLoader("android_bt_camnode", BRIDGE_PATH)
        spec = importlib.util.spec_from_loader("android_bt_camnode", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    except Exception as e:  # noqa
        log("failed to load bridge module %s: %r" % (BRIDGE_PATH, e))
        return None


BRIDGE = load_bridge()


class CameraNode:
    """One PipeWire Video/Source node (front or back): a GStreamer pipeline that is always PLAYING and
    always outputs CAPS_STR, switching between a black placeholder and the decoded Android camera via an
    input-selector. Owns nothing about WHEN to capture — that is CaptureManager's job; this class only knows
    how to build/tear down its own pipeline, flip its own selector, and run its own socket reader thread."""

    def __init__(self, facing):
        self.facing = facing
        self.node_name = NODE_NAME[facing]
        self.display_name = DISPLAY_NAME[facing]
        self.pipeline = None
        self.sel = None
        self.black_pad = None
        self.cam_pad = None
        self.appsrc = None
        self.wp_node_id = None      # set once WirePlumber sees our own node (for link-ownership matching)
        self.link_count = 0
        self._probe_id = None
        self._probe_pad = None
        self._reader_thread = None
        self._reader_stop = None
        self._reader_sock = None
        self._reader_gen = 0        # bumped on every start/stop so a late _reader_ended from an old reader is ignored
        self._enough = threading.Event()   # set by appsrc enough-data, cleared by need-data (audit F13)
        self._bus = None
        self._bus_ids = []

    # ---- pipeline lifecycle -------------------------------------------------------------------------
    def ensure_pipeline(self):
        if self.pipeline is not None:
            return
        self._build_pipeline()

    def _build_pipeline(self):
        log("%s: building pipeline" % self.facing)
        pipeline = Gst.Pipeline.new("android-camera-" + self.facing)

        def make(factory, name=None):
            e = Gst.ElementFactory.make(factory, name)
            if e is None:
                raise RuntimeError("missing GStreamer element: " + factory)
            pipeline.add(e)
            return e

        vtsrc = make("videotestsrc", "black")
        vtsrc.set_property("pattern", "black")   # enum "black" (verified: value 2 of GstVideoTestSrcPattern)
        vtsrc.set_property("is-live", True)
        black_caps = make("capsfilter", "black-caps")
        black_caps.set_property("caps", Gst.Caps.from_string(CAPS_STR))
        vtsrc.link(black_caps)

        appsrc = make("appsrc", "camsrc")
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("is-live", True)
        appsrc.set_property("do-timestamp", True)
        # h264parse's sink pad template caps are the fixed string "video/x-h264" (verified via gst-inspect);
        # setting appsrc's caps to exactly that gets the same negotiated result the deployed fdsrc-based
        # android-camera pipeline gets from an uncapped fd, deterministically instead of by omission.
        # 2026-09-28 (doc 11 §AC): the reader now pushes ONE whole access unit per buffer (AccessUnitSplitter), so
        # do-timestamp stamps every frame. Before, arbitrary 64 KiB socket chunks were stamped and only ~8 of the
        # camera's ~27 fps came out of the decoder (measured live). alignment=au tells h264parse it is aligned.
        appsrc.set_property("caps", Gst.Caps.from_string("video/x-h264,stream-format=byte-stream,alignment=au"))
        # audit F13: appsrc's max-bytes is only a signal threshold in non-blocking mode. Raise it to a sane bound and
        # let the reader thread gate on enough-data/need-data instead of pushing blindly (H.264 reference data is
        # never dropped; a decoder that stops draining ends the capture, see start_reader()).
        appsrc.set_property("max-bytes", APPSRC_MAX_BYTES)
        self._enough.clear()
        appsrc.connect("enough-data", lambda _src: self._enough.set())
        appsrc.connect("need-data", lambda _src, _n: self._enough.clear())
        h264parse = make("h264parse")
        dec = make("openh264dec")
        # Hard rule (doc 11 §Y): leaky queue AFTER the decoder, never before — see module docstring SAFETY.
        queue = make("queue", "postdec-queue")
        queue.set_property("leaky", 2)          # 2 = downstream (drops newest when full)
        queue.set_property("max-size-buffers", 2)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", 0)
        conv = make("videoconvert")
        flip = None
        if os.path.exists(FLIP_FLAG % self.facing):
            try:
                flip = make("videoflip", "mirror-fix")
                flip.set_property("method", "horizontal-flip")
                log("%s: horizontal flip ENABLED by %s" % (self.facing, FLIP_FLAG % self.facing))
            except RuntimeError as e:
                log("%s: flip requested but unavailable (%s) — continuing unflipped" % (self.facing, e))
                flip = None
        scale = make("videoscale")
        rate = make("videorate")
        # 2026-09-28 (doc 11 §AC): without these, videorate filled every gap with duplicates — from the segment start
        # to the first camera frame and across the idle time between two captures — i.e. a burst of hundreds of
        # copies of one old frame (measured 60-100 fps out of a 27 fps camera on host PipeWire).
        rate.set_property("skip-to-first", True)
        rate.set_property("max-duplication-time", 100 * Gst.MSECOND)
        cam_caps = make("capsfilter", "cam-caps")
        cam_caps.set_property("caps", Gst.Caps.from_string(CAPS_STR))
        appsrc.link(h264parse)
        h264parse.link(dec)
        dec.link(queue)
        queue.link(conv)
        if flip is not None:
            conv.link(flip)
            flip.link(scale)
        else:
            conv.link(scale)
        scale.link(rate)
        rate.link(cam_caps)

        sel = make("input-selector", "sel")
        black_pad = sel.request_pad_simple("sink_%u")
        cam_pad = sel.request_pad_simple("sink_%u")
        black_caps.get_static_pad("src").link(black_pad)
        cam_caps.get_static_pad("src").link(cam_pad)

        sink = make("pipewiresink", "pwsink")
        sink.set_property("mode", 2)            # 2 = "provide" (verified enum value on this pipewiresink)
        sink.set_property("client-name", "android-camera-node-" + self.facing)
        props = Gst.Structure.new_empty("props")
        props.set_value("media.class", "Video/Source")
        props.set_value("media.role", "Camera")
        props.set_value("node.name", self.node_name)
        props.set_value("node.description", self.display_name)
        sink.set_property("stream-properties", props)
        sel.link(sink)
        # 2026-09-28 (doc 11 §AC) "one still frame, then black on re-open" (reproduced on host PipeWire, old node too):
        # upstream allocated from pipewiresink's own pool; when a consumer unlinks, PipeWire removes those buffers
        # (pool acquire -> "Broken pipe" -> FLUSHING) and videotestsrc/appsrc silently pause their streaming tasks, so
        # the next consumer only gets the ~16 already-queued frames. Refusing the ALLOCATION query makes upstream use
        # plain system memory; pipewiresink copies each frame into its own buffers instead (1.4 MB memcpy per frame).
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.QUERY_DOWNSTREAM, self._refuse_allocation)

        sel.set_property("active-pad", black_pad)   # never start on an unselected/undefined pad

        # "switch to camera after the first DECODED frame": probe the queue's src pad (right after the
        # decoder), not appsrc — undecoded H.264 arriving is not the same as a displayable frame existing.
        probe_pad = queue.get_static_pad("src")
        self._probe_id = probe_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_first_decoded_frame)
        self._probe_pad = probe_pad

        # audit F12: remember the watch and handler ids so teardown() can remove them; the handlers also carry the
        # pipeline they belong to and ignore messages from a pipeline that is no longer current.
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus = bus
        self._bus_ids = [bus.connect("message::error", self._on_bus_error, pipeline),
                         bus.connect("message::eos", self._on_bus_eos, pipeline)]

        self.pipeline = pipeline
        self.sel = sel
        self.black_pad = black_pad
        self.cam_pad = cam_pad
        self.appsrc = appsrc
        pipeline.set_state(Gst.State.PLAYING)
        # Verified on a live host PipeWire (2026-09-27): pipewiresink in mode=provide with no consumer
        # linked yet reports GST_STATE_CHANGE_ASYNC (stuck PAUSED->PLAYING) more or less forever — the
        # pw_stream itself is created and the node is registered and visible in `pw-dump` immediately
        # regardless, which is all "always listed" needs. It proceeds to a real PLAYING (and starts
        # rendering buffers) once something actually negotiates a format with it, i.e. once a consumer
        # links. Do not wait for get_state() to return SUCCESS/PLAYING here — it may never do so for the
        # black branch alone, by design.
        log("%s: pipeline PLAYING requested (node.name=%s, black)" % (self.facing, self.node_name))

    @staticmethod
    def _refuse_allocation(pad, info):
        q = info.get_query()
        if q is not None and q.type == Gst.QueryType.ALLOCATION:
            return Gst.PadProbeReturn.DROP
        return Gst.PadProbeReturn.OK

    def _on_first_decoded_frame(self, pad, info):
        if self._probe_id is not None:
            pad.remove_probe(self._probe_id)
            self._probe_id = None
        GLib.idle_add(self._flip_to_camera)
        return Gst.PadProbeReturn.REMOVE

    def _flip_to_camera(self):
        if self.sel is not None and self.cam_pad is not None:
            self.sel.set_property("active-pad", self.cam_pad)
            log("%s: camera live (first decoded frame)" % self.facing)
            MANAGER.unexpected_retries = 0   # a real frame arrived: the capture path works again
        return False

    def go_black(self):
        if self.sel is not None and self.black_pad is not None:
            self.sel.set_property("active-pad", self.black_pad)
            log("%s: back to black" % self.facing)
        # Deliberately NOT flushing/re-arming the first-frame probe here (tried, reverted — see git history
        # of this file / the design writeup: sending FLUSH_START/STOP into the middle of the camera branch
        # to purge its 2-buffer leaky queue works, but the appsrc upstream of the flushed section never
        # resends a SEGMENT event afterwards, which every downstream element then logs as "Got data flow
        # before segment event" on the next capture — cosmetically noisy for no real benefit). The actual
        # bug this was chasing (a stale buffered frame re-triggering the probe after a stop, flipping the
        # selector back to a now-dead branch) is already fixed by NOT re-arming the probe here at all: it is
        # only armed in start_reader(), right before a genuinely new capture begins, so at most the 1-2
        # frames still draining out of the queue at that point are shown briefly before real fresh frames
        # follow — harmless, and confirmed live to still fire "camera live" and flip correctly.

    def _arm_first_frame_probe(self):
        """Called right before a fresh capture starts (see start_reader()) — NOT from go_black(), which
        would let stale buffers still draining after a stop re-trigger it (see go_black's comment)."""
        if self._probe_id is not None:
            return
        if self.pipeline is None:
            return
        q = self.pipeline.get_by_name("postdec-queue")
        if q is None:
            return
        pad = q.get_static_pad("src")
        self._probe_id = pad.add_probe(Gst.PadProbeType.BUFFER, self._on_first_decoded_frame)
        self._probe_pad = pad

    def teardown(self):
        self.stop_reader()
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
        if self._bus is not None:
            for hid in self._bus_ids:
                try:
                    self._bus.disconnect(hid)
                except Exception:  # noqa
                    pass
            self._bus.remove_signal_watch()
        self._bus = None
        self._bus_ids = []
        if self._probe_id is not None and self._probe_pad is not None:
            try:
                self._probe_pad.remove_probe(self._probe_id)
            except Exception:  # noqa
                pass
        self._probe_pad = None
        self.pipeline = None
        self.sel = self.black_pad = self.cam_pad = self.appsrc = None
        self._probe_id = None

    def _on_bus_error(self, bus, msg, pipeline=None):
        if pipeline is not None and pipeline is not self.pipeline:
            return   # stale message from a torn-down pipeline
        err, dbg = msg.parse_error()
        log("%s: GStreamer ERROR: %s (%s) — tearing down and rebuilding" % (self.facing, err, dbg))
        self.on_crash()

    def _on_bus_eos(self, bus, msg, pipeline=None):
        if pipeline is not None and pipeline is not self.pipeline:
            return
        log("%s: unexpected EOS — tearing down and rebuilding" % self.facing)
        self.on_crash()

    def on_crash(self):
        self.teardown()
        self.ensure_pipeline()
        if MANAGER is not None:
            MANAGER.on_pipeline_crashed(self.facing)

    # ---- socket reader (its own thread; only touches appsrc, which is documented thread-safe) --------
    def start_reader(self):
        self.stop_reader()
        self._arm_first_frame_probe()
        stop = threading.Event()
        self._reader_stop = stop
        self._reader_gen += 1
        gen = self._reader_gen
        enough = self._enough
        enough.clear()
        appsrc = self.appsrc

        def run():
            sock = None
            try:
                for _ in range(SOCK_CONNECT_RETRIES):
                    if stop.is_set():
                        return
                    try:
                        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        s.settimeout(0.5)
                        s.connect(SOCK_ADDR)
                        sock = s
                        break
                    except OSError:
                        time.sleep(SOCK_CONNECT_DELAY_S)
                else:
                    log("%s: camera socket did not come up" % self.facing)
                    GLib.idle_add(self._reader_ended, "socket did not come up", gen)
                    return
                self._reader_sock = sock
                splitter = AccessUnitSplitter()
                while not stop.is_set():
                    try:
                        data = sock.recv(1 << 16)
                    except socket.timeout:
                        continue
                    except OSError as e:
                        log("%s: socket error: %r" % (self.facing, e))
                        break
                    if not data:
                        log("%s: camera socket EOF" % self.facing)
                        break
                    ret = Gst.FlowReturn.OK
                    stalled = False
                    for au in splitter.feed(data):
                        # audit F13: bounded ingestion. Wait while appsrc says enough-data; a decoder that does not
                        # drain for APPSRC_STALL_S ends this capture (it restarts through the normal retry path).
                        t0 = time.monotonic()
                        while enough.is_set() and not stop.is_set():
                            if time.monotonic() - t0 > APPSRC_STALL_S:
                                stalled = True
                                break
                            time.sleep(0.01)
                        if stalled or stop.is_set():
                            break
                        ret = appsrc.push_buffer(Gst.Buffer.new_wrapped(au))
                        if ret != Gst.FlowReturn.OK:
                            break
                    if stalled:
                        log("%s: decoder not draining for %.0f s (appsrc full) — ending capture" % (self.facing, APPSRC_STALL_S))
                        break
                    if ret != Gst.FlowReturn.OK:
                        log("%s: appsrc push-buffer -> %s" % (self.facing, ret))
                        break
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                if not stop.is_set():
                    GLib.idle_add(self._reader_ended, "stream ended", gen)

        t = threading.Thread(target=run, name="camnode-reader-" + self.facing, daemon=True)
        self._reader_thread = t
        t.start()

    def _reader_ended(self, reason, gen=None):
        if gen is not None and gen != self._reader_gen:
            return False   # a reader that was already replaced/stopped (audit F11)
        self.go_black()
        if MANAGER is not None:
            MANAGER.on_reader_ended(self.facing, reason)
        return False

    def stop_reader(self):
        # Only set the flag here — do NOT close self._reader_sock from this (the main) thread. Found live:
        # doing so races the reader thread's own blocking recv() on that fd and produces a spurious
        # "Bad file descriptor" OSError there. The reader thread's recv() has a 0.5s timeout specifically so
        # it notices `stop` promptly on its own and closes its own socket in its own `finally` — a plain
        # single-writer-per-fd rule, not a cosmetic choice.
        self._reader_gen += 1
        if self._reader_stop is not None:
            self._reader_stop.set()
        self._reader_sock = None
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2)
        self._reader_thread = None
        self._reader_stop = None


class AccessUnitSplitter:
    """Split a raw H.264 Annex-B byte stream (scrcpy raw_stream: one MediaCodec output buffer per access unit, but the
    socket merges/splits them arbitrarily) back into whole access units. A new AU starts at an AUD (9), SPS (7), PPS
    (8), SEI (6) or at a slice NAL (1/5) whose first_mb_in_slice is 0 (first payload bit set), once the current AU
    already holds a slice. Each AU is returned only when the next one begins (one frame of latency, ~33 ms)."""

    def __init__(self):
        self.buf = bytearray()
        self.scan = 0           # next index to search for a start code
        self.au_start = None    # index where the pending AU begins
        self.has_slice = False

    def feed(self, data):
        out = []
        self.buf += data
        b = self.buf
        i = max(self.scan - 3, 0)
        while True:
            j = b.find(b"\x00\x00\x01", i)
            if j < 0 or j + 4 >= len(b):   # need the NAL header byte and the first payload byte
                self.scan = j if j >= 0 else len(b)
                break
            nal_type = b[j + 3] & 0x1F
            start = j - 1 if j > 0 and b[j - 1] == 0 else j
            is_slice = nal_type in (1, 5)
            if is_slice:
                new_au = self.has_slice and bool(b[j + 4] & 0x80)   # first_mb_in_slice == 0 (ue(v) '1')
            else:
                new_au = self.has_slice and nal_type in (6, 7, 8, 9)
            if self.au_start is None:
                self.au_start = start
            elif new_au:
                out.append(bytes(b[self.au_start:start]))
                self.au_start = start
                self.has_slice = False
            if is_slice:
                self.has_slice = True
            i = j + 3
        if self.au_start:                      # drop consumed bytes, keep the pending AU
            del b[:self.au_start]
            self.scan -= self.au_start
            self.au_start = 0
        elif self.au_start is None and len(b) > (1 << 20):
            b.clear()                          # garbage with no start code at all: never grow unbounded
            self.scan = 0
        if len(b) > MAX_PENDING_AU_BYTES:
            # audit F14: a synchronised stream that never shows the next access-unit boundary. Drop the malformed
            # pending data and resynchronise on the next start code (the decoder recovers at the next IDR).
            log("splitter: pending access unit exceeded %d bytes — resynchronising" % MAX_PENDING_AU_BYTES)
            b.clear()
            self.scan = 0
            self.au_start = None
            self.has_slice = False
        return out


class CaptureManager:
    """Owns the single-Android-camera-at-a-time policy described in the module docstring. All state here is
    mutated only on the GLib main thread; bridge I/O happens on throwaway worker threads that report back
    with GLib.idle_add. `busy` serializes bridge requests so start/stop/switch can never race each other."""

    def __init__(self, nodes):
        self.nodes = nodes
        self.active_facing = None
        self.busy = False             # exactly one bridge request is in flight while True (audit F10/F11: never cleared elsewhere)
        self.want = None              # facing requested while busy; served by _pump() when the in-flight request completes
        self.need_stop = False        # Android's camera may be running with no owner: send cam-stop (serialized) when idle
        self.unexpected_retries = 0   # consecutive unexpected-stop retries (bounded, see below)
        self.grace_id = None
        self.grace_facing = None

    # ---- events from the WirePlumber link watcher --------------------------------------------------
    def on_first_consumer(self, facing):
        self._cancel_grace()
        self.unexpected_retries = 0   # a fresh 0->1 link is a new user action: retry budget starts over
        if facing == self.active_facing:
            return  # already serving this facing
        self._request_facing(facing)

    def on_last_consumer_gone(self, facing):
        if facing != self.active_facing:
            return  # this facing was never driving the camera (pre-empted or never started)
        self._start_grace(facing)

    def on_reader_ended(self, facing, reason):
        # The socket closed (server stopped, e.g. because the OTHER facing pre-empted it, or a real error).
        if self.active_facing == facing:
            log("%s: reader ended (%s) while active — treating as stopped" % (facing, reason))
            self.active_facing = None
            self.need_stop = True   # Android may still be capturing (e.g. decoder stall): stop it, serialized
            self._pump()
            self._reconcile_idle_after_unexpected_stop()

    def on_pipeline_crashed(self, facing):
        if self.active_facing == facing:
            self.active_facing = None
            self.need_stop = True   # serialized cam-stop (never overlaps an in-flight request; audit F11)
            self._pump()
        self._reconcile_idle_after_unexpected_stop()

    def _reconcile_idle_after_unexpected_stop(self):
        # An UNEXPECTED stop (socket error/EOF or a pipeline crash) still-wanted by a linked consumer is
        # worth one retry (a transient scrcpy hiccup should self-heal) but MUST NOT be allowed to spin: if
        # the underlying problem is persistent (verified live: an abandoned scrcpy socket path answers
        # connect() and then immediately ECONNRESETs, which looks just like a fresh failure each time), an
        # immediate reconcile()->request_facing()->fail loop would hammer the bridge in a tight cycle. A
        # fixed delay is enough to turn that into a bounded, slow retry instead of a spin — this is not
        # something a normal user-triggered start (on_first_consumer) needs, only this unexpected path.
        # Audit fix 2026-09-28: bounded — exponential backoff (2, 4, 8, 16, 32 s) and give up after
        # MAX_UNEXPECTED_RETRIES; each retry is an app_process start on Android. Reset by a decoded frame
        # (camera live again) or by a new consumer link (on_first_consumer).
        if self.unexpected_retries >= MAX_UNEXPECTED_RETRIES:
            log("unexpected-stop retries exhausted (%d) — staying black until a new app links" % self.unexpected_retries)
            return
        delay = UNEXPECTED_STOP_RETRY_DELAY_S * (2 ** self.unexpected_retries)
        self.unexpected_retries += 1
        GLib.timeout_add(int(delay * 1000), self._delayed_reconcile)

    def _delayed_reconcile(self):
        self._reconcile_idle()
        return False

    # ---- internal: start / stop / switch ------------------------------------------------------------
    def _pump(self):
        """Run the next queued bridge operation. Only acts when no request is in flight (audit F11)."""
        if self.busy:
            return
        w, self.want = self.want, None
        if w is not None and w != self.active_facing and self.nodes[w].link_count > 0:
            self._request_facing(w)
            return
        if self.need_stop and self.active_facing is None:
            self.need_stop = False
            self._bridge_stop_serialized()

    def _request_facing(self, facing):
        if self.busy:
            self.want = facing   # serialized: runs after the in-flight request completes
            return
        self.need_stop = False   # cam-<facing> itself stops whatever was running first
        node = self.nodes[facing]
        try:
            node.ensure_pipeline()   # first: a build failure must not leave busy/active_facing set
        except Exception as e:  # noqa
            log("%s: pipeline build failed (%r) — staying idle until the next new link" % (facing, e))
            return
        old = self.active_facing
        self.active_facing = facing
        self.busy = True
        if old and old != facing:
            self.nodes[old].go_black()
            self.nodes[old].stop_reader()

        def worker():
            try:
                if BRIDGE is None:
                    raise RuntimeError("android-bt bridge module not loaded")
                reply = BRIDGE.request("cam-" + facing, timeout=BRIDGE_TIMEOUT_S).strip()
            except Exception as e:  # noqa
                GLib.idle_add(self._start_done, facing, None, str(e))
                return
            GLib.idle_add(self._start_done, facing, reply, None)

        threading.Thread(target=worker, name="camnode-bridge-" + facing, daemon=True).start()

    def _start_done(self, facing, reply, err):
        self.busy = False
        ok = err is None and reply == "ok"
        if self.active_facing != facing:
            # superseded or the pipeline crashed while the bridge call was in flight. If it succeeded and nobody
            # owns the camera now, Android's capture is running unowned: stop it (serialized).
            if ok and self.active_facing is None:
                self.need_stop = True
            self._pump()
            return False
        if not ok:
            log("%s: bridge cam-%s failed (%s) — staying on black, will retry on the next new link"
                % (facing, facing, err or reply))
            self.active_facing = None
            self.nodes[facing].go_black()
            # audit fix 2026-09-28: a timed-out request may still be processed by the bridge later, so make
            # sure Android's camera is not left running while we believe it is off
            self.need_stop = True
            self._pump()
            return False
        w = self.want
        if w is not None and w != facing and self.nodes[w].link_count > 0:
            self._pump()   # another facing is already waiting: switch without starting a reader for this one
            return False
        self.nodes[facing].start_reader()
        # audit F10: every consumer may have left while the bridge call was in flight (a grace timer that fired
        # meanwhile found us busy). Re-arm the grace stop so the capture does not run unowned.
        if self.nodes[facing].link_count == 0 and self.grace_id is None:
            self._start_grace(facing)
        self._pump()
        return False

    def _start_grace(self, facing):
        self._cancel_grace()
        self.grace_facing = facing
        self.grace_id = GLib.timeout_add(int(GRACE_S * 1000), self._grace_fire)

    def _cancel_grace(self):
        if self.grace_id is not None:
            GLib.source_remove(self.grace_id)
        self.grace_id = None
        self.grace_facing = None

    def _grace_fire(self):
        facing = self.grace_facing
        self.grace_id = None
        self.grace_facing = None
        if facing == self.active_facing and self.nodes[facing].link_count == 0:
            if self.busy:
                self._start_grace(facing)   # audit F10: do not lose the stop while a bridge call is in flight
            else:
                self._begin_stop(facing)
        return False

    def _begin_stop(self, facing):
        if self.busy or self.active_facing != facing:
            return
        self.busy = True
        node = self.nodes[facing]
        node.go_black()
        node.stop_reader()

        def worker():
            try:
                if BRIDGE is not None:
                    BRIDGE.request("cam-stop", timeout=BRIDGE_TIMEOUT_S)
            except Exception as e:  # noqa
                log("cam-stop error (ignored, Android side may already be stopped): %r" % e)
            GLib.idle_add(self._stop_done, facing)

        threading.Thread(target=worker, name="camnode-bridge-stop", daemon=True).start()

    def _bridge_stop_serialized(self):
        self.busy = True

        def worker():
            try:
                if BRIDGE is not None:
                    BRIDGE.request("cam-stop", timeout=BRIDGE_TIMEOUT_S)
            except Exception as e:  # noqa
                log("cam-stop error (ignored): %r" % e)
            GLib.idle_add(self._aux_stop_done)
        threading.Thread(target=worker, name="camnode-bridge-stop-aux", daemon=True).start()

    def _aux_stop_done(self):
        self.busy = False
        self._pump()
        if self.active_facing is None and not self.busy and self.need_stop is False:
            self._reconcile_idle()   # a consumer may have linked while the unowned capture was being stopped
        return False

    def _stop_done(self, facing):
        self.busy = False
        if self.active_facing == facing:
            self.active_facing = None
        self._pump()
        self._reconcile_idle()
        return False

    def _reconcile_idle(self):
        """Called after the camera is confirmed off (or crashed): if either facing still has a consumer
        waiting, start it — this is the "reclaim after pre-emption" behaviour documented in POLICY above."""
        if self.busy or self.active_facing is not None:
            return
        for f in FACINGS:
            if self.nodes[f].link_count > 0:
                self._request_facing(f)
                return


# ---- WirePlumber wiring (module level: one Core, two ObjectManagers) --------------------------------
NODES = {f: CameraNode(f) for f in FACINGS}
MANAGER = CaptureManager(NODES)
LINK_OWNER = {}   # Wp.Link bound id -> facing, so object-removed can attribute a link without re-deriving it
OBJECT_MANAGERS = None   # set by main(); see the comment at its assignment for why this must be kept alive


def _on_node_added(om, node):
    # NOTE: node.get_properties() (the Wp.PipewireObject interface method) reliably raises
    # "TypeError: requires at least one argument" through this PyGObject/typelib combination — a real,
    # reproduced binding quirk (interface method vs. a same-named symbol resolves to an unbound descriptor),
    # not a maybe. get_global_properties() (from Wp.GlobalProxy) carries the same registry properties
    # (including node.name) for a bound proxy and works correctly — verified live against a real PipeWire.
    props = node.get_global_properties()
    name = props.get("node.name") if props is not None else None
    for f in FACINGS:
        if name == NODE_NAME[f]:
            NODES[f].wp_node_id = node.get_bound_id()
            log("wp: %s node bound (id=%d)" % (f, NODES[f].wp_node_id))


def _on_node_removed(om, node):
    nid = node.get_bound_id()
    for f in FACINGS:
        if NODES[f].wp_node_id == nid:
            NODES[f].wp_node_id = None
            log("wp: %s node unbound" % f)


def _on_link_added(om, link):
    try:
        out_node, _out_port, _in_node, _in_port = link.get_linked_object_ids()
    except Exception as e:  # noqa
        log("wp: link introspection failed: %r" % e)
        return
    for f in FACINGS:
        if NODES[f].wp_node_id is not None and out_node == NODES[f].wp_node_id:
            lid = link.get_bound_id()
            LINK_OWNER[lid] = f
            node = NODES[f]
            was_zero = node.link_count == 0
            node.link_count += 1
            log("wp: %s consumer linked (count=%d)" % (f, node.link_count))
            if was_zero:
                MANAGER.on_first_consumer(f)
            return


def _on_link_removed(om, link):
    lid = link.get_bound_id()
    f = LINK_OWNER.pop(lid, None)
    if f is None:
        return
    node = NODES[f]
    node.link_count = max(0, node.link_count - 1)
    log("wp: %s consumer unlinked (count=%d)" % (f, node.link_count))
    if node.link_count == 0:
        MANAGER.on_last_consumer_gone(f)


def _on_core_connected(core):
    log("wp: connected to PipeWire")


def _on_core_disconnected(core):
    log("wp: disconnected from PipeWire — will keep retrying")
    GLib.timeout_add(2000, _try_connect, core)


def _try_connect(core):
    if not core.connect():
        return True   # keep retrying (GLib.timeout_add repeats while the callback returns True)
    return False


def build_wp(core):
    om_nodes = Wp.ObjectManager.new()
    for f in FACINGS:
        interest = Wp.ObjectInterest.new_type(Wp.Node.__gtype__)
        interest.add_constraint(Wp.ConstraintType.PW_PROPERTY, "node.name",
                                 Wp.ConstraintVerb.EQUALS, GLib.Variant("s", NODE_NAME[f]))
        om_nodes.add_interest_full(interest)
    # get_bound_id() reads back SPA_ID_INVALID (0xffffffff) for every object unless the object manager was
    # explicitly asked to bind to PROXY_FEATURE_BOUND — verified live: without this line every node/link
    # this daemon sees reports bound_id 4294967295, so no id ever matches and no link is ever attributed to
    # either camera node. Confirmed present on Wp.ProxyFeatures in the 0.5 typelib.
    om_nodes.request_object_features(Wp.Node.__gtype__, Wp.ProxyFeatures.PROXY_FEATURE_BOUND)
    GObject.Object.connect(om_nodes, "object-added", _on_node_added)
    GObject.Object.connect(om_nodes, "object-removed", _on_node_removed)

    om_links = Wp.ObjectManager.new()
    om_links.add_interest_full(Wp.ObjectInterest.new_type(Wp.Link.__gtype__))
    om_links.request_object_features(Wp.Link.__gtype__, Wp.ProxyFeatures.PROXY_FEATURE_BOUND)
    GObject.Object.connect(om_links, "object-added", _on_link_added)
    GObject.Object.connect(om_links, "object-removed", _on_link_removed)

    core.install_object_manager(om_nodes)
    core.install_object_manager(om_links)
    return om_nodes, om_links


def main():
    if "ANDROID_CAMERA_NODE_NO_OPTOUT_CHECK" not in os.environ and os.path.exists(OPT_OUT):
        log("opt-out file %s present — not starting" % OPT_OUT)
        return 0

    Gst.init(None)
    # The nodes must exist and be PLAYING (black) the moment this daemon starts, independent of whether
    # WirePlumber is reachable yet — "always listed" must not depend on the WP link-watcher coming up.
    for f in FACINGS:
        NODES[f].ensure_pipeline()

    Wp.init(Wp.InitFlags.ALL)
    core = Wp.Core.new(None, None, None)
    # Keep the ObjectManagers referenced at module scope for the life of the process: install_object_manager
    # does not hand Python a reference-counted claim on them that would stop `core` being the only thing
    # keeping them alive, and letting the local variables from build_wp() go out of scope here has, in
    # testing, been enough for them to be garbage-collected while `core` (a bare handle) is not — silently
    # turning off all link/consumer detection with no error. Global OBJECT_MANAGERS is the fix, not cosmetic.
    global OBJECT_MANAGERS
    OBJECT_MANAGERS = build_wp(core)
    GObject.Object.connect(core, "connected", _on_core_connected)
    GObject.Object.connect(core, "disconnected", _on_core_disconnected)
    if not core.connect():
        log("wp: initial connect() did not start immediately — retrying")
        GLib.timeout_add(2000, _try_connect, core)

    log("ready: %s" % ", ".join("%s=%s" % (f, NODE_NAME[f]) for f in FACINGS))
    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
