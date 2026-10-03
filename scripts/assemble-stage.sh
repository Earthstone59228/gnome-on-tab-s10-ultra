#!/usr/bin/env bash
# Make a directory tree matching the paths expected by the current scripts.
# This only copies public text assets; native binaries and vendor libraries
# must be built or obtained separately for the matching device.
set -euo pipefail

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
stage=${1:-"$repo/out/stage"}
if [ -d "$stage" ] && [ -n "$(find "$stage" -mindepth 1 -print -quit)" ]; then
  echo "Stage directory is not empty: $stage" >&2
  echo "Use a new destination so old files cannot leak into a deployment." >&2
  exit 2
fi
mkdir -p "$stage/android/data/local/tmp" \
  "$stage/fedora/root" "$stage/fedora/usr/local/bin" \
  "$stage/fedora/usr/local/etc" "$stage/fedora/usr/local/share/gnome-shell-fixes" \
  "$stage/fedora/usr/local/share/gnome-shell/extensions" \
  "$stage/fedora/usr/local/share/applications" \
  "$stage/fedora/usr/local/share/glvnd/egl_vendor.d" \
  "$stage/fedora/usr/local/lib/udev/rules.d" \
  "$stage/fedora/usr/share/dbus-1/services" \
  "$stage/fedora/etc/NetworkManager/conf.d" \
  "$stage/fedora/etc/libinput" "$stage/termux/home"

android_files=(
  android-bridge.sh fedora-enter.sh fedora-session-gnome-shell.sh
  fedora-restore.sh fedora-android-settings.sh fedora-unlock.sh
  gnome-switcher.sh input-hide.sh usb-watch.sh
)
for name in "${android_files[@]}"; do
  cp -p "$repo/src/android/$name" "$stage/android/data/local/tmp/$name"
done
cp -p "$repo/src/android/haptic" "$stage/fedora/usr/local/bin/haptic"
cp -p "$repo/src/android/fedora-audio-bridge.sh" "$stage/termux/home/"
cp -p "$repo/src/android/start-audio-bridge.sh" "$stage/termux/home/"
cp -p "$repo/src/android/sensor-bridge-termux.sh" "$stage/termux/home/"

cp -p "$repo/src/fedora/gnome-shell-session-runner.sh" \
  "$repo/src/fedora/gnome-peripherals.sh" "$stage/fedora/root/"
for source in "$repo"/src/fedora/*; do
  case "${source##*/}" in
    gnome-shell-session-runner.sh|gnome-peripherals.sh|pkexec-chroot-wrapper.sh) ;;
    *) cp -p "$source" "$stage/fedora/usr/local/bin/" ;;
  esac
done
cp -p "$repo/src/fedora/pkexec-chroot-wrapper.sh" \
  "$stage/fedora/usr/local/bin/pkexec"

cp -p "$repo/config/android-brightness-map.json" "$stage/fedora/usr/local/etc/"
cp -p "$repo/config/95-wifi-preserve-mac.conf" \
  "$stage/fedora/etc/NetworkManager/conf.d/"
cp -p "$repo/config/gnome-wifi.conf" \
  "$stage/fedora/etc/NetworkManager/conf.d/90-gnome-wifi.conf"
cp -p "$repo/config/10_libhybris.json" \
  "$stage/fedora/usr/local/share/glvnd/egl_vendor.d/"
cp -p "$repo"/config/input/*.rules \
  "$stage/fedora/usr/local/lib/udev/rules.d/"
cp -p "$repo/config/input/local-overrides.quirks" \
  "$stage/fedora/etc/libinput/"
cp -a "$repo/integration/gnome-shell-fixes/." \
  "$stage/fedora/usr/local/share/gnome-shell-fixes/"
cp -a "$repo/integration/screen-blank@fedora-tab" \
  "$stage/fedora/usr/local/share/gnome-shell/extensions/"
# audit F19: the automatic android_camera_node.py daemon owns the Android camera (same bridge, scrcpy id and socket);
# the manual front/back/off launchers would interrupt or fight it, so they are not staged unless explicitly asked.
for desktop in "$repo"/integration/desktop/*.desktop; do
  case "${desktop##*/}" in
    android-camera-*.desktop)
      [ "${STAGE_MANUAL_CAMERA_LAUNCHERS:-0}" = 1 ] || continue ;;
  esac
  cp -p "$desktop" "$stage/fedora/usr/local/share/applications/"
done
cp -p "$repo/integration/org.gnome.ScreenSaver.service" \
  "$stage/fedora/usr/share/dbus-1/services/"

printf 'Staged text assets at %s\n' "$stage"
printf 'Build native helpers and provide matching device vendor libraries before deployment.\n'
