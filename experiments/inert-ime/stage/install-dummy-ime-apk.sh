#!/system/bin/sh
# Stage 1: APK installation only. No keyboard switch or supervisor changes.
set -eu
T=/data/local/tmp
HERE=$(cd "$(dirname "$0")" && pwd)
RECEIPT=$T/dummy-ime-apk-installed-20261007
[ -f "$HERE/APK_ADMISSION_REVIEWED" ] || { echo 'Independent admission-script review marker missing'; exit 1; }
[ "$(getprop init.svc.surfaceflinger)" = running ] && [ "$(getprop init.svc.vendor.hwcomposer-3-2)" = running ] || exit 1
! pidof sfsentinel >/dev/null 2>&1 || exit 1
! pidof gnome-shell >/dev/null 2>&1 || exit 1
[ ! -e "$T/.fedora-session-phase" ] && [ ! -e "$T/.fedora-session-ime" ] && [ ! -e "$RECEIPT" ] || exit 1
(cd "$HERE" && sha256sum -c SHA256SUMS)
old_rc=0
old=$(timeout -k 2 15 runcon u:r:shell:s0 /system/bin/cmd package path org.fedora.sessionime) || old_rc=$?
[ "$old_rc" = 1 ] && [ -z "$old" ] || exit 1
umask 077
mkdir "$RECEIPT"
sha256sum "$HERE/fedora-session-inert-ime.apk" | cut -d ' ' -f1 > "$RECEIPT/apk.sha256"
result=$(timeout -k 2 60 runcon u:r:shell:s0 /system/bin/cmd package install --user 0 "$HERE/fedora-session-inert-ime.apk")
[ "$result" = Success ] || { printf '%s\n' "$result"; exit 1; }
echo 'APK installed only. Android keyboard and supervisor unchanged. Admission is a separate supervised step.'
