# Hardware GNOME recording — verified DMA path

## Outcome
A direct GPU DMA-buffer capture -> MediaTek V4L2 H.264 encoding candidate works with GNOME/Mutter 50.5 on this tablet. The exact independently reviewed v3 resource is now installed and checksum-verified. GNOME PID stayed unchanged; the normal recording UI uses the hardware path, with upstream software fallbacks retained.

## One-minute continuous-motion test
- Native output 2960x1848 H.264, 1216 decoded frames in 60.3628s (~20.1 delivered fps; 30fps maximum requested). No claim of sustained 30fps.
- Recorder CPU min/mean/max: 11.0/18.9/24.9% of one core.
- Compositor CPU mean: 75.1% of one core, including scene painting.
- Recorder RSS: 63.8–66.1 MiB.
- Compositor RSS: 971.4–987.1 MiB; periodic small reclamation, no rapid unbounded growth.
- Minimum available system memory: 4.67 GiB.
- Same persistent D-Bus connection confirmed start and stop, GNOME PID survived; exact stock resource hash restored automatically.
- Earlier three-second DMA motion recording: 63 decoded frames, nonblack luminance statistics, no searched recent encoder/IOMMU fault markers.

Earlier software result (554–574% CPU) was observed during user's actual video playback, not this identical synthetic workload; comparison is indicative, not a controlled battery or energy benchmark.

## Why v1/v2 were rejected
Hardware encoding alone on CPU-readable PipeWire MemFd buffers saved recorder CPU but compositor RSS grew approximately 1GiB/s during motion. This triggered memory pressure, Samsung Keyboard eviction/rebind, then the known Android handwriting surface DEAD_OBJECT crash. V2 cap alone did not fix it: exact Mutter 50.5 source already negotiates at most eight capture buffers.

Exact Fedora installed-version source was downloaded from https://kojipkgs.fedoraproject.org/packages/mutter/50.5/1.fc44/src/mutter-50.5-1.fc44.src.rpm . In src/backends/meta-screen-cast-area-stream-src.c the MemFd path calls clutter_stage_paint_to_buffer, which creates a fresh offscreen texture each frame. The DMA path paints into a retained framebuffer for the capture buffer. Direct DMA tests remove the rapid RSS behavior; exact vendor/GL allocation-retention cause is not proven.

## Candidate
resource-src-v3/screencastService.js adds one DMA hardware pipeline. Caps require DMA_DRM; output-io-mode=dmabuf-import, capture-io-mode=mmap. Explicit GOP/IDR=30, joined header, H264 level5.1, bitrate12Mbps; two raw queued buffers, PipeWire4–8 buffers. Existing software pipelines and normal GNOME recording UI retained. No unsafe MemFd hardware fallback is added.
Candidate SHA256 cb8d431acd9f1370ec7ff9f3f87d30703132b9025ecd17161363feceafd51b88.
Original SHA256 89f8954d2302e8c9eb22b4fcfe587ebddf2c51f1de64565358e2e43b6239a0a0.

install-recorder-device.sh refuses unexpected installed/staged/backup hashes and active recorder service, atomically replaces only the recorder resource, preserves original backup. It does not restart desktop or Android. rollback-recorder-device.sh restores pinned original. A GNOME package update may overwrite this local resource; installer refuses an unrecognized new version rather than applying blindly.

## Limits
Tests cover native display size, synthetic motion, repeated start/stop and 60s memory behavior. They do not establish every window/rotation/area format, sustained hours, audio recording (unchanged GNOME behavior), DRM capture, or battery temperature improvement. No screen/video content was viewed or pulled; only synthetic test file initially was exported. Later on-tablet checks decoded frames/metadata and nonblack statistics only.

Keyboard IME mitigation is a separate staged build/audit; not installed. This recorder change does not suppress the existing handwriting vulnerability if unrelated future memory pressure evicts Android Keyboard.

## Independent installation checks
Separate reviewer source review found no added Android/display-service interaction in v3. Review identified installer process-inventory failures and argv0 matching gaps; corrected installer/rollback then passed all eight independent adversarial host cases (failed inventory, absolute gjs path, late-active service, idle installation, failed inventory rollback, active service rollback, unknown-current rollback and idle rollback). Exact final independent audit report is separate.

## Final installation
Applied through audited install-recorder-device.sh after an actual Android mksh helper-name portability issue was safely refused, corrected to recorder_file_sha256, independently re-reviewed and executed read-only in the actual shell. Final installed resource SHA256 matches candidate cb8d431acd9f1370ec7ff9f3f87d30703132b9025ecd17161363feceafd51b88. Original backup remains alongside resource. Normal default cursor behavior also passed a four-second motion test (13–26% recorder CPU, stable compositor RSS, successful stop). No desktop restart or Android keyboard change.
