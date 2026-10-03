# MobileGL: desktop OpenGL on the Mali GPU (experiment)

An experiment to run **modern desktop OpenGL** applications on the tablet's Mali-G720 by translating it to the GLES the
driver already provides: [MobileGL](https://github.com/MobileGL-Dev/MobileGL) (DirectGLES backend) → libhybris → the
vendor `libGLES_mali.so`. It is **per-application and opt-in**; nothing replaces the system `libGL`/`libEGL`, and it is not
used by the GNOME desktop itself.

Status as of 2026-10-02: a desktop-GL 3.3 core test and a real application render on the GPU in isolated test displays.
This is **not** an OpenGL conformance claim, and it has **not** been exercised inside a live GNOME session.

## What was verified

| Date | Test | Result |
| --- | --- | --- |
| 2026-10-01 | Offscreen: request a GL 3.3 core context, compile `#version 330 core` shaders, VAO/VBO triangle, read pixels back | Passed: link OK, centre pixel `255,0,0,255`, corner `0,0,255,255`, GL error 0. Renderer string `Espryt (MobileGL Core) (Mali-G720-Immortalis MC12, OpenGL ES 3.2)`. [`evidence/offscreen-triangle-2026-10-01.txt`](evidence/offscreen-triangle-2026-10-01.txt) |
| 2026-10-02 | PrusaSlicer 2.9.6 main window in an isolated Xvfb display | Rendered the model view, a visibly different resized view, and a 66-layer sliced preview. The process mapped `libMobileGL.so`, hybris `libGLESv2.so` and `libGLES_mali.so`. [main](evidence/mobilegl-normal-main-first-2026-10-02.png), [resized](evidence/mobilegl-normal-main-resized-2026-10-02.png), [preview](evidence/mobilegl-normal-main-preview-2026-10-02.png) |
| 2026-10-02 | PrusaSlicer G-code viewer in a headless labwc/Xwayland session | Rendered a 40-layer toolpath. The two captured frames were identical, so this run does **not** prove resize behaviour. [frame](evidence/mobilegl-normal-gcode-viewer-2026-10-02.png) |

PrusaSlicer has since been removed from the tablet, so these results are a record, not a currently installed app.
MobileGL reports a 4.6 version string; only the GL 3.3 operations above were checked. The application runs emitted preset
and metadata-fetch warnings from the test fixture that did not stop rendering.

## What does not work, or is untested

- **Blender** (Vulkan) rejects the Mali device: it lacks vertex-pipeline stores and atomics, multiple viewports, shader clip
  distance, logic ops and dual-source blending. MobileGL has not passed a Blender workload; software rendering is the
  fallback. See [`PROPOSAL.md`](PROPOSAL.md) for why a feature-reporting shim alone would not be safe.
- **Steam / Proton** is not established. The probed Vulkan 1.3 driver lacks `VK_EXT_robustness2` and `VK_KHR_maintenance5`,
  which current DXVK and vkd3d-proton require, and no x86 execution path (FEX/Wine) was validated.
- Use inside a real GNOME session, other applications, and performance are untested.

## Layout

| Path | Contents |
| --- | --- |
| [`PROPOSAL.md`](PROPOSAL.md) | Research and design notes: why MobileGL DirectGLES, ordered experiments, pass criteria |
| [`source-pin.txt`](source-pin.txt) | MobileGL commit, submodule versions, source-archive checksum |
| [`patches/`](patches) | Changes applied to MobileGL (see below) |
| [`scripts/`](scripts) | Build and test scripts; they use the paths of the tested setup (`/data/fedora` chroot) |
| [`launchers/mali-modern-gl`](launchers/mali-modern-gl) | Per-application wrapper that selects the MobileGL libraries |
| [`evidence/`](evidence) | Raw probe/test output and screenshots |

## Reproducing

MobileGL is **LGPL** (`COPYING.LESSER` in its tree). Its source and the built `libMobileGL.so` are **not** redistributed
here; rebuild from the pinned commit in [`source-pin.txt`](source-pin.txt) with its submodules initialised.

1. Check out MobileGL at the pinned commit. Apply `patches/glx-xdisplay-type-name.patch` (a one-line GLX type-name fix).
2. Apply the Python patch scripts in this order: `patch-loader.py` and `patch-gles.py` (explicit EGL/GLES provider
   selection via `MOBILEGL_EGL_LIBRARY` / `MOBILEGL_GLES_LIBRARY`; the same change is shown as `mobilegl-hybris-provider.patch`),
   then `patch-glx.py` (X11 copy presentation, `MOBILEGL_X11_COPY=1`), `patch-resize.py`, `patch-surface-switch.py`.
   The scripts edit `/tmp/mobilegl-20261001/MobileGL`; adjust the path for another checkout. The order is taken from the
   files' creation order and the installed library's contents; the full replay has not been re-run from scratch.
3. Build natively on the tablet inside the Fedora chroot ([`scripts/build.sh`](scripts/build.sh)). CMake needs
   `-DVulkan_LIBRARY=/usr/lib64/libvulkan.so.1` and `-DVulkan_INCLUDE_DIR=<checkout>/3rdparty/Vulkan-Headers/include`,
   because the runtime Vulkan library exists without its development symlink. Tests and benchmarks are turned off.
4. Install the library as a per-application directory (the tested one is `/usr/local/lib/mobilegl-20261001/`), with a
   `glx/` subdirectory whose `libGL.so.1`, `libGLX.so.0` and `libOpenGL.so.0` are symlinks to `libMobileGL.so`, plus the
   libhybris links the wrapper expects. Run applications through `mali-modern-gl PROGRAM [ARGS...]`.
5. Check with [`scripts/run.sh`](scripts/run.sh) (offscreen triangle) and [`scripts/xvfb-test.sh`](scripts/xvfb-test.sh)
   (GLX triangle). Both refuse to run while a GNOME session is up.

### Why the provider patch is needed

With EGL alone, `glGetString(GL_VERSION)` through EGL-returned function pointers was null and upstream built a string from
it, aborting in `FillInGLESCapabilities`. Taking core GLES functions from hybris's glibc wrappers via `dlsym` (and only
extension functions from `eglGetProcAddress`) fixed it for this test; it does not show every extension pointer is safe.

## Open problem

MobileGL hands an X11 native window to Mali EGL, which this hybris stack cannot accept. Presentation therefore renders to a
pbuffer and copies the result into the X11 window (`MOBILEGL_X11_COPY=1`). A proper window-system path and a wider
application matrix are the next steps; keep the launcher opt-in until each application passes rendering and
window-lifecycle tests.
