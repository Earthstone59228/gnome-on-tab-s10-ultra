#!/system/bin/sh
# 2026-10-07: audited once (GO-WITH-CHANGES, all 7 findings applied); NOT yet live-tested.
# "Power button = deep sleep" daemon for the Fedora GNOME session (Android-side, root).
# Follows org.fedoratab.ScreenBlank.Blanked (power button / cover set it) via gdbus monitor; when the screen has been
# blanked for SLEEPD_SETTLE seconds it hands over to fedora-suspend-dpms.sh (display off -> s2idle loop with key-state
# guard). On any user wake the engine turns the display back on and unblanks. After ANY engine return the daemon will
# not re-arm until the screen has been seen unblanked (no wake->sleep loops).
# Start (inside a live session, detached):  setsid sh /data/local/tmp/fedora-sleepd.sh >/dev/null 2>&1 &
# Stop: touch /data/local/tmp/sleepd.stop  -- NOTE (mksh): the stop file / SIGTERM only take effect when the engine returns
# (i.e. after a wake) or within SETTLE seconds when idle; a sleeping daemon cannot be stopped without waking the tablet.
# Log: /data/local/tmp/sleepd.log
T=/data/local/tmp
R=/data/fedora
L=$T/sleepd.log
STATEF=$T/sleepd.blank
STOP=$T/sleepd.stop
LOCK=$T/sleepd.lock
SETTLE=${SLEEPD_SETTLE:-4}
setstate() { echo "$1" > "$STATEF.n" && mv "$STATEF.n" "$STATEF"; }
log() { echo "$(date +%T) sleepd: $*" >> "$L"; }

if ! mkdir "$LOCK" 2>/dev/null; then
	op=$(cat "$LOCK/pid" 2>/dev/null)
	if [ -n "$op" ] && kill -0 "$op" 2>/dev/null && grep -q fedora-sleepd "/proc/$op/cmdline" 2>/dev/null; then
		log "already running (pid $op), exit"; exit 1
	fi
	log "stale lock (pid '$op') removed"
	rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || { log "cannot take lock, exit"; exit 1; }
fi
echo $$ > "$LOCK/pid"
rm -f "$STOP"
GS=""
for p in $(pgrep -x gnome-shell); do [ "$(readlink /proc/$p/root)" = "$R" ] && GS=$p; done
[ -n "$GS" ] || { log "no gnome-shell, exit"; rm -rf "$LOCK"; exit 1; }
resolve() {
	GS=""
	for p in $(pgrep -x gnome-shell); do [ "$(readlink /proc/$p/root)" = "$R" ] && GS=$p; done
	[ -n "$GS" ] || return 1
	XR=$(tr '\0' '\n' < /proc/$GS/environ | grep "^XDG_RUNTIME_DIR=" | cut -d= -f2-)
	DB=$(tr '\0' '\n' < /proc/$GS/environ | grep "^DBUS_SESSION_BUS_ADDRESS=" | cut -d= -f2-)
	[ -n "$XR" ] && [ -n "$DB" ]
}
envv() { tr '\0' '\n' < /proc/$GS/environ | grep "^$1=" | cut -d= -f2-; }
XR=$(envv XDG_RUNTIME_DIR); DB=$(envv DBUS_SESSION_BUS_ADDRESS)
[ -n "$XR" ] && [ -n "$DB" ] || { log "no bus env, exit"; rm -rf "$LOCK"; exit 1; }
inch() { runcon u:r:untrusted_app:s0 nsenter -t "$GS" -m -- chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin HOME=/root XDG_RUNTIME_DIR="$XR" DBUS_SESSION_BUS_ADDRESS="$DB" "$@"; }
getblank() { inch timeout 5 gdbus call --session --dest org.gnome.Shell --object-path /org/fedoratab/ScreenBlank \
	--method org.freedesktop.DBus.Properties.Get org.fedoratab.ScreenBlank Blanked 2>/dev/null; }
DPMS=/sys/class/drm/card0-DSI-1/dpms
psm0() { inch timeout 10 gdbus call --session --dest org.gnome.Mutter.DisplayConfig --object-path /org/gnome/Mutter/DisplayConfig \
	--method org.freedesktop.DBus.Properties.Set org.gnome.Mutter.DisplayConfig PowerSaveMode "<int32 0>" >> "$L" 2>&1; }

cleanup() {
	{ [ -f "$T/sleepd.bt-state" ] || [ -f "$T/sleepd.bt-state.restoring" ]; } && sh "$T/fedora-bt-quiet.sh" restore >/dev/null 2>&1
	for p in $(pgrep -f 'gdbus monitor --session --dest org.gnome.Shell'); do kill -9 "$p" 2>/dev/null; done
	kill -9 "$MON" 2>/dev/null
	rm -rf "$LOCK" 2>/dev/null
}
trap cleanup EXIT
trap '' HUP
trap 'exit 0' INT TERM

# crash recovery: a previous engine/daemon may have died with Bluetooth quieted — put the snapshot back first
[ -f "$T/sleepd.bt-state" ] || [ -f "$T/sleepd.bt-state.restoring" ] && { log "restoring Bluetooth snapshot left by an earlier run"; sh "$T/fedora-bt-quiet.sh" restore; }
# the screen-blank extension may not be exported yet when the supervisor starts us: retry for ~30 s
B=""
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
	B=$(getblank)
	case "$B" in *true*|*false*) break;; esac
	sleep 2
done
case "$B" in *true*) setstate 1;; *false*) setstate 0;; *) log "cannot read Blanked ($B), exit"; exit 1;; esac
( inch gdbus monitor --session --dest org.gnome.Shell --object-path /org/fedoratab/ScreenBlank 2>/dev/null | while read -r line; do
	case "$line" in
		*"'Blanked': <true>"*) setstate 1;;
		*"'Blanked': <false>"*) setstate 0;;
	esac
done ) &
MON=$!
log "started gs=$GS initial blanked=$(cat $STATEF) settle=${SETTLE}s"

fails=0
while [ ! -f "$STOP" ]; do
	[ "$(readlink /proc/$GS/root 2>/dev/null)" = "$R" ] || { log "gnome-shell $GS gone or changed, exit"; exit 0; }
	kill -0 "$MON" 2>/dev/null || { log "monitor died, exit"; exit 1; }
	read -r st < "$STATEF"
	if [ "$st" = 1 ]; then
		sleep "$SETTLE"
		read -r st < "$STATEF"
		[ "$st" = 1 ] || continue
		# fresh authoritative read (the monitor may have missed an event or died)
		case "$(getblank)" in *true*) ;; *) log "fresh Blanked read is not true -> not sleeping"; setstate 0; continue;; esac
		t0=$(date +%s)
		log "screen blanked -> starting sleep engine"
		SLEEP_STATE_FILE="$STATEF" UNBLANK_ON_WAKE=1 sh $T/fedora-suspend-dpms.sh 30 -1
		rc=$?
		dt=$(( $(date +%s) - t0 ))
		log "sleep engine returned rc=$rc after ${dt}s"
		if [ "$rc" != 2 ] && [ "$(cat $DPMS)" != On ]; then log "engine rc=$rc but dpms=$(cat $DPMS) -> forcing PowerSaveMode 0"; resolve && psm0; sleep 1; [ "$(cat $DPMS)" = On ] || rc=2; fi
		if [ "$rc" = 2 ]; then
			log "!!! engine says the display did not come back; retrying PowerSaveMode 0 every 5 s until dpms=On"
			n=0
			while [ "$(cat $DPMS)" != On ] && [ $n -lt 60 ]; do resolve && psm0; sleep 5; n=$((n + 1)); done
			log "dpms=$(cat $DPMS) after $n retries; exiting daemon (needs a manual restart)"
			exit 1
		fi
		if [ "$rc" != 0 ]; then
			# engine aborted (not ready / key held / unreadable key state ...): retry while still blanked, give up after 3
			fails=$((fails + 1))
			[ "$fails" -ge 3 ] && { log "3 engine failures in a row, giving up"; exit 1; }
			sleep 5
			continue
		fi
		fails=0
		# do not re-arm until the screen has been seen unblanked (prevents wake -> sleep loops)
		w=0
		while :; do
			read -r st < "$STATEF"
			[ "$st" = 1 ] || break
			[ ! -f "$STOP" ] || break
			sleep 1; w=$((w + 1))
			[ $w = 10 ] && log "still blanked 10 s after wake; waiting for an unblank"
		done
	else
		sleep 1
	fi
done
log "stop file seen, exit"
