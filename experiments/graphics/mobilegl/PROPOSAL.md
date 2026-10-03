# Tab S10 Ultra: OpenGL → GLES proposal

Date: 2026-10-01. Status: research proposal plus a completed offscreen ADB capability/execution probe; no graphics configuration changes. See the live-probe addendum.

## Recommendation

The goal is **modern desktop OpenGL and Vulkan applications running locally on the tablet with hardware acceleration**, using their normal Linux binaries. Individual legacy-app fixes are outside scope.

Use two parallel API paths: **MobileGL DirectGLES → libhybris → Mali for desktop OpenGL**, and the **existing libhybris Vulkan → Mali path for native Vulkan apps**. This is a proposed general graphics stack, not a claim that either path supports every desktop application. MobileGL still needs an AArch64 runtime proof, correct Linux presentation, and verified modern GL semantics. Vulkan already has recorded local GPU execution and normal-window success, but some apps request unsupported features.

If “working” means immediate universal desktop compatibility with GPU speed, the inspected evidence does not establish an available solution on the stock stack. Software rendering is the existing compatibility fallback; completing the hardware path requires engineering and testing. Running a local translation layer still means the application executes locally on the tablet; it does not require remote rendering or replacing the Android kernel.

An earlier host-side build of MobileGL, at commit `b1d60523e4b34f9b8a6e290f10a41a4167ba0e8a` with a small GLX source fix (`patches/glx-xdisplay-type-name.patch`), had already identified the remaining X11 presentation problem. The work below starts from that commit.

## Evidence and its limits

| Evidence inspected | What it establishes |
|---|---|
| An earlier GPU test log from the tablet (not included in this repository) | EGL 1.5, Mali-G720-Immortalis MC12, GLES 3.2 r44p1, successful offscreen context and clean teardown. This log alone does not establish application correctness. |
| Earlier gl4es experiments (see [`../README.md`](../README.md)) | Prior pixel-checked gl4es probes; OpenSCAD image success; KiCad drawing at fixed size but black after resize; slicer shader failures. These are recorded prior results, not rerun here. |
| Host `AUTO-GPU-STATUS.md` | Later Vulkan/WSI results and MobileGL build investigation. It revises the simpler earlier Vulkan success narrative: an offscreen resize checker failed a frame while normal vkcube presentation succeeded. |
| Host `vk-gpu-probe.log` | Mali Vulkan 1.3.247 enumeration, robustness2/nullDescriptor absent, device creation and GPU fill/readback success. |
| Local MobileGL source | GLX window creation passes through `CreatePlatformWindowSurface`; DirectGLES sets a target GL/GLSL version of 4.6. Neither proves desktop GL 4.6 correctness on this tablet. |

Early graphics-stack proposals predate the successful hybris integration; use the later device results when deciding what remains to build.

## Theory: three problems must be solved separately

A desktop GL application expects an API and state machine, a shader language, and a window/context interface. GLES can execute much of the underlying work, but does not automatically satisfy all three contracts.

Proposed path:

```text
Fedora application: desktop GL + GLSL + GLX (or EGL)
  → MobileGL: desktop state, object semantics, shaders and dispatch
  → Linux presentation adapter: EGL pbuffer initially, X11 copy for GLX
  → libhybris EGL/GLES: glibc-to-bionic bridge
  → existing Arm libGLES_mali.so
  → existing kbase /dev/mali0 → Mali-G720
```

**API/state translation:** compatible operations can map to GLES; incompatible operations require state tracking and a correct implementation. Texture formats, framebuffer completeness, draw/read buffers, buffer mapping, synchronization, queries and context sharing all matter. Exporting a function name is not implementing its semantics.

**Shader translation:** desktop GLSL must be parsed and lowered to valid ESSL. A compiler pipeline can handle many language differences, but must preserve attribute/output bindings, uniform locations and layouts, sampler bindings, precision and linking across stages. Replacing `#version` alone is insufficient. Start with the exact shaders emitted by the installed slicers, including the recorded `#version 140` cases.

**Presentation:** Fedora wxGTK applications use GLX. The existing Mali/hybris configuration cannot accept an ordinary X11 window directly as an EGL native window. Reuse the architectural idea already proven by gl4es: render on the GPU into a pbuffer/FBO, read pixels, then copy into the X window. Port this as a separate presentation component; do not stack two competing desktop GL state trackers by placing MobileGL over gl4es.

This first presentation path costs a GPU-to-CPU readback and an X11 upload. At 1280×720 RGBA and 60 frames/s, each full-frame transfer is approximately 221 MB/s before overhead. At 2960×1848 it is approximately 1.31 GB/s per transfer. These are arithmetic estimates, not measured throughput; stalls and extra copies may dominate. Establish correctness at a modest window size before pursuing native Wayland or dma-buf presentation.

## Which implementation to use

| Candidate | Proposed role | Reason / limitation |
|---|---|---|
| MobileGL DirectGLES | First modern-GL prototype | Existing local host build, Linux frontends and explicit desktop state architecture. Still developmental; AArch64 build, Mali correctness and presentation remain unproven. |
| MobileGlues | Second implementation to compare if shader/API coverage blocks MobileGL | GL over GLES 3.x with desktop shader conversion; primarily Minecraft-oriented. General Fedora app support must be demonstrated. |
| LTW | Smaller alternative/reference | Thin desktop core GL → GLES wrapper, primarily Minecraft-oriented; upstream build produces Android libraries. |
| Existing gl4es | Background evidence only | Demonstrates the GLES bridge and presentation concept, but does not meet the modern desktop GL objective. |
| Zink | Defer unless its measured feature gaps are addressed | Uses Vulkan, where this driver has several required features/extensions missing. A new loader alone does not fill them. |

Upstream references checked: [MobileGL](https://github.com/MobileGL-Dev/MobileGL), [MobileGlues](https://github.com/MobileGL-Dev/MobileGlues), [LTW](https://github.com/MojoLauncher/LTW), and [Mesa Zink requirements](https://docs.mesa3d.org/drivers/zink.html). MobileGL and MobileGlues are distinct projects. Pin the selected source and dependencies for experiments rather than silently following a moving branch.

## Corrections to the older theory

1. **A failed Vulkan feature query proves a driver/API limitation, not that the silicon can never implement the behavior.** The records report missing logic operations, clip distance, dual-source blending, multiple viewports and vertex stores/atomics. That explains the tested Blender Vulkan rejection. It does not, by itself, prove every GLES translation or emulation strategy impossible.
2. **GLES 3.2 is not desktop GL 4.3.** Overlap in compute, storage buffers and shader stages makes translation worth investigating; differences in limits, precision, formats, rasterization and synchronization remain. Query GLES features independently instead of copying Vulkan capability bits into a GLES verdict.
3. **Modern translation is more than Linux glue.** The earlier claim that only GLX/EGL integration is missing is a hypothesis. Application and semantic tests must still verify the translator itself.
4. **Version strings are not capability evidence.** The local MobileGL DirectGLES target is 4.6 while its README calls 4.2 Core a development target. Do not present either as verified support. Reject unsupported requests and keep extension/limit reports truthful.
5. **Kernel replacement is not a routine remedy.** Later testing considered a theoretically loadable-module route, making the older blanket “impossible” wording too strong. It still requires a substantial kernel/platform port and is outside this userspace proposal.

Blender therefore remains on the working software fallback. A future GPU attempt needs its own exact-version feature audit and rendering tests; success with a slicer would not establish Blender compatibility or GPU Cycles support.

## Ordered experiments and pass criteria

### 1. Freeze and measure the existing baseline

Record source commit plus local patches, build flags, library hashes, exact app versions and launch environment. Save EGL/GLES extension lists and numeric limits from the working hybris path. Preserve all working launchers.

### 2. DirectGLES offscreen prototype

Build an AArch64 glibc library and select `MOBILEGL_BACKEND_TYPE=DirectGLES`. Explicitly load the working hybris EGL/GLES libraries without recursively loading the new wrapper or accidentally selecting Mesa software EGL. Reuse the recorded distinction between hybris's plain EGL library and its GLVND vendor plugin; they are not interchangeable entry points.

First prove: desktop context request → GLES backend → triangle with the application's shader dialect → pixel readback. Record both frontend and backend renderer strings, loaded-library mappings, shader source/compiler logs and expected/actual pixels. A host compile or successful context creation does not pass this gate.

### 3. Shader and framebuffer correctness

Replay captured slicer shaders, then test VAO/indexed draws, uniform and sampler updates, depth/stencil, blending, framebuffer attachment replacement and multisample resolve as actually used by the app. Compare images against llvmpipe with explicit tolerances.

### 4. Linux presentation and modern application proof

Add the pbuffer/readback/X11-copy adapter and exercise it in the existing Xvfb/headless-Xwayland harness. Implement the application's required GLX entry points, context sharing, make-current behavior, resize lifecycle and swap behavior. Do not assume the working gl4es shim can be transplanted without adjusting state ownership.

Use a representative modern-application matrix: PrusaSlicer/OrcaSlicer for desktop GL 3.x, Blender for the recorded GL 4.3 requirement, and normal Vulkan applications beyond vkcube. Test startup, actual rendering, interaction and window lifecycle. A passing slicer is an intermediate checkpoint, not the final deliverable. Blender must render correctly in the intended workload; merely bypassing its feature checks is a failure.

For Vulkan, preserve the proven direct libhybris loader path initially. Earlier testing recorded a crash when Fedora's standard Vulkan loader creates a device through the attempted ICD adapter. A robust general desktop setup must resolve loader/dispatch interoperability or explicitly establish which apps work with the direct loader. Validate ordinary window surfaces, resize/swapchain recreation and required extensions per app. Do not infer application support from Vulkan 1.3 enumeration alone.

### 5. Performance and deployment decision

Compare the same scene/window size against llvmpipe after shader warm-up: median and tail frame time, CPU use, memory growth and readback time. Check visual correctness alongside speed. Only then consider a supervised live GNOME test under the existing project's display-test protocol. Use isolated launchers during validation. The final objective is transparent selection for normal desktop launches, backed by a tested compatibility matrix and a reliable software fallback. Do not install an unverified replacement as system libGL.

If the adapter works but required shader/state semantics fail, reduce the failure to a test and compare MobileGlues/LTW before committing to a major translator rewrite. If readback dominates, investigate native Wayland or validated dma-buf synchronization as a separate optimization.

## Practical next action and definition of done

Continue the existing MobileGL experiment toward a usable modern desktop GL library. The first diagnostic gate is an offscreen modern-GL shader and pixel test through DirectGLES → hybris → Mali, followed immediately by GLX/EGL window support and real applications. It is a gate toward the general stack, not a substitute for it.

The goal is met only when the target modern desktop applications launch normally, render correctly on the tablet GPU, and survive ordinary window interaction. Report GL and Vulkan compatibility separately, with actual app and version results and explicit unsupported features. The legacy KiCad/gl4es canvas bug is out of scope for this effort. If essential GL 4.x behaviour cannot be implemented correctly and efficiently on the available GLES driver, state that limitation rather than promising universal support.

## Live ADB probe — 2026-10-01

Performed after the research, on the attached tablet and outside any GNOME session. Ran offscreen in Fedora through the installed hybris environment. No display session was started and SurfaceFlinger remained PID 25098 before/after.

- Existing `gles2_ext_probe` exited 0: EGL 1.5; hardware renderer Mali-G720-Immortalis MC12; GLES 3.2 r44p1. The probe requests an ES2 context and the driver reports ES3.2; it enumerates extensions, not full ES3.2 conformance or shader-stage execution.
- GLES extensions include geometry/tessellation, buffer storage, draw-buffers-indexed, framebuffer fetch, clip control and float color buffers. No `GL_EXT_blend_func_extended`, `GL_EXT_clip_cull_distance`, or `GL_OES_viewport_array` appears in that returned extension string. Clip control is not clip distance; multiview is not arbitrary viewport arrays. Absence here is not a separately requested ES3.2-context feature test.
- Freshly compiled Vulkan probe exited 0: Mali Vulkan 1.3.247, successful device creation, GPU fill of 1024 words, fence completion, correct host readback and clean teardown.
- Vulkan reports `logicOp=0`, `fillModeNonSolid=0`, `alphaToOne=0`, `shaderClipDistance=0`, `dualSrcBlend=0`, `multiViewport=0`, `vertexStores=0`, `tessGeomPointSize=0`. Geometry shaders, independent blending, depth clamp, fragment stores and robust buffer access report supported.
- `VK_EXT_robustness2`, nullDescriptor, maintenance5, conditional rendering and depth-clip-enable remain unavailable. Line rasterization, transform feedback and custom border color are exposed.

These fresh results confirm working local hardware GLES and Vulkan execution, and confirm the recorded Vulkan feature gaps. They do not prove modern desktop OpenGL translation, full Vulkan application compatibility, or window/swapchain correctness. No MobileGL binary or desktop application was run in this probe.

Raw output: [`evidence/gpu-probe-2026-10-01.txt`](evidence/gpu-probe-2026-10-01.txt). The Vulkan source was derived from the existing host probe, with its early feature-only return removed so execution and cleanup also run.

## Implementation gate passed — 2026-10-01

An isolated native AArch64 MobileGL DirectGLES build now passes the first desktop GL rendering gate: GL 3.3 core context request, desktop GLSL 330 vertex/fragment compilation and link, VAO/VBO triangle draw, pixel readback and clean teardown through hybris to Mali. Red center and blue corner pixels matched exactly; GL error 0, process exit 0. SurfaceFlinger remained active at the same PID. This verifies those operations, not advertised GL 4.6 support or normal desktop-app compatibility.

The experiment adds explicit plain-hybris EGL selection and core GLES dispatch through hybris `libGLESv2` wrappers. Getting every GLES function solely through `eglGetProcAddress` caused null backend version strings and an abort; the core-wrapper selection resolved that first failure. Native CMake needed the explicit existing Vulkan runtime path to avoid unresolved vk symbols. The original source checkout and deployed graphics policy were preserved. Build prerequisites `gcc-c++` and `libstdc++-devel` were added to Fedora; all graphics artifacts stayed isolated.

The next concrete blocker is GLX X11 presentation: implement a pbuffer/readback/X11-copy adapter, then test a real modern app and resize/make-current behavior in Xvfb. See `mobilegl-implementation-2026-10-01.md`, `mobilegl-offscreen-2026-10-01.log`, and `mobilegl-hybris-provider.patch` beside this proposal for actual evidence and reproducibility. No real desktop app was validated in this pass.
