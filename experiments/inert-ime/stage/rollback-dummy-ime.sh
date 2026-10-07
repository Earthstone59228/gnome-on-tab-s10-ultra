#!/system/bin/sh
# Roll back any completed/partial stage while Android owns the display.
set -eu
T=/data/local/tmp
HERE=$(cd "$(dirname "$0")" && pwd)
B=$T/dummy-ime-backup-20261007
RECEIPT=$T/dummy-ime-apk-installed-20261007
[ "$(getprop init.svc.surfaceflinger)" = running ] && [ "$(getprop init.svc.vendor.hwcomposer-3-2)" = running ] || exit 1
! pidof sfsentinel >/dev/null 2>&1 || exit 1
! pidof gnome-shell >/dev/null 2>&1 || exit 1
[ ! -e "$T/.fedora-session-phase" ] || exit 1
[ -f "$RECEIPT/apk.sha256" ] || exit 1
HELPER=$HERE/fedora-session-ime.sh
if [ -d "$T/.fedora-session-ime" ]; then sh "$HELPER" restore final; fi
now=$(timeout -k 2 15 runcon u:r:shell:s0 /system/bin/settings --user 0 get secure default_input_method)
[ "$now" != org.fedora.sessionime/.InertIme ] || exit 1
sh "$HELPER" restored-binding-check
old_rc=0
package_path=$(timeout -k 2 15 runcon u:r:shell:s0 /system/bin/cmd package path org.fedora.sessionime) || old_rc=$?
if [ "$old_rc" = 0 ]; then
    case "$package_path" in package:/*/base.apk) :;; *) exit 1;; esac
    APK=${package_path#package:}
    [ "$(sha256sum "$APK" | cut -d ' ' -f1)" = "$(cat "$RECEIPT/apk.sha256")" ] || exit 1
else
    [ "$old_rc" = 1 ] && [ -z "$package_path" ] || exit 1
fi
# Invalidate future launch immediately; restored baseline has no IME gate.
rm -f "$T/.fedora-ime-runtime-admitted"
if [ -d "$B" ]; then
    for f in fedora-android-settings.sh fedora-session-gnome-shell.sh; do
        cp -p "$B/$f" "$T/$f.dummy-ime-rollback"
        cmp -s "$B/$f" "$T/$f.dummy-ime-rollback"
        mv -f "$T/$f.dummy-ime-rollback" "$T/$f"
    done
fi
if [ "$old_rc" = 0 ]; then
    result=$(timeout -k 2 60 runcon u:r:shell:s0 /system/bin/cmd package uninstall --user 0 org.fedora.sessionime)
    [ "$result" = Success ] || exit 1
fi
rm -f "$T/fedora-session-ime.sh"
# Receipts and backups retained as evidence; admission no longer authorizes integration.
rm -f "$T/dummy-ime-admission-20261007/ADMISSION_PASSED"
echo 'Rolled back; receipts and backups retained.'
