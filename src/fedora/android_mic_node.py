#!/usr/bin/env python3
"""android_mic_node.py — Android's microphone as a GNOME/PipeWire source, on demand (2026-09-28, doc 11 §AE).

WHY scrcpy and not Termux: Termux's PulseAudio can load module-sles-source, but Android silences it ("App op 27
missing, silencing record"): Termux's RECORD_AUDIO is "while in use" (uid mode foreground, owned by the permission
system — `cmd appops set --uid` is ignored), and nothing is in the foreground during a session. The open-source
scrcpy server (already used for the camera) runs as the shell uid, which has RECORD_AUDIO with no foreground rule:
`audio_source=mic audio_codec=raw raw_stream=true` = raw PCM s16le 48 kHz stereo on abstract socket scrcpy_6d696330.
Verified 2026-09-28: real signal, no silencing.

WHAT: one always-listed PipeWire source "android_mic" (pipewire-pulse module-pipe-source reading FIFO). It runs at full
rate with silence when nothing feeds it (tested on host PipeWire 1.6.9: no stall). Every POLL_S we count the source
outputs recording from it; 0->1 asks android-bridge.sh for mic-start (via android-bt request()) and pumps the socket into
the FIFO; back to 0 for GRACE_S -> mic-stop, socket closed, FIFO drained (so the next recording does not begin with
up to ~0.3 s of stale audio). So Android's mic (and its privacy indicator) is on only while a GNOME app records.
Unexpected stream end while still recording -> retry with backoff (2..32 s), capped at MAX_RETRIES.

SAFETY: no input devices, no Android settings. The Android-side server is killed by android-bridge.sh (SIGKILL + sweep)
and by the supervisor teardown sweep (scid=6d696330). Opt-out: /usr/local/etc/no-android-mic-node. Log: /android-mic-node.log
"""
import importlib.machinery
import importlib.util
import os
import signal
import socket
import subprocess
import sys
import threading
import time

SOURCE = "android_mic"
FIFO = "/run/android-mic.fifo"
SOCK_ADDR = "\0scrcpy_6d696330"
BRIDGE_PATH = os.environ.get("ANDROID_MIC_NODE_BRIDGE", "/usr/local/bin/android-bt")
NO_FLAG = "/usr/local/etc/no-android-mic-node"
POLL_S = 1.0
GRACE_S = 3.0
MAX_RETRIES = 5
# Audit 2026-09-28 (doc 11 §AI): the FIFO must only ever get WHOLE frames (s16le stereo = 4 bytes). recv() returns
# any byte count, and a dropped or partially written non-multiple of 4 shifts every later sample by 1-3 bytes = loud
# static until the next shift. Writes of <= PIPE_BUF (4096, a multiple of 4) to a pipe are all-or-nothing even with
# O_NONBLOCK, so a full FIFO drops whole frames only.
FRAME = 4
CHUNK = 4096


def log(msg):
    print("%s [android-mic-node] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def pactl(*args, timeout=5):
    return subprocess.run(["/usr/bin/pactl"] + list(args), capture_output=True, text=True, timeout=timeout)


def load_bridge():
    loader = importlib.machinery.SourceFileLoader("android_bt_micnode", BRIDGE_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class MicNode:
    def __init__(self):
        self.module_id = None
        self.fifo_fd = None
        self.pump = None            # (thread, stop_event)
        self.active = False         # mic-start requested and not yet stopped
        self.idle_since = None
        self.retries = 0
        self.next_retry = 0.0
        self.bridge = None
        self.ended = False          # set by the pump thread when the stream dies on its own

    # ---- PipeWire source -----------------------------------------------------------------------------
    def setup(self):
        try:
            os.unlink(FIFO)
        except FileNotFoundError:
            pass
        r = pactl("load-module", "module-pipe-source", "source_name=" + SOURCE, "file=" + FIFO,
                  "format=s16le", "rate=48000", "channels=2",
                  "source_properties=device.description='Android Microphone'")
        if r.returncode != 0:
            raise RuntimeError("load-module module-pipe-source failed: %s" % r.stderr.strip())
        self.module_id = r.stdout.strip()
        # O_RDWR: our writes never hit SIGPIPE/ENXIO whatever the reader does; O_NONBLOCK: a full FIFO drops audio
        # instead of stalling the pump.
        self.fifo_fd = os.open(FIFO, os.O_RDWR | os.O_NONBLOCK)
        pactl("set-default-source", SOURCE)
        log("source %s ready (module %s), set as default source" % (SOURCE, self.module_id))

    def teardown(self):
        self.stop("shutdown")
        if self.module_id:
            pactl("unload-module", self.module_id)
            self.module_id = None
        if self.fifo_fd is not None:
            os.close(self.fifo_fd)
            self.fifo_fd = None

    def consumers(self):
        """Number of source outputs recording from our source (pactl short lists: index, source index, ...)."""
        src = pactl("list", "short", "sources")
        idx = None
        for line in src.stdout.splitlines():
            f = line.split("\t")
            if len(f) > 1 and f[1] == SOURCE:
                idx = f[0]
        if idx is None:
            return 0
        outs = pactl("list", "short", "source-outputs")
        return sum(1 for line in outs.stdout.splitlines() if line.split("\t")[1:2] == [idx])

    # ---- Android side ---------------------------------------------------------------------------------
    def request(self, word):
        if self.bridge is None:
            self.bridge = load_bridge()
        return self.bridge.request(word)

    def start(self):
        try:
            out = self.request("mic-start")
        except Exception as e:
            log("mic-start failed: %r" % (e,))
            self.retry_later()
            return
        log("mic-start -> %s" % str(out).strip())
        self.active = True
        stop = threading.Event()
        t = threading.Thread(target=self._pump, args=(stop,), name="mic-pump", daemon=True)
        self.pump = (t, stop)
        t.start()

    def _pump(self, stop):
        sock = None
        for _ in range(80):
            if stop.is_set():
                return
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(SOCK_ADDR)
                sock = s
                break
            except OSError:
                time.sleep(0.1)
        if sock is None:
            log("mic socket did not come up")
            self.ended = True
            return
        sock.settimeout(0.5)
        got = 0
        carry = b""                         # bytes of an incomplete frame, written with the next recv
        try:
            while not stop.is_set():
                try:
                    data = sock.recv(1 << 14)
                except socket.timeout:
                    continue
                if not data:
                    break
                if got == 0:
                    log("mic live (first audio)")
                    self.retries = 0
                got += len(data)
                buf = carry + data
                n = len(buf) - len(buf) % FRAME
                carry = buf[n:]
                for off in range(0, n, CHUNK):
                    try:
                        os.write(self.fifo_fd, buf[off:min(off + CHUNK, n)])
                    except BlockingIOError:
                        pass                # FIFO full (nobody draining): drop these whole frames, never block
        except OSError as e:
            log("mic socket error: %r" % (e,))
        finally:
            sock.close()
        if not stop.is_set():
            log("mic stream ended unexpectedly after %d bytes" % got)
            self.ended = True

    def stop(self, why):
        if self.pump is not None:
            t, ev = self.pump
            ev.set()
            t.join(timeout=2)
            self.pump = None
        # audit 2026-09-28: a stream that died while nobody recorded must not count as a failed start (2 s delay) later
        self.ended = False
        if self.active:
            try:
                self.request("mic-stop")
            except Exception as e:
                log("mic-stop failed: %r" % (e,))
            self.active = False
            log("mic stopped (%s)" % why)
        self.drain()

    def drain(self):
        if self.fifo_fd is None:
            return
        drained = 0
        try:
            while True:
                b = os.read(self.fifo_fd, 1 << 16)
                if not b:
                    break
                drained += len(b)
        except BlockingIOError:
            pass
        # audit 2026-09-28: PipeWire may have read part of a frame we then drained the rest of. Everything written is
        # whole frames, so it is short exactly drained % FRAME bytes: complete that frame or the next stream is shifted.
        if drained % FRAME:
            try:
                os.write(self.fifo_fd, bytes(drained % FRAME))
            except BlockingIOError:
                pass

    def retry_later(self):
        self.retries += 1
        if self.retries > MAX_RETRIES:
            log("giving up after %d failed starts (a new recording resets this)" % MAX_RETRIES)
            self.next_retry = float("inf")
        else:
            self.next_retry = time.monotonic() + min(2 ** self.retries, 32)

    # ---- main loop ------------------------------------------------------------------------------------
    def run(self):
        prev = 0
        while True:
            try:
                n = self.consumers()
            except (subprocess.SubprocessError, OSError) as e:
                log("pactl failed: %r" % (e,))
                n = prev
            now = time.monotonic()
            if n > 0 and prev == 0:
                self.retries = 0
                self.next_retry = 0.0
            if n > 0:
                self.idle_since = None
                if self.ended:
                    self.ended = False
                    self.stop("stream ended")
                    self.retry_later()
                if not self.active and now >= self.next_retry:
                    self.start()
            elif self.active:
                if self.idle_since is None:
                    self.idle_since = now
                elif now - self.idle_since >= GRACE_S:
                    self.stop("no recorder for %.0f s" % GRACE_S)
                    self.idle_since = None
            prev = n
            time.sleep(POLL_S)


def main():
    if os.path.exists(NO_FLAG):
        log("disabled (%s present)" % NO_FLAG)
        return
    node = MicNode()

    def _term(_s=None, _f=None):
        node.teardown()
        log("exit")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    try:
        node.setup()
    except Exception as e:
        log("setup failed: %r" % (e,))
        node.teardown()
        sys.exit(1)
    try:
        node.run()
    finally:
        node.teardown()


if __name__ == "__main__":
    main()
