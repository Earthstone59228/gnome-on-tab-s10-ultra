#!/system/bin/sh
# Stage 3: install integrated launch scripts only after supervised runtime admission.
set -eu
T=/data/local/tmp
B=$T/dummy-ime-backup-20261007
HERE=$(cd "$(dirname "$0")" && pwd)
[ -f "$HERE/INTEGRATION_REVIEWED" ] || { echo 'Independent integration review marker missing'; exit 1; }
[ -f "$T/dummy-ime-admission-20261007/ADMISSION_PASSED" ] || exit 1
cmp -s "$HERE/SHA256SUMS" "$T/dummy-ime-admission-20261007/ADMISSION_PASSED" || exit 1
[ "$(getprop init.svc.surfaceflinger)" = running ] && [ "$(getprop init.svc.vendor.hwcomposer-3-2)" = running ] || exit 1
! pidof sfsentinel >/dev/null 2>&1 || exit 1
! pidof gnome-shell >/dev/null 2>&1 || exit 1
[ ! -e "$T/.fedora-session-phase" ] && [ ! -e "$T/.fedora-session-ime" ] && [ ! -e "$B" ] || exit 1
[ "$(md5sum "$T/fedora-session-gnome-shell.sh" | cut -d ' ' -f1)" = 598b853b0657b06a684cf3739bba9805 ] || exit 1
[ "$(md5sum "$T/fedora-android-settings.sh" | cut -d ' ' -f1)" = f46d9f769df0a96f15c6291510db3ca2 ] || exit 1
[ "$(md5sum "$T/fedora-restore.sh" | cut -d ' ' -f1)" = 448466e8fcaf972871874adf583d115a ] || exit 1
[ ! -e "$T/fedora-session-ime.sh" ] || exit 1
package_path=$(timeout -k 2 15 runcon u:r:shell:s0 /system/bin/cmd package path org.fedora.sessionime)
case "$package_path" in package:/*/base.apk) :;; *) exit 1;; esac
APK=${package_path#package:}
[ "$(sha256sum "$APK" | cut -d ' ' -f1)" = "$(sha256sum "$HERE/fedora-session-inert-ime.apk" | cut -d ' ' -f1)" ] || exit 1
(cd "$HERE" && sha256sum -c SHA256SUMS) || exit 1
for f in fedora-session-ime.sh fedora-session-gnome-shell.sh fedora-android-settings.sh; do sh -n "$HERE/$f"; done
umask 077
mkdir "$B"
for f in fedora-session-gnome-shell.sh fedora-android-settings.sh; do
 cp -p "$T/$f" "$B/$f"
 cp -p "$T/$f" "$T/$f.pre-dummy-ime-20261007"
done
# APK already admitted; this stage performs no keyboard switch.
cp "$HERE/fedora-session-ime.sh" "$T/fedora-session-ime.sh"
chmod 755 "$T/fedora-session-ime.sh"
restorecon "$T/fedora-session-ime.sh" 2>/dev/null || true
for f in fedora-android-settings.sh fedora-session-gnome-shell.sh; do
 cp -p "$T/$f" "$T/$f.dummy-ime-new"
 cat "$HERE/$f" > "$T/$f.dummy-ime-new"
 chmod 755 "$T/$f.dummy-ime-new"
 cmp -s "$HERE/$f" "$T/$f.dummy-ime-new"
 mv -f "$T/$f.dummy-ime-new" "$T/$f"
done
# Pin installed code paths for future launcher and direct supervisor entry.
sha256sum "$APK" "$T/fedora-session-ime.sh" "$T/fedora-session-gnome-shell.sh" "$T/fedora-android-settings.sh" > "$T/.fedora-ime-runtime-admitted.tmp"
mv "$T/.fedora-ime-runtime-admitted.tmp" "$T/.fedora-ime-runtime-admitted"
echo 'Integrated scripts installed with hash-pinned Android admission gate. No session started.' 
