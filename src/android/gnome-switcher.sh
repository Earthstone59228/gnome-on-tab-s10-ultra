#!/system/bin/sh
# gnome-switcher.sh (2026-09-27) — "dual boot" switch: in Android, press VOL UP, VOL DOWN, VOL UP, VOL DOWN within
# 2 s to start the Fedora GNOME session (no PC needed). Started once per boot by fedora-enter.sh
# after root access is available. Leave GNOME with Log Out or the panic chord as usual.
# Rules: only while Android is UNLOCKED (never a way around Android's lock screen), only when no session runs,
# battery >= 10 %. Reads the two button devices (switcher_keys.py) without grabbing them (Android keeps working normally).
# Feedback: one 120 ms buzz = starting; two short buzzes = refused (see $LOG). Opt-out: touch $T/no-gnome-switcher
T=/data/local/tmp
R=/data/fedora
LOG=$T/gnome-switcher.log
PIDF=$T/.gnome-switcher.pid
HOURS_MIN=720
log() { echo "$(date) switcher: $*" >> "$LOG"; }
buzz() { echo "$1" > /sys/class/timed_output/vibrator/enable 2>/dev/null; }
[ -e "$T/no-gnome-switcher" ] && exit 0
# single instance: the pid file survives reboots, so also check that the pid really is a switcher (audit F11)
op=$(cat "$PIDF" 2>/dev/null)
if [ -n "$op" ] && tr '\0' ' ' < "/proc/$op/cmdline" 2>/dev/null | grep -q gnome-switcher; then exit 0; fi
echo $$ > "$PIDF"

log "armed (pattern: VOL UP, DOWN, UP, DOWN within 2 s)"
session_running() {
	# only the supervisor itself (a loose "pgrep -f name" also matched any shell whose command line mentions it)
	pgrep -f "^(/system/bin/)?sh $T/fedora-session-gnome-shell[.]sh" >/dev/null 2>&1 && return 0
	for p in $(pgrep -x gnome-shell); do [ "$(readlink /proc/$p/root)" = "$R" ] && return 0; done
	return 1
}

# The reader is a chroot python3: every GNOME session start/restore kills chroot python3 processes, so restart it
# (2026-09-27: the switcher used to exit after the first session). Stops only on opt-out or missing devices.
while [ ! -e "$T/no-gnome-switcher" ] && [ ! -e "$T/.switcher-nodev" ]; do
	# unbuffered, non-grabbing reader of both button devices (runs in the chroot; defex_off is loaded by fedora-enter.sh)
	chroot "$R" /usr/bin/python3 -u /usr/local/bin/switcher_keys.py 2>>"$LOG" | while read -r ev; do
		[ "$ev" = NODEVICES ] && { log "no button devices found"; touch "$T/.switcher-nodev"; break; }
		[ "$ev" = TRIGGER ] || continue
		if session_running; then log "pattern seen but a session is already running — ignored"; continue; fi
		if ! timeout 10 runcon u:r:shell:s0 /system/bin/dumpsys window 2>/dev/null | grep -q "isKeyguardShowing=false"; then
			log "refused: Android is locked (unlock Android first)"; buzz 40; sleep 0.15; buzz 40; continue
		fi
		cap=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null)
		if [ -n "$cap" ] && [ "$cap" -lt 10 ]; then log "refused: battery $cap %"; buzz 40; sleep 0.15; buzz 40; continue; fi
		log "pattern accepted: starting GNOME (time-box $HOURS_MIN min)"
		buzz 120
		setsid sh "$T/fedora-session-gnome-shell.sh" "$HOURS_MIN" >> "$LOG" 2>&1 < /dev/null &
		sleep 20   # the session grabs the buttons; no re-trigger while it starts
	done
	session_running && log "reader stopped (session running) - restarting when it ends" || log "reader stopped - restarting"
	while session_running; do sleep 5; done
	sleep 5
done
rm -f "$T/.switcher-nodev"
log "switcher exiting"
rm -f "$PIDF"
