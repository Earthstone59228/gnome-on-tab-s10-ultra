# GNOME hardware recording investigation — 2026-10-07

Status: hardware recorder patch ROLLED BACK. Do not redeploy until session failure protection and recording memory behavior are investigated. No further live encoder tests after incident.

Installed GNOME/Mutter: 50.5-1.fc44.aarch64. Original recorder source resource SHA256: 89f8954d2302e8c9eb22b4fcfe587ebddf2c51f1de64565358e2e43b6239a0a0. Restored resource verified identical.

Original recorder: OpenH264 software; 554–574% CPU in short top samples. Hardware GStreamer v4l2h264enc on /dev/video3 works with mmap OUTPUT/CAPTURE, BGRx 2960x1848 30fps, explicit GOP/IDR 30, joined sequence header, H264 level 5.1, 12 Mbps. Default zero GOP produced undecodable output; corrected synthetic file decoded 60/60 frames by host FFmpeg. Modified resource changed only screencastService.js, inserting hardware pipeline before untouched upstream fallbacks. Syntax and companion resource identity checked. Live recorder opened /dev/video3, no OpenH264 loaded, 3.5–7.4% CPU. These samples do not establish sustained memory/power behavior. Full live-file verification was interrupted by session shutdown. No captured screen video was pulled or viewed.

## Proven incident chain (Android incident log attached)
22:37:33.316: am_low_memory.
22:37:33.490: killinfo records victim PID 4300 / UID 10185 (Samsung Keyboard).
22:37:33.537–.541: HoneyBoardService disconnect / process death.
22:37:39.914: Samsung Keyboard restarted as PID 31819.
22:37:40.704: IME reconnect.
22:37:40.708: Initializing Handwriting Spy.
22:37:40.714: system_server android.imms fatal IllegalStateException DEAD_OBJECT at SurfaceControl.nativeCreate, WindowManagerService.getHandwritingSurfaceForDisplay, HandwritingModeController.initializeHandwritingSpy.
22:37:45: supervisor detects system_server restart, ends session.
22:38:39: supervisor restores Android settings and removes snapshot.
Android services subsequently running; boot uptime unchanged. This matches existing October 5 handwriting failure notes. Recording may have increased memory pressure; causal memory allocation attribution is NOT established. Baseline memory and swap were already nearly full before hardware test.

## Proposal (not implemented)
Temporarily select an inert Android input method with supportsStylusHandwriting=false and no keyboard UI BEFORE SurfaceFlinger stops. Snapshot default_input_method, enabled_input_methods, selected_input_method_subtype; verify actual input_method dump supports flag false; restore after SF/HWC running, retaining snapshot across zygote restart. GNOME keyboard and pen input remain independent. Only installed visible non-handwriting alternative is Google voice IME; Samsung dump also lists internal FakeHoneyBoardService with no handwriting, but its behavior is unverified. Prefer a deliberately minimal source-reviewed dummy IME to assuming internal service is inert.

AOSP android16-release MSG_RESET_HANDWRITING checks current method, supportsStylusHandwriting, and supported stylus IDs; not stylus_handwriting_enabled. That setting gates app availability, not necessarily surface initialization. Samsung actual dump: mStylusIds=[9], Honeyboard mSupportsStylusHw=true, handwriting setting=1. Need exact Samsung framework verification before calling setting-only mitigation sufficient.

Pogo-style alternative: selectively remove pen from system_server input mirror AND ensure Android forgets cached device while SF still running. Current mirror includes all initial nodes and existing open descriptors survive mount hiding; simply omitting pen node cannot clear cached stylus IDs. Never modify real global /dev/input or affect Fedora input.

## V2 staged follow-up (not installed)
The installed PipeWire 1.6.9 pipewiresrc exposes min-buffers/max-buffers. Source defaults: desired 16, maximum INT32_MAX. V2 sets hardware pipeline source range 4–8 and raw queue max 2 with downstream dropping to prevent queued raw-frame accumulation. This limits capture buffers; it does not prove that capture buffers caused the LMK incident or bound all firmware allocations. All upstream software fallbacks unchanged. Source JavaScript syntax checked; patched-v2.gresource and hardware-v2.diff staged locally only.
Read-only ffprobe of incident recording: H264 2960x1848, 187 packets, 28.886 seconds, 10,554,439 bytes. Variable frame timing; not proof of sustained 30fps. No video content viewed or exported.
recording-memory-watch.py is a temporary 45s monitor with PID/starttime identity verification and optional safety abort (kills only recorder below 1GiB MemAvailable; never compositor/framework). Not staged to device or exercised. SIGKILL abort can leave incomplete recording. Requires independent review before use; not a guarantee against abrupt allocation/LMK.

## Final recorder follow-up
The user explicitly authorized autonomous tests after the incident. The v2 active-motion test was safely aborted due rapid compositor RSS growth. V3 direct GPU DMA capture avoids that behavior in 3s,20s,60s motion tests; see RESULTS.md for complete metrics and supported boundary. Stock recorder was restored after each bounded probe. Earlier statements about no further tests and v2 being staged describe the initial incident response, superseded by this authorized follow-up.
