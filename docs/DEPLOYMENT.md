# Deployment notes

This source snapshot matches the SM-X926B setup on firmware X926BXXS9DZG1. The session replaces Android's SurfaceFlinger display owner while GNOME runs, then restores it. Keep the supervisor and restore helper together; use the time-boxed launcher.

## Prepare the Fedora rootfs

The tested filesystem is Fedora 44 aarch64 at `/data/fedora`, with GNOME Shell and Mutter 50.4. The runner expects Python 3 and PyGObject, D-Bus, NetworkManager, PipeWire, PulseAudio tools, GStreamer, eudev, and GNOME settings daemons. It also expects the tablet's vendor graphics libraries staged under `/usr/local/lib/hybris-vendor`, a libhybris build under `/usr/local/lib/hybris` and `/usr/local/lib/hybris-wl`, and the local native helpers below. These libraries depend on the firmware and cannot be substituted with generic Mali downloads.

The `src/hybris/build.sh` recipe pins a libhybris revision and the `src/hybris/patches` directory records the local changes. The recipe requires an aarch64 Fedora sysroot, Android headers, Clang, and an Android NDK linker. Its output alone is insufficient: device-matched vendor libraries and `src/hybris/ui_compat_ahb.c` must also be built and staged. No vendor binaries or entire Fedora filesystem are bundled.

Build `src/native/defex_off.c` with the matching Samsung kernel headers using its `Makefile`. Build `sfsentinel.c` with the Android NDK and the device's own binder libraries. Build `gnome_gbm_shim.c`, `wlan_keepup_shim.c`, `libtls-padding.c`, `outline-atomics-shim.c`, and `mastercheck.c` for Fedora aarch64. The scripts expect `defex_off.ko` and `sfsentinel` in `/data/local/tmp`; they expect the shared libraries in `/data/fedora/usr/local/lib` and `mastercheck` in `/data/fedora/usr/local/bin`. The source is included, but the cross-build setup is device-specific and is not automated by this repository.

## Stage the text assets

Run `bash scripts/assemble-stage.sh` on the host. It creates `out/stage/` with three trees:

| Tree | Destination on tablet |
| --- | --- |
| `android/data/local/tmp/` | `/data/local/tmp/` |
| `fedora/` | `/data/fedora/` |
| `termux/home/` | Termux home directory |

Review the tree and copy its contents to the corresponding destinations from an already-rooted environment. Preserve executable modes, ownership, and SELinux labels on existing tablet files. The Termux audio bridge uses the included `start-audio-bridge.sh` and needs Termux PulseAudio with the AAudio sink module available. Termux sensor support also needs Termux:API and external app commands enabled.

The GNOME resource overlays under `integration/gnome-shell-fixes` are for GNOME Shell 50.4. They replace select JavaScript modules via `G_RESOURCE_OVERLAYS`; a different shell version needs the changes rebased. The `screen-blank@fedora-tab` extension is used for lock and screen blanking without powering off the panel.

## Check and launch

Before launching, check `sh -n` on the Android shell files and `bash -n` on the Fedora shell files. Check that the device copy and host source hashes match, and that `/data/fedora/usr/local/lib/gnome_gbm_shim.so`, `/data/fedora/usr/local/bin/fake_logind.py`, and `/data/local/tmp/fedora-restore.sh` exist. The launcher checks several of these prerequisites before it stops Android's display service.

In an already-rooted shell:

```sh
sh /data/local/tmp/fedora-enter.sh
sh /data/local/tmp/fedora-session-gnome-shell.sh 15
```

The argument is a maximum duration in minutes. GNOME Log Out or holding both volume keys for three seconds ends it early. The supervisor calls `fedora-restore.sh` to return the display to Android. The session log is `/data/local/tmp/fedora-session-gnome-shell.log`.

## Known gaps

The GNOME desktop and GPU compositor work on the tested tablet. OpenGL and Vulkan applications are broken. The source tree has no firmware images, proprietary libraries, prebuilt module, or device-specific keys and dumps. A fresh device still needs a matching Fedora rootfs, native builds, vendor library extraction, and the Termux audio setup.
