#!/system/bin/sh
# Stage 2: supervised Android-only switch, exact dummy-process rebind, and restore.
# Run foreground. Never run during GNOME or without a supervising operator.
set -eu
T=/data/local/tmp
HERE=$(cd "$(dirname "$0")" && pwd)
RECEIPT=$T/dummy-ime-apk-installed-20261007
RECORD=$T/dummy-ime-admission-20261007
HELPER=$HERE/fedora-session-ime.sh
[ -f "$HERE/APK_ADMISSION_REVIEWED" ] || exit 1
[ "${1:-}" = supervised-android-only ] || { echo 'Explicit supervised-android-only argument required'; exit 1; }
[ "$(getprop init.svc.surfaceflinger)" = running ] && [ "$(getprop init.svc.vendor.hwcomposer-3-2)" = running ] || exit 1
! pidof sfsentinel >/dev/null 2>&1 || exit 1
! pidof gnome-shell >/dev/null 2>&1 || exit 1
[ ! -e "$T/.fedora-session-phase" ] && [ ! -e "$T/.fedora-session-ime" ] && [ ! -e "$RECORD" ] || exit 1
[ -f "$RECEIPT/apk.sha256" ] || exit 1
(cd "$HERE" && sha256sum -c SHA256SUMS)
package_path=$(timeout -k 2 15 runcon u:r:shell:s0 /system/bin/cmd package path org.fedora.sessionime)
case "$package_path" in package:/*/base.apk) :;; *) exit 1;; esac
APK=${package_path#package:}
[ "$(sha256sum "$APK" | cut -d ' ' -f1)" = "$(cat "$RECEIPT/apk.sha256")" ] || exit 1
[ "$(sha256sum "$HERE/fedora-session-inert-ime.apk" | cut -d ' ' -f1)" = "$(cat "$RECEIPT/apk.sha256")" ] || exit 1
umask 077
mkdir "$RECORD"
# Package installed by stage 1; do not force-stop Samsung or any Android service.
success=0
ss=$(pidof system_server)
case "$ss" in ''|*[!0-9]*) exit 1;; esac
echo "$ss" > "$RECORD/system-server.before"
cleanup() {
    trap - EXIT INT TERM HUP
    restored=0; n=0
    while [ "$n" -lt 3 ]; do
        if sh "$HELPER" restore final; then restored=1; break; fi
        n=$((n + 1)); sleep 2
    done
    if [ "$restored" = 1 ] && [ "$success" = 1 ] && [ "$(pidof system_server)" = "$ss" ]; then
        # Evidence pins every reviewed staged artifact, not just the APK.
        cp "$HERE/SHA256SUMS" "$RECORD/ADMISSION_PASSED.tmp"
        mv "$RECORD/ADMISSION_PASSED.tmp" "$RECORD/ADMISSION_PASSED"
        echo 'Admission passed: connected inert binding, dummy-only rebind, original connected and exact settings restored.'
        exit 0
    fi
    echo 'Admission failed; no integration allowed. Originals retained if restoration failed.' >&2
    # An unbounded but individually timed recovery worker continues after bounded foreground admission.
    # Display readiness remains enforced by the helper. It never changes display-service state.
    if [ "$restored" != 1 ]; then
        nohup sh "$HERE/recover-dummy-ime.sh" "$HELPER" >> "$RECORD/recovery.log" 2>&1 </dev/null &
        echo "$!" > "$RECORD/recovery.pid"
    fi
    exit 1
}
trap cleanup EXIT
trap 'exit 1' INT TERM HUP
sh "$HELPER" prepare
sh "$HELPER" binding-evidence > "$RECORD/pre-rebind.binding"
# Record and validate exact package UID and one process cmdline immediately before kill.
packages=$(timeout -k 2 15 runcon u:r:shell:s0 /system/bin/cmd package list packages -U org.fedora.sessionime)
uid=$(printf '%s\n' "$packages" | sed -n 's/^package:org\.fedora\.sessionime uid:\([0-9][0-9]*\)$/\1/p')
case "$uid" in ''|*[!0-9]*) exit 1;; esac
[ "$uid" -ge 10000 ] || exit 1
pid=$(pidof org.fedora.sessionime)
case "$pid" in ''|*[!0-9]*) exit 1;; esac
[ "$(tr '\000' '\n' < "/proc/$pid/cmdline" | head -n 1)" = org.fedora.sessionime ] || exit 1
real_uid=$(awk '/^Uid:/ {print $2}' "/proc/$pid/status")
[ "$real_uid" = "$uid" ] || exit 1
# Recheck binding/display before killing only this package instance; no global restart.
sh "$HELPER" verify
[ "$(getprop init.svc.surfaceflinger)" = running ] && [ "$(getprop init.svc.vendor.hwcomposer-3-2)" = running ] || exit 1
[ "$(tr '\000' '\n' < "/proc/$pid/cmdline" | head -n 1)" = org.fedora.sessionime ] || exit 1
[ "$(awk '/^Uid:/ {print $2}' "/proc/$pid/status")" = "$uid" ] || exit 1
echo "$pid $uid" > "$RECORD/dummy-process.before"
[ "$(pidof system_server)" = "$ss" ] || exit 1
kill -9 "$pid"
new_identity() {
    newpid=$(pidof org.fedora.sessionime) || return 1
    case "$newpid" in ''|*[!0-9]*) return 1;; esac
    [ "$newpid" != "$pid" ] || return 1
    [ "$(tr '\000' '\n' < "/proc/$newpid/cmdline" | head -n 1)" = org.fedora.sessionime ] || return 1
    [ "$(awk '/^Uid:/ {print $2}' "/proc/$newpid/status")" = "$uid" ] || return 1
    # Remote current-IME dump marker must identify this NEW process, not a stale invoker.
    remote=$(sh "$HELPER" service-identity) || return 1
    [ "$remote" = "$newpid $uid" ] || return 1
    [ "$(pidof system_server)" = "$ss" ] || return 1
}
n=0; good=0
while [ "$n" -lt 12 ]; do
    sleep 1
    if new_identity; then good=$((good + 1)); else good=0; fi
    [ "$good" -ge 3 ] && break
    n=$((n + 1))
done
[ "$good" -ge 3 ] || exit 1
sh "$HELPER" binding-evidence > "$RECORD/post-rebind.binding"
cmp -s "$RECORD/pre-rebind.binding" "$RECORD/post-rebind.binding" || exit 1
echo "$newpid $uid" > "$RECORD/dummy-process.after"
pidof system_server > "$RECORD/system-server.after"
success=1
# EXIT trap performs and verifies original restoration before issuing success evidence.
exit 0
