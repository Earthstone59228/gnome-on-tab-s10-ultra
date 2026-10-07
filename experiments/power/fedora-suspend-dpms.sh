#!/system/bin/sh
# 2026-10-07: audited twice (GO-WITH-CHANGES, applied); key-state guard audit found+fixed a function-order bug. Test D: suspend the way stock Android does -- display OFF first, then s2idle.
# Why (Samsung kernel source, mtk_drm_drv.c): mtk_drm_sys_suspend() runs drm_atomic_helper_suspend() only if a CRTC
# wakelock (disp_crtc0_wakelock) is still active; the PM notifier has already set kernel_pm.status=SUSPEND, which makes
# mtk_atomic_commit() block -> self-deadlock (test A, softdog panic). mutter PowerSaveMode=3 does a blocking atomic
# commit with CRTC ACTIVE=0 -> mtk_drm_crtc_suspend() releases the wakelock inside that commit.
# dpms=Off alone is NOT proof (connector->dpms is set before the CRTC is disabled); /proc/mtkfb "CRTC0 wk active:0" is.
# total: >0 keep re-suspending that many seconds; 0 = one cycle; <0 = until a wake condition (daemon mode).
# env (set by fedora-sleepd.sh): SLEEP_STATE_FILE (contains 1 while the screen is blanked; sleep ends when it is not 1),
#   UNBLANK_ON_WAKE=1 (call ScreenBlank.Unblank after waking).
# usage: fedora-suspend-dpms.sh [secs] [total]  (total>0: keep re-suspending for that many seconds, logging each wake reason)   (RTC safety wake, default 20). Run with setsid, detached, in a live session.
# If the panel stays dark after resume: power button/touch, or over adb: gdbus Set PowerSaveMode <int32 0> in the session.
L=/data/local/tmp/suspend-test.log
R=/data/fedora
SECS=${1:-20}
TOTAL=${2:-0}
RTC=/sys/class/rtc/rtc0
S=/sys/power/suspend_stats
DPMS=/sys/class/drm/card0-DSI-1/dpms
log() { echo "$(date +%T) D: $*" >> "$L"; }
wkoff() { timeout 3 cat /proc/mtkfb 2>/dev/null | grep -q 'CRTC0 wk active:0'; }
ready() { [ "$(cat $DPMS)" = Off ] && wkoff; }
keyheld() { timeout 5 chroot "$R" /usr/bin/python3 /usr/local/bin/keystate.py 2>/dev/null; }

resolve() {
	GS=""
	for p in $(pgrep -x gnome-shell); do [ "$(readlink /proc/$p/root)" = "$R" ] && GS=$p; done
	[ -n "$GS" ] || return 1
	XR=$(tr '\0' '\n' < /proc/$GS/environ | grep "^XDG_RUNTIME_DIR=" | cut -d= -f2-)
	DB=$(tr '\0' '\n' < /proc/$GS/environ | grep "^DBUS_SESSION_BUS_ADDRESS=" | cut -d= -f2-)
	[ -n "$XR" ] && [ -n "$DB" ]
}
resolve || { log "no gnome-shell / bus env, abort"; exit 1; }
inch() { runcon u:r:untrusted_app:s0 nsenter -t "$GS" -m -- chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin HOME=/root XDG_RUNTIME_DIR="$XR" DBUS_SESSION_BUS_ADDRESS="$DB" "$@"; }
psm() { inch timeout 10 gdbus call --session --dest org.gnome.Mutter.DisplayConfig --object-path /org/gnome/Mutter/DisplayConfig \
	--method org.freedesktop.DBus.Properties.Set org.gnome.Mutter.DisplayConfig PowerSaveMode "<int32 $1>" >> "$L" 2>&1; }
unblank() { inch timeout 10 gdbus call --session --dest org.gnome.Shell --object-path /org/fedoratab/ScreenBlank \
	--method org.fedoratab.ScreenBlank.Unblank >> "$L" 2>&1; }
cleanup() { echo 0 > $RTC/wakealarm; [ "$(cat $DPMS)" = On ] || { resolve && psm 0; }; }

log "start gs=$GS secs=$SECS dpms=$(cat $DPMS) wk=$(timeout 3 grep -o 'CRTC0 wk active:[01]' /proc/mtkfb) success=$(cat $S/success) fail=$(cat $S/fail)"
[ -n "$(cat $RTC/since_epoch)" ] || { log "no rtc since_epoch, abort"; exit 1; }
trap cleanup EXIT
trap '' HUP
trap 'exit 1' INT TERM

# precheck: key state must be readable (rc 1 = readable, nothing held) or we refuse to blank/sleep at all
KH=$(keyheld); KRC=$?
[ "$KRC" = 1 ] || { log "keystate precheck rc=$KRC [$KH], abort (no sleep)"; exit 1; }

PWF=/data/fedora/fake-sessionmanager.log
pwcount() { grep -a -c "power button" $PWF; }
PW0=$(pwcount)
PWRWAKE=0
psm 3
i=0
while ! ready && [ $i -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
ready || { log "not ready: dpms=$(cat $DPMS) wk=$(timeout 3 grep -o 'CRTC0 wk active:[01]' /proc/mtkfb), abort (no suspend)"; exit 1; }
log "display off, wakelock released after ${i}x0.1s"
sleep 1

T0=$(date +%s)
cycle=0
while :; do
	cycle=$((cycle + 1))
	[ "$(wc -c < "$L")" -gt 1000000 ] && { tail -c 200000 "$L" > "$L.tmp" && mv "$L.tmp" "$L"; }
	echo 0 > $RTC/wakealarm
	NOW=$(cat $RTC/since_epoch)
	echo $((NOW + SECS)) > $RTC/wakealarm
	[ -n "$(cat $RTC/wakealarm)" ] || { log "alarm not armed, abort"; exit 1; }
	ready || { log "display/wakelock came back before suspend, abort"; exit 1; }
	log "cycle $cycle: alarm armed, suspending: now=$NOW alarm=$(cat $RTC/wakealarm)"
	sync
	# EBUSY (-16) alarmtimer / -1 clkchk = Android-style transient refusals; its suspend service just retries.
	try=0
	while [ $try -lt 40 ]; do
		ready || { log "display/wakelock came back before suspend (try $try), abort"; exit 1; }
		kill -0 "$GS" 2>/dev/null || { log "gnome-shell gone before suspend"; break 2; }
		[ -z "$SLEEP_STATE_FILE" ] || [ "$(cat "$SLEEP_STATE_FILE" 2>/dev/null)" = 1 ] || { log "unblanked before suspend -> ending sleep"; break 2; }
		[ "$(pwcount)" = "$PW0" ] || { log "power event before suspend -> ending sleep"; PWRWAKE=1; break 2; }
		KH=$(keyheld); KRC=$?
		[ "$KRC" = 1 ] || { log "KEY state before suspend rc=$KRC [$KH] -> ending sleep"; break 2; }
		echo mem > /sys/power/state 2>>"$L"
		rc=$?
		[ "$rc" = 0 ] && break
		try=$((try + 1))
		log "try $try rc=$rc errno=$(cat $S/last_failed_errno) dev=$(cat $S/last_failed_dev)"
		sleep 0.5
		[ -n "$(cat $RTC/wakealarm)" ] || { NOW=$(cat $RTC/since_epoch); echo $((NOW + SECS)) > $RTC/wakealarm; }
	done
	echo 0 > $RTC/wakealarm
	log "resumed rc=$rc rtc=$(cat $RTC/since_epoch) success=$(cat $S/success) fail=$(cat $S/fail) dpms=$(cat $DPMS) reason=[$(cat /sys/kernel/wakeup_reasons/last_resume_reason | tr '\n' ';')] slept=$(cat /sys/kernel/wakeup_reasons/last_suspend_time)"
	[ "$rc" = 0 ] || break
	# FAIL-SAFE: keep sleeping only through known-benign wakes. Any other reason (buttons incl. VOL UP = panic chord,
	# power key, touch, cover, unknown) ends the sleep so the display comes back and userspace (panic chord watcher) runs.
	# every cycle: any held VOL UP / VOL DOWN / POWER (panic chord!) or an unreadable key state ends the sleep
	KH=$(keyheld); KRC=$?
	[ "$KRC" = 1 ] || { log "KEY state rc=$KRC [$KH] -> ending sleep"; break; }
	kill -0 "$GS" 2>/dev/null || { log "gnome-shell gone -> ending sleep"; break; }
	sleep 0.3    # grace so a key event that woke us reaches fake_sessionmanager before we re-suspend
	PW1=$(pwcount)
	[ "$PW1" = "$PW0" ] || { PWRWAKE=1; log "POWER KEY event seen ($PW0 -> $PW1) reason=[$(cat /sys/kernel/wakeup_reasons/last_resume_reason)] -> ending sleep"; break; }
	if [ -n "$SLEEP_STATE_FILE" ] && [ "$(cat "$SLEEP_STATE_FILE" 2>/dev/null)" != 1 ]; then log "screen no longer blanked -> ending sleep"; break; fi
	R1=$(cat /sys/kernel/wakeup_reasons/last_resume_reason)
	case "$R1" in
		# user-wake reasons FIRST (a combined reason like "A96T3X6;rcs_irq" must not be swallowed by a benign pattern):
		# rcs_irq = PMIC interrupt line (power key / PMIC events), Volume_Up = gpio-keys VOL UP
		*rcs_irq*|*Volume*|*pmic*|*pwrkey*) case "$R1" in *rcs_irq*|*pmic*|*pwrkey*) PWRWAKE=1;; esac; log "user wake reason [$R1] -> ending sleep"; break;;
		*CCIF_AP_DATA0*|*A96T3X6*|*vcp_mboxdev*|*gpueb_mboxdev*|*mboxdev*|*MBOX_SCP_ISR*|*adsp_mailbox*|*mailbox*|*spm-irq*|*alarmtimer*|*mt6685-rtc*|"") ;;  # spm-irq = SPM system-timer wake (R12_SYSTIMER_EVENT_B), gpueb/vcp/adsp = on-chip mailboxes
		*) log "non-benign wake [$R1] -> ending sleep"; break;;
	esac
	if [ "$TOTAL" -ge 0 ]; then
		[ "$TOTAL" -gt 0 ] && [ $(( $(date +%s) - T0 )) -lt "$TOTAL" ] || break
	fi
done
ok=0
for t in 1 2 3 4 5 6 7 8; do
	resolve && psm 0
	sleep 1
	[ "$(cat $DPMS)" = On ] && { ok=1; break; }
done
if [ "$ok" != 1 ]; then
	log "!!! display did NOT come back (dpms=$(cat $DPMS)) after 8 tries; press POWER or run gdbus PowerSaveMode 0 in the session"
	exit 2
fi
if [ "$UNBLANK_ON_WAKE" = 1 ]; then
	# a power-key wake is followed by a Toggle from fake_sessionmanager; let it land first so our Unblank cannot be undone by it
	if [ "$PWRWAKE" = 1 ]; then
		w=0; while [ "$(pwcount)" = "$PW0" ] && [ $w -lt 20 ]; do sleep 0.25; w=$((w + 1)); done
		sleep 0.3
	fi
	# unblank only if still blanked (a power Toggle may already have done it)
	[ -z "$SLEEP_STATE_FILE" ] || [ "$(cat "$SLEEP_STATE_FILE" 2>/dev/null)" != 0 ] && unblank
fi
log "end dpms=$(cat $DPMS) wk=$(timeout 3 grep -o 'CRTC0 wk active:[01]' /proc/mtkfb)"
