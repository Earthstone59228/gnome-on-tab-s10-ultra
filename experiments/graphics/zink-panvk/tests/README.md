# Zink on the tablet: actual GPU test passed

2026-10-05. Experimental offscreen proof; not OpenGL conformance or desktop-app certification.

The final `run-in-fedora.sh` compiled `zink_pixels.c` on the tablet with gcc and ran both controls successfully:

- Software control: Zink over lavapipe/llvmpipe, OpenGL 4.6 Core reported.
- Hardware: Zink over the existing `candidate-aafix` native PanVK ICD, `Mali-G720 MC12`, OpenGL 4.6 Core reported.
- Each run compared every RGBA8 pixel after GLSL 330 shader rendering at 64×64, 127×93, 256×128 and 64×64 again: 52,771 pixels, zero mismatches.
- Each run dispatched a GLSL 430 compute shader into an SSBO and compared all 128 words: zero mismatches.
- The hardware process mappings contain `/opt/panvk-unaudited-20261004/candidate-aafix/libvulkan_panfrost.so`, Fedora Mesa Gallium and the system Vulkan loader. Renderer checks reject CPU fallback for hardware mode.
- Final runner returned zero; GNOME remained PID 21835. Earlier commentary misidentified this PID as SurfaceFlinger because two `pidof` results were read in the wrong order. No display session was started or stopped by these tests.
- Saved kernel snapshots contain 28 newly observed lines; none matches the searched GPU-fault/MMU-fault/translation-fault/GPU-reset/kernel-panic markers. The kernel ring changed between snapshots, so this is a limited log check, not exhaustive fault accounting.

## Interpretation

The driver exposes **OpenGL 4.6 Core**. Actual tests establish desktop GLSL 330 shader rendering, framebuffer allocation/reallocation/readback and GLSL 430 compute/SSBO writes. They do not validate every feature in GL 3.3, 4.3 or 4.6, compatibility-profile support, geometry/tessellation shaders, complex materials, synchronization stress, window presentation, performance or app compatibility.

The original software control failed at EGL initialization because Zink could not select a software physical device. The final runner uses `LIBGL_ALWAYS_SOFTWARE=1` only for the software control, and `0` for the Mali run. Both use explicit ICD selection and a clean process environment. No device-feature overrides or capability-spoof shims are used in this test.

## Files and reproduction

- `zink_pixels.c`: test source; host compile also passed `-Wall -Wextra -Werror`.
- `run-in-fedora.sh`: corrected repeatable runner, exits nonzero on either failure.
- `results.log`: final complete software and Mali output, including mapped libraries.
- `kernel-before.log`, `kernel-after.log`, `kernel-new.log`: retained log evidence.
- `include/`: isolated headers copied from the host; no development packages installed on the tablet.

Tablet test directory: `/data/fedora/tmp/zink-theory-20261005`. To rerun, invoke `/bin/sh /tmp/zink-theory-20261005/run-in-fedora.sh` inside Fedora through the documented root/chroot path. It compiles the source and applies driver selection only to the test child processes. Both runs have a 30-second kill timeout. A kernel-level hang cannot be bounded by a userspace timeout.

The driver and ordinary app launchers are unchanged. The next useful gate is a GLX/EGL window test and FreeCAD viewport rendering, not a global library replacement.
