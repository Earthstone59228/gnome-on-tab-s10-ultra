# Deployment notes

This source snapshot matches the SM-X926B setup on firmware X926BXXS9DZG1. The session replaces Android's SurfaceFlinger display owner while GNOME runs, then restores it. Keep the supervisor and restore helper together; use the time-boxed launcher.

Read [the architecture](ARCHITECTURE.md) first. It explains which process owns the display and why the Android and Fedora helpers run together. This page is a map of the files and external inputs needed for another installation. It is not a one-command installer.

## Prepare the Fedora rootfs

The tested filesystem is Fedora 44 aarch64 at `/data/fedora`, with GNOME Shell 50.4 and Mutter 50.5. It began with Fedora's official aarch64 container-base rootfs. Extract an OCI rootfs layer with its symlinks, file modes, and ownership preserved; the tested first extraction needed a repair pass because Android's toybox tar rejected absolute symlink targets. Verify `/data/fedora/bin/bash`, `/data/fedora/etc/shadow`, and executable `/data/fedora/usr/bin/python3` before building on top of it.

The runner expects Python 3 and PyGObject, D-Bus, NetworkManager, PipeWire, PulseAudio tools, GStreamer, eudev, GNOME settings daemons, and Xwayland. The exact installed core versions are in [the architecture](ARCHITECTURE.md#tested-package-baseline). The rootfs must also have a writable `/tmp`, a private `/run`, and the bind mounts installed by `fedora-enter.sh`. `dnf` inside the chroot needs `TMPDIR=/tmp` rather than Android's inherited temporary directory.

The graphics path expects the tablet's vendor libraries under `/usr/local/lib/hybris-vendor`, a libhybris build under `/usr/local/lib/hybris` and `/usr/local/lib/hybris-wl`, and the local native helpers below. These libraries depend on the firmware and cannot be substituted with generic Mali downloads.

## Device-specific graphics inputs

The working library set was assembled from the *same tablet and firmware*, then placed in the Fedora rootfs. Source locations are `/apex/com.android.runtime/lib64/bionic/` for bionic `libc`, `libm`, and `libdl`; `/apex/com.android.vndk.v34/lib64/` for VNDK dependencies; `/system/lib64/` for the system versions of `libbinder`, `libbinder_ndk`, `libc++`, `libui`, and `libdrm`; and `/vendor/lib64/` for the Mali and MediaTek vendor libraries. The system versions of the binder, C++, and UI libraries matter: their VNDK versions lacked symbols the working stack needed. Resolve the transitive `DT_NEEDED` closure with `readelf -d`, then verify every name resolves in the staged directory. Keep originals on the tablet untouched; all adaptation happens to copies inside Fedora.

[`vendor-library-names.txt`](vendor-library-names.txt) records the filenames in the working staged closure without publishing any library bytes. It is an inventory for comparison, not a list of packages to download from an unrelated firmware.

The tested libhybris setup also used a staged-copy bionic TLS compatibility change and a gralloc compatibility layer. `src/hybris/ui_compat_ahb.c` is the source for that layer. `src/hybris/build.sh` pins the upstream libhybris revision and applies the local patches; it needs an aarch64 Fedora sysroot, Android headers, Clang, and an Android NDK linker. Device-matched libraries and the compat layer must be built and staged separately. The build script is a starting point and has not been rerun from a clean checkout as a release gate.

No vendor binaries or entire Fedora filesystem are bundled. The tested firmware and the matching stock kernel are essential inputs.

## Native build outputs

| Source | Required output in the tested layout | Build environment |
| --- | --- | --- |
| `src/native/defex_off.c`, `src/native/Makefile` | `/data/local/tmp/defex_off.ko` | Matching Samsung kernel build tree |
| `src/native/sfsentinel.c` | `/data/local/tmp/sfsentinel` | Android NDK and device-matched binder libraries |
| `src/native/mastercheck.c` | `/data/fedora/usr/local/bin/mastercheck` | Fedora aarch64 |
| `src/native/gnome_gbm_shim.c` | `/data/fedora/usr/local/lib/gnome_gbm_shim.so` | Fedora aarch64 plus Android/DRM/EGL headers |
| `src/native/outline-atomics-shim.c` | `/data/fedora/usr/local/lib/liboutline-atomics-shim.so` | aarch64 Clang with `-mno-outline-atomics` |
| `src/native/wlan_keepup_shim.c` | `/data/fedora/usr/local/lib/wlan_keepup_shim.so` | Fedora aarch64 |
| `src/native/libtls-padding.c` | `/data/fedora/usr/local/lib/libtls-padding.so` | Fedora aarch64 |
| `src/native/nautilus_root_shim.c` | `/data/fedora/usr/local/lib/libnautilus_root_shim.so` | Fedora aarch64; optional Files integration |
| `src/hybris/ui_compat_ahb.c` | `/data/fedora/usr/local/lib/hybris-vendor/libui_compat_layer.so` | Android NDK and matching `libui`/`libnativewindow` |

The `src/native/Makefile` builds only the kernel module. Other outputs were built individually during development and need a cross-build recipe adapted to the reader's toolchain. Android binaries and Fedora binaries use different C libraries; do not interchange them.

The host cross-build used Clang targeting aarch64 glibc, a Fedora 44 aarch64 RPM sysroot, and `ld.lld` from the Android 14 6.1 DDK container. [`src/toolchain/ld.lld-podman-wrapper.sh`](../src/toolchain/ld.lld-podman-wrapper.sh) is the original linker wrapper; it maps `/tmp` into that container, so its build directory must live under `/tmp` unless the wrapper is changed. The Android-side `sfsentinel` uses the Android NDK and the matching device's binder libraries instead of the Fedora sysroot.

## Stage the text assets

Run `bash scripts/assemble-stage.sh` on the host. It creates `out/stage/` with three trees:

| Tree | Destination on tablet |
| --- | --- |
| `android/data/local/tmp/` | `/data/local/tmp/` |
| `fedora/` | `/data/fedora/` |
| `termux/home/` | Termux home directory |

Review the tree and copy its contents to the corresponding destinations from an already-rooted environment. Preserve executable modes, ownership, and SELinux labels on existing tablet files. The Termux audio bridge uses the included `start-audio-bridge.sh` and needs Termux PulseAudio with the AAudio sink module available. The sensor feed needs Termux:API, `termux-sensor`, `socat`, `flock`, `timeout`, and external app commands enabled. Fedora's camera path needs the GStreamer OpenH264 decoder and PipeWire sink plugins.

Camera and microphone integration use the open-source [scrcpy v4.1 server](https://github.com/Genymobile/scrcpy/releases/tag/v4.1). `android-bridge.sh` expects its server JAR at `/data/local/tmp/scrcpy/scrcpy-server-v4.1.jar` and copies it to `/data/local/tmp/scrcpy-server-v4.1.jar` as needed. The JAR is not included here. Verify the release checksum before using a copy from outside the official release.

The stage script installs `pkexec-chroot-wrapper.sh` under the executable name `/usr/local/bin/pkexec`, which Mutter's backlight helper expects. Fedora's `android-bt`, `android-camera`, and `android-bluetooth-app` stay in `/usr/local/bin`; `gnome-shell-session-runner.sh` and `gnome-peripherals.sh` go under Fedora's `/root` because the supervisor calls those absolute paths. The stage tree does not contain the native build outputs above or proprietary libraries.

The stage also includes the tested GLVND vendor JSON and the active input rules. The private eudev rules directory must contain the included `60-input-id`, touchscreen, S Pen, and minimal chroot rules; do not expose Android's real `/dev` to the stock broad udev rules. `local-overrides.quirks` supplies the S Pen resolution hint. These input files were read from the working tablet and contain no device serials or Wi-Fi configuration.

For GNOME Files (Nautilus), the tested rootfs also changes `/usr/share/applications/org.gnome.Nautilus.desktop`: set `DBusActivatable=false` and set both `Exec` entries to `env LD_PRELOAD=/usr/local/lib/libnautilus_root_shim.so nautilus`. Apply those changes to the Fedora-installed desktop file so its translations and metadata remain intact.

The GNOME resource overlays under `integration/gnome-shell-fixes` are for GNOME Shell 50.4. They replace select JavaScript modules via `G_RESOURCE_OVERLAYS`; a different shell version needs the changes rebased. The `screen-blank@fedora-tab` extension is used for lock and screen blanking without powering off the panel.

## Check and launch

Before launching, check `sh -n` on the Android shell files and `bash -n` on the Fedora shell files. Check that the device copy and host source hashes match, and that `/data/fedora/usr/local/lib/gnome_gbm_shim.so`, `/data/fedora/usr/local/bin/fake_logind.py`, `/data/local/tmp/sfsentinel`, `/data/fedora/usr/local/bin/mastercheck`, and executable `/data/local/tmp/fedora-restore.sh` exist. The launcher checks several of these prerequisites before it stops Android's display service.

In an already-rooted shell:

```sh
sh /data/local/tmp/fedora-enter.sh
sh /data/local/tmp/fedora-session-gnome-shell.sh 15
```

The argument is a maximum duration in minutes. GNOME Log Out or holding both volume keys for three seconds ends it early. The supervisor calls `fedora-restore.sh` to return the display to Android. The session log is `/data/local/tmp/fedora-session-gnome-shell.log`.

## Known gaps

The GNOME desktop and GPU compositor work on the tested tablet. General OpenGL and Vulkan applications are broken or incomplete; the [graphics experiments](../experiments/graphics/README.md) contain partial gl4es results. The source tree has no firmware images, proprietary libraries, prebuilt module, or device-specific keys and dumps. A fresh device still needs a matching Fedora rootfs, native builds, and vendor library extraction. There is no verified one-command clean-device installation yet.
