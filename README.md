# GNOME on Galaxy Tab S10 Ultra

Fedora 44 and GNOME Shell 50.4 running directly on the Galaxy Tab S10 Ultra display, using the tablet's stock kernel and Android graphics stack. This repository contains the current session supervisor, Fedora helpers, native shims, GNOME integration, and the source and notes for reproducing this device-specific setup.

Root access was achieved using Ghostlock ([Root My Galaxy](https://github.com/BuSung-dev/Root-My-Galaxy)).

## Current status

Tested on SM-X926B with firmware X926BXXS9DZG1 and kernel 6.1.145. A usable GNOME desktop works with GPU-accelerated GNOME Shell, touch, S Pen, keyboard, Wi-Fi, audio, camera, and the Android display restore path. Desktop OpenGL 4.6 and Vulkan work for apps launched through the Zink-over-PanVK wrappers (OrcaSlicer, Blender; PanVK is experimental and Blender still has a widget glitch). Earlier gl4es and MobileGL tests are superseded; see [`experiments/graphics`](experiments/graphics) and [`docs/PROGRESS-2026-10-04-to-07.md`](docs/PROGRESS-2026-10-04-to-07.md). The power button puts the tablet into real deep sleep (s2idle with the display powered off first) via an experimental daemon; see [`docs/DEEP-SLEEP.md`](docs/DEEP-SLEEP.md) and [`experiments/power`](experiments/power). This is a working personal port, not a general installer for other firmware or tablet variants.

## What is here

| Directory | Contents |
| --- | --- |
| [`src/android`](src/android) | Android-side session supervisor, startup, restore, and service bridges |
| [`src/fedora`](src/fedora) | GNOME runner, D-Bus stand-ins, input, sensors, and camera helpers |
| [`src/native`](src/native) | C source for kernel and user-space helpers; no compiled modules or blobs |
| [`src/hybris`](src/hybris) | libhybris build recipe and local patches |
| [`src/toolchain`](src/toolchain) | aarch64 linker wrapper used by the host build |
| [`integration`](integration) | GNOME Shell resource overlays, extension, and desktop files |
| [`config`](config) | Wi-Fi and brightness configuration |

The source snapshot is organized by where each component runs. Files ending in `.pre-*`, diagnostic logs, device dumps, firmware, binary builds, and proprietary GPU libraries are omitted. The optional [`experiments/graphics`](experiments/graphics) folder holds current OpenGL and Vulkan experiments (gl4es and MobileGL); these are not part of the working desktop path.

## Reproducing the setup

1. Start with a rooted SM-X926B on the matching firmware. Root must already support executing a Fedora chroot; this repository includes only the `defex_off` source needed by the tested runtime.
2. Put a Fedora 44 aarch64 root filesystem at `/data/fedora` on the tablet. The tested installation has GNOME Shell 50.4, Mutter 50.5, Python 3 with PyGObject, D-Bus, NetworkManager, PulseAudio, PipeWire, and the other services used by the scripts.
3. Build the native helpers and libhybris for aarch64. Stage the tablet's own vendor graphics libraries into the Fedora rootfs. Those libraries are device and firmware specific and are not redistributed here.
4. Deploy the Android-side files to `/data/local/tmp` and Fedora-side files to the paths expected by [`gnome-shell-session-runner.sh`](src/fedora/gnome-shell-session-runner.sh). Read the [architecture](docs/ARCHITECTURE.md) and [deployment notes](docs/DEPLOYMENT.md) for the file map, external dependencies, and checks.
5. From an already-rooted shell, run `sh /data/local/tmp/fedora-enter.sh`, then `sh /data/local/tmp/fedora-session-gnome-shell.sh 15` for a 15-minute session. The supervisor restores Android when the session ends. GNOME Log Out or holding both volume keys for three seconds ends it early.

The repository captures the working sources and integration layout. Rebuilding the stock-device graphics and binder dependencies requires matching vendor libraries and a cross-build toolchain; the [deployment notes](docs/DEPLOYMENT.md) identify those requirements. Do not use a mismatched binary or firmware-specific kernel module.

## Source and licensing

The GNOME Shell resource overlays under `integration/gnome-shell-fixes` are modifications of GNOME Shell 50.4 source and retain its GPL-2.0-or-later terms; see [`LICENSES/GPL-2.0-or-later.txt`](LICENSES/GPL-2.0-or-later.txt). Other code in this repository has no added license grant. Upstream libhybris source and proprietary vendor libraries are not included.
