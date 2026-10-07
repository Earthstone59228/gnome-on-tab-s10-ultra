#!/system/bin/sh
# UN-AUDITED test helper (2026-10-07): one s2idle suspend with an RTC safety wake.
# usage: fedora-suspend-test.sh <A|B|C> [secs]
#   A = suspend, panel left powered, RTC wake (default 10 s)
#   B = like A but meant for pressing the power button to wake (default 60 s RTC fallback)
#   C = bl_power=4 (panel off) before suspend, bl_power=0 after resume
# Run detached from the root tool; adb may drop during suspend. Log: /data/local/tmp/suspend-test.log
L=/data/local/tmp/suspend-test.log
MODE=${1:-A}
case "$MODE" in B) DEF=60;; *) DEF=10;; esac
SECS=${2:-$DEF}
RTC=/sys/class/rtc/rtc0
BL=/sys/class/backlight/panel/bl_power
S=/sys/power/suspend_stats
log() { echo "$(date +%T) $*" >> "$L"; }

log "start mode=$MODE secs=$SECS success=$(cat $S/success) fail=$(cat $S/fail) bl=$(cat $BL)"
NOW=$(cat $RTC/since_epoch)
[ -n "$NOW" ] || { log "no rtc since_epoch, abort"; exit 1; }
trap 'echo 0 > $BL; echo 0 > $RTC/wakealarm' EXIT HUP INT TERM
echo 0 > $RTC/wakealarm
echo $((NOW + SECS)) > $RTC/wakealarm
[ -n "$(cat $RTC/wakealarm)" ] || { log "alarm not armed, abort"; exit 1; }
log "alarm set: now=$NOW alarm=$(cat $RTC/wakealarm)"
[ "$MODE" = C ] && { echo 4 > $BL; log "bl_power=4"; }
sync
echo mem > /sys/power/state 2>>"$L"
rc=$?
echo 0 > $BL
echo 0 > $RTC/wakealarm
log "resumed rc=$rc rtc=$(cat $RTC/since_epoch) success=$(cat $S/success) fail=$(cat $S/fail) last_failed_dev=$(cat $S/last_failed_dev) bl=$(cat $BL)"
