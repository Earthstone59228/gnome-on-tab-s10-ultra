#!/system/bin/sh
# fedora-session-gnome-shell.sh — identical supervisor/watchdog/restore
# harness as the proven fedora-session.sh and fedora-session-gpu-test.sh
# (same process-group-kill discipline, same restore path, same prereq-
# assertion pattern) except it launches gnome-shell-session-runner.sh instead
# of a labwc runner, and its prereq list checks for the new pieces (mutter/
# gnome-shell binary, fake_logind.py, gnome_gbm_shim.so, dbus-daemon) instead
# of labwc/seatd (mutter needs neither — see gnome-shell-session-runner.sh's
# header). Separate file; none of the proven scripts are touched.
#
# Usage: fedora-session-gnome-shell.sh [MINUTES]  (time-box, default 2).
R=/data/fedora
T=/data/local/tmp
HB=$T/.fedora-session-gnome-shell-hb
LOG=$T/fedora-session-gnome-shell.log
log() { echo "$(date) $*" >> "$LOG"; }
# pkill -x does not match sfsentinel on this Android (returns 1 with the process alive); kill by pid instead
kill_sentinel() { for _sp in $(pidof sfsentinel 2>/dev/null); do kill -9 "$_sp" 2>/dev/null; done; }
# Same chroot-scoped kill as fedora-restore.sh: never touches Android/Termux
# processes that share a name (python3, dbus-daemon).
# Root of a process as seen from here; falls back to a live thread when the
# thread-group leader has already exited (its task->fs is gone, readlink fails).
proc_root() {
	r=$(readlink /proc/$1/root 2>/dev/null)
	if [ -z "$r" ]; then
		for t in /proc/$1/task/*; do r=$(readlink $t/root 2>/dev/null) && [ -n "$r" ] && break; done
	fi
	echo "$r"
}
chroot_kill() {
	pids=$(timeout 2 pgrep -x "$1" 2>/dev/null)
	[ "$?" = 124 ] && return 124
	hit=1
	for pid in $pids; do
		[ "$(proc_root "$pid")" = "$R" ] || continue
		kill -9 "$pid" 2>/dev/null && hit=0
	done
	return $hit
}
kill_compositors() {
	round=0
	while [ "$round" -lt 5 ]; do
		any=0
		for p in gnome-shell gnome-shell-bin mutter udevd dbus-daemon fake_logind.py python3; do
			chroot_kill "$p"
			rc=$?
			[ "$rc" = 0 ] && any=1
			[ "$rc" = 124 ] && any=1
		done
		[ "$any" = 0 ] && return 0
		round=$((round + 1))
		sleep 1
	done
	return 1
}

# ---------------- launcher path (default) ----------------
# Argument is MINUTES, not seconds (same units/reasoning as fedora-session-gpu-test.sh).
if [ "$1" != "sup" ]; then
	MINS=${1:-2}
	case "$MINS" in ''|*[!0-9]*) MINS=2;; esac
	if ! grep -q '^defex_off ' /proc/modules; then
		log "prereq: defex_off NOT loaded — aborting (SF untouched)"; echo "prereq fail: defex_off not loaded (run fedora-enter.sh)"; exit 1
	fi
	if [ "$(grep -c fedora /proc/mounts)" -lt 4 ]; then
		log "prereq: chroot mounts missing — aborting (SF untouched)"; echo "prereq fail: chroot mounts (run fedora-enter.sh)"; exit 1
	fi
	if [ ! -x "$R/bin/bash" ] || [ ! -f "$R/root/gnome-shell-session-runner.sh" ]; then
		log "prereq: gnome-shell runner missing in chroot — aborting (SF untouched)"; echo "prereq fail: gnome-shell runner"; exit 1
	fi
	if [ ! -x "$R/usr/bin/gnome-shell" ]; then
		log "prereq: gnome-shell binary missing — aborting (SF untouched)"; echo "prereq fail: gnome-shell binary"; exit 1
	fi
	if [ ! -f "$R/usr/local/bin/fake_logind.py" ]; then
		log "prereq: fake_logind.py missing in chroot — aborting (SF untouched)"; echo "prereq fail: fake_logind.py"; exit 1
	fi
	if [ ! -f "$R/usr/local/lib/gnome_gbm_shim.so" ]; then
		log "prereq: gnome_gbm_shim.so missing in chroot — aborting (SF untouched)"; echo "prereq fail: gnome_gbm_shim.so"; exit 1
	fi
	if [ ! -x "$R/usr/bin/dbus-daemon" ]; then
		log "prereq: dbus-daemon missing — aborting (SF untouched)"; echo "prereq fail: dbus-daemon"; exit 1
	fi
	if [ ! -x "$R/usr/bin/python3" ]; then
		log "prereq: python3 missing — aborting (SF untouched)"; echo "prereq fail: python3"; exit 1
	fi
	if [ ! -d "$R/usr/local/lib/hybris" ] || [ ! -d "$R/usr/local/lib/hybris-vendor" ]; then
		log "prereq: hybris staging dirs missing — aborting (SF untouched)"; echo "prereq fail: hybris staging"; exit 1
	fi
	if [ "$(getprop init.svc.vendor.hwcomposer-3-2)" != "running" ]; then
		log "prereq: vendor.hwcomposer-3-2 not running — aborting (SF untouched)"; echo "prereq fail: hwcomposer not running"; exit 1
	fi
	if [ ! -x "$T/fedora-restore.sh" ]; then
		log "prereq: fedora-restore.sh missing — aborting (SF untouched)"; echo "prereq fail: fedora-restore.sh"; exit 1
	fi
	if [ ! -f "$T/fedora-android-settings.sh" ]; then
		log "prereq: fedora-android-settings.sh missing — aborting (SF untouched)"; echo "prereq fail: fedora-android-settings.sh"; exit 1
	fi
	# audit F04: everything the recovery path needs must exist BEFORE SurfaceFlinger is ever stopped
	if [ ! -x "$R/usr/local/bin/mastercheck" ]; then
		log "prereq: $R/usr/local/bin/mastercheck missing/not executable (restore cannot test DRM master) — aborting (SF untouched)"; echo "prereq fail: mastercheck"; exit 1
	fi
	if [ ! -x "$T/sfsentinel" ]; then
		log "prereq: $T/sfsentinel missing/not executable (no SurfaceFlinger placeholder) — aborting (SF untouched)"; echo "prereq fail: sfsentinel"; exit 1
	fi
	kill_compositors
	setsid nohup sh "$0" sup "$MINS" </dev/null > "$T/fedora-supervisor-gnome-shell.log" 2>&1 &
	echo "gnome-shell session started: time-box ${MINS}m, supervisor pid $!"
	log "launcher: supervisor detached (pid $!) (time-box ${MINS}m)"
	exit 0
fi

# ---------------- supervisor path ----------------
MINS=$2
SPID=$$
trap "" TERM INT HUP
# audit F07: phase file "<name> <deadline-epoch|0>". The watchdog below treats a LIVE supervisor that is still in a
# bring-up phase past its deadline as hung: it kills it and runs the shared restore itself (so exactly one restorer
# runs). The run phase has no deadline (the time-box/monitor own it); a normal exit removes the file.
PH=$T/.fedora-session-phase
mono() { read -r _up _ </proc/uptime; echo "${_up%%.*}"; }   # monotonic seconds: immune to wall-clock steps
# seconds 0 = no deadline (the run phase); anything else is a deadline that many seconds from now
phase() { _dl=0; [ "$2" -gt 0 ] && _dl=$(( $(mono) + $2 )); echo "$1 $_dl $SPID" > "$PH.tmp" && mv -f "$PH.tmp" "$PH"; }
phase bringup 480
# 2026-09-26 audit #3 (daily use): the supervisor and its watchdog are the only things that bring
# Android's display back, so the OOM killer must never pick them. oom_score_adj is INHERITED across
# fork/exec, so the supervisor protects itself only AFTER the runner is forked (see below) — the GNOME
# session and its apps must stay killable (independent audit finding, 2026-09-26).
log "supervisor: start (pid $SPID, time-box ${MINS}m, gnome-shell --wayland --no-x11, DRM card0)"

setsid nohup sh -c '
	trap - TERM
	HB="'"$HB"'"; LOG="'"$LOG"'"; SPID='"$SPID"'; PH="'"$PH"'"
	echo -1000 > /proc/$$/oom_score_adj 2>/dev/null
	stale=0
	wn=0
	sleep 12
	while :; do
		sleep 5
		# 2026-09-24 ROOT CAUSE of "finger touch dead in session": Android drifts
		# into mWakefulness=Dozing while SF is stopped, and the Goodix finger
		# digitizer is powered off in that state (tsp/enabled=0; the S Pen is a
		# separate chip, unaffected). Nudge a wake key ~every 20s. (The per-minute
		# `dumpsys power` evidence log was dropped after the fix was confirmed.)
		wn=$((wn + 1))
		if [ $((wn % 4)) -eq 0 ]; then
			timeout 5 runcon u:r:shell:s0 /system/bin/input keyevent KEYCODE_WAKEUP >/dev/null 2>&1
		fi
		# 2026-09-21: keep the Samsung native bootchecker dead for the whole
		# session (it reboots on PF_EX exception storms from the display
		# thread — see the supervisor-start comment block). init restarts it
		# on every framework restart, so re-stop unconditionally here.
		if [ "$(getprop init.svc.bootchecker 2>/dev/null)" != "stopped" ]; then
			runcon u:r:shell:s0 /system/bin/stop bootchecker 2>/dev/null
			echo "$(date) watchdog: re-stopped bootchecker (was $(getprop init.svc.bootchecker 2>/dev/null))" >> "$LOG"
		fi
		NOW=$(date +%s)
		HB_T=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
		if [ $((NOW - HB_T)) -gt 20 ]; then stale=$((stale + 1)); else stale=0; fi
		sdead=0
		kill -0 "$SPID" 2>/dev/null || sdead=1
		if [ "$sdead" = 0 ] && [ -f "$PH" ]; then
			pname=""; pdl=0; psp=""
			read -r pname pdl psp < "$PH" 2>/dev/null
			if [ -n "$psp" ] && [ "$psp" != "$SPID" ]; then
				echo "$(date) watchdog: a newer supervisor ($psp) owns the session — this watchdog ($SPID) retires" >> "$LOG"
				exit 0
			fi
			case "$pdl" in ""|*[!0-9]*) pdl=0;; esac
			read -r mup _ < /proc/uptime; MNOW=${mup%%.*}
			if [ "$pdl" -gt 0 ] && [ "$MNOW" -gt "$pdl" ]; then
				echo "$(date) watchdog: supervisor $SPID still in phase $pname $((MNOW - pdl))s past its deadline — killing it, restore follows" >> "$LOG"
				kill -9 "$SPID" 2>/dev/null
				sleep 1
				sdead=1; stale=2
			fi
		fi
		if [ "$stale" -ge 2 ] && [ "$sdead" = 1 ]; then
			echo "$(date) watchdog: heartbeat stale x2 + supervisor $SPID dead — invoking shared restore" >> "$LOG"
			sh "'"$T"'"/fedora-restore.sh
			wrc=$?
			echo "$(date) watchdog: restore invocation returned rc=$wrc" >> "$LOG"
			stale=0
			if [ "$wrc" = 0 ]; then
				rm -f "$PH"
				echo "$(date) watchdog: restore succeeded — watchdog done" >> "$LOG"
				exit 0
			fi
			echo "$(date) watchdog: restore failed — re-armed" >> "$LOG"
		fi
	done
' </dev/null >/dev/null 2>&1 &
WPID=$!

# 2026-09-24 fix: this used to forward only ONE argument ("$2"), so
# `runcon_shell svc power stayon true` actually ran `svc power` alone and the
# stay-awake setting was never applied by the script. Forward everything.
runcon_shell() { _c=$1; shift; runcon u:r:shell:s0 /system/bin/"$_c" "$@"; }

# audit F05/F06: abort BEFORE SurfaceFlinger is stopped and unwind whatever bring-up already did (all of it is safe
# while SF is still up). Every variable is optional; the ones not set yet are simply empty.
abort_before_sf() {
	log "supervisor: ABORT — $1; SurfaceFlinger NOT stopped, unwinding"
	[ -n "$AWPID" ] && kill -9 "$AWPID" 2>/dev/null
	[ -n "$IHON" ] && { sh "$T/input-hide.sh" off; IHON=""; }
	[ -n "$PGPID" ] && { kill "$PGPID" 2>/dev/null; sleep 1; kill -9 "$PGPID" 2>/dev/null; PGPID=""; }
	runcon u:r:shell:s0 /system/bin/cmd power set-wakelock release FULL_WAKE_LOCK >/dev/null 2>&1
	runcon u:r:shell:s0 /system/bin/cmd power suppress-ambient-display fedora-session false >/dev/null 2>&1
	sh "$T/fedora-android-settings.sh" restore
	kill -9 "$WPID" 2>/dev/null
	rm -f "$PH"
	exit 1
}

# 2026-09-21 incident fix (19:48:09 rescueparty_by_bootchecker reboot ~76s into
# a healthy session, decoded from /data/log/rescueparty_log): Android's
# DisplayPowerController keeps acting on screen-state changes while SF is
# stopped (idle timeout / power button). Its ColorFade path calls SF's
# createSurface against the sfsentinel fail-fast stub and throws a caught
# exception every ~5s; Samsung's watcher logs each one as a PF_EX record
# (thread android.display) into /data/log/rescueparty_log, and the SEPARATE
# Samsung native /system/bin/bootchecker (restarted by init on EVERY framework
# restart, so it was live from the 19:41 crash) counts them and issues
# `reboot,rescueparty_by_bootchecker` after ~7 in a row. That path ignores
# AOSP's disable_rescue_party entirely (verified live; only the bootchecker
# binary contains that reboot-reason string on this build).
# Defuse 1: hold Android's screen on while plugged so idle never starts a
# screen-off animation (ColorFade retries only run on a pending state change).
# User guidance still applies: do not press the power button mid-session.
# 2026-09-26 audit #3: snapshot Android's brightness mode / stay-on / screen-off timeout FIRST (restored by
# fedora-restore.sh). Android auto-brightness off for the session: GNOME owns the panel backlight now, and two
# controllers writing /sys/class/backlight/panel made GNOME's slider jump (P.1). Timeout max: stayon only
# covers "plugged in", so an unplugged session would otherwise hit Android's idle screen-off.
sh "$T/fedora-android-settings.sh" save || abort_before_sf "could not save/apply the protective Android settings (rotation, idle, crash dialogs)"
# 2026-09-27 ROOT CAUSE of the ColorFade crash loops (sessions #2, #4): `svc power stayon true` never worked from
# this root context — svc runs app_process, which fails here with CANNOT LINK libnativeloader.so — so nothing
# held Android awake: after the wake key it fell back into Doze/AOD ~4 s later, i.e. right after SF was stopped.
# Use PowerManager's own shell command (native binder, works from root): hold a FULL_WAKE_LOCK for the whole
# session and suppress AOD. Both are released by fedora-restore.sh (and die with system_server on a restart —
# the monitor re-takes them).
PWR="runcon u:r:shell:s0 /system/bin/cmd power"
$PWR suppress-ambient-display fedora-session true >/dev/null 2>&1
$PWR set-wakelock acquire FULL_WAKE_LOCK >/dev/null 2>&1
log "supervisor: FULL_WAKE_LOCK held + AOD suppressed: $($PWR set-wakelock list 2>/dev/null | grep -o 'held=[a-z]*' | head -1) aod-tokens=$($PWR list-ambient-display-suppression-tokens 2>/dev/null | tr -d '\n')"
# Wake the device NOW (SF still up, `input` works) so the finger digitizer is
# powered when the session starts; verify + log wakefulness and tsp state.
runcon u:r:shell:s0 /system/bin/input keyevent KEYCODE_WAKEUP
# 2026-09-27 live finding: waking a Dozing Android starts DisplayPowerController's screen-on sequence
# ("Blocking screen on until initial contents have been drawn"); stopping SurfaceFlinger 2 s later, in the
# middle of it, made its ColorFade hit SF -> DEAD_OBJECT -> system_server crash, and every restart without
# HWC then loops ("Timeout waiting for default display"). Wait until the display is fully on and ready;
# if it never gets there, abort BEFORE touching SurfaceFlinger (the snapshot is restored).
# 2026-09-27 (session #4): the four "ready" flags go true ~1 s after waking from Doze, but Samsung's AOD doze
# dream is still being torn down and DisplayPowerController made one more screen-state change (ColorFade ->
# DEAD_OBJECT) 3 s later. So require the whole display/power pipeline to be idle — no dream, no wakefulness
# change, no pending screen-off, no brightness ramp, no ColorFade-off animation — for 5 consecutive seconds.
display_idle() {
	pw=$(timeout 5 runcon u:r:shell:s0 /system/bin/dumpsys power 2>/dev/null)
	dp=$(timeout 5 runcon u:r:shell:s0 /system/bin/dumpsys display 2>/dev/null)
	dr=$(timeout 5 runcon u:r:shell:s0 /system/bin/dumpsys dreams 2>/dev/null)
	echo "$pw" | grep -q 'mWakefulness=Awake' && echo "$pw" | grep -q 'mWakefulnessChanging=false' &&
	echo "$dr" | grep -q 'mCurrentDream=null' &&
	echo "$dp" | grep -q 'mDisplayReadyLocked=true' && echo "$dp" | grep -q 'mPendingScreenOnUnblocker=null' &&
	echo "$dp" | grep -q 'mScreenState=ON' && echo "$dp" | grep -q 'mPendingScreenOff=false' &&
	echo "$dp" | grep -q 'mScreenBrightnessRampAnimator.isAnimating()=false' &&
	echo "$dp" | grep -q 'mColorFadeOffAnimator.isStarted()=false'
}
ready=0
stable=0
w=0
while [ "$w" -lt 30 ]; do
	if display_idle; then stable=$((stable + 1)); else stable=0; fi
	[ "$stable" -ge 5 ] && { ready=1; break; }
	sleep 1
	w=$((w + 1))
done
log "supervisor: wake nudge sent: $(echo "$pw" | grep -m1 mWakefulness=) display idle-stable=$ready after ${w}s tsp_enabled=$(cat /sys/class/sec/tsp/enabled 2>/dev/null)"
if [ "$ready" != 1 ]; then
	abort_before_sf "Android display did not settle (dream/animation still active)"
fi
# Defuse 2: archive + keep the native bootchecker dead for the whole session.
# It is a oneshot (normally 'stopped') but init auto-RESTARTS it on framework
# restart (on property:init.svc.zygote=restarting in /system/etc/init/
# bootchecker.rc) — exactly how it came alive 19:41→19:48 today. The detached
# watchdog below re-stops it continuously, closing that window. Nothing in
# Android requires it alive for stability (verified: it is 'stopped' in normal
# idle operation too).
cp /data/log/rescueparty_log "$T/rescueparty_log.$(date +%Y%m%d_%H%M%S).bak" 2>/dev/null
runcon_shell stop bootchecker
log "supervisor: bootchecker stop requested (init.svc.bootchecker=$(getprop init.svc.bootchecker))"
# Defuse 3 (2026-09-22 incident decode): `stop` alone is NOT enough. Every
# framework restart re-arms bootchecker (bootchecker.rc: on
# property:init.svc.zygote=restarting -> restart bootchecker) — incident #2
# re-armed it 9x in 42s while system_server crash-looped — and any 0-5s window
# suffices for it to fire (it reads the crash buffer via `logcat -b crash` at
# startup and reboots once >=7 FATAL EXCEPTION records are accumulated:
# reboot,rescueparty_by_bootchecker). Bind-mounting an empty file over the
# binary makes every re-arm attempt fail at exec for the rest of the boot.
# Idempotent; clears on reboot. (rc file + binary strings verified live.)
grep -q " /system/bin/bootchecker " /proc/mounts || /system/bin/mount -o bind "$T/.empty" /system/bin/bootchecker
log "supervisor: bootchecker bind-neutralized (/system/bin/bootchecker bytes=$(wc -c < /system/bin/bootchecker))"
# Also clear any PRE-session FATAL EXCEPTION records from the crash buffer —
# bootchecker counts the whole buffer, not just records from this session.
runcon_shell logcat -b crash -c 2>/dev/null
log "supervisor: crash buffer cleared (logcat -b crash -c)"

# 2026-09-26 audit #3: start the Termux light-sensor feeder through Termux's own RUN_COMMAND service (needs
# allow-external-apps = true in Termux; also brings up Termux's foreground service, without which Android
# gives Termux:API empty sensor data). The feeder is single-instance and waits for fake_sensorproxy, so this
# is harmless if it already runs. Opt out: touch $T/no-sensor-bridge
TX_FEEDER=/data/data/com.termux/files/home/sensor-bridge-termux.sh
if [ ! -e "$T/no-sensor-bridge" ] && [ -f "$TX_FEEDER" ] && [ ! -e "$R/usr/local/etc/no-fake-sensorproxy" ]; then
	set -- --es com.termux.RUN_COMMAND_PATH "$TX_FEEDER" --ez com.termux.RUN_COMMAND_BACKGROUND true
	[ -e "$R/usr/local/etc/sensorproxy-accel" ] && set -- "$@" --esa com.termux.RUN_COMMAND_ARGUMENTS --accel
	timeout 15 runcon u:r:shell:s0 /system/bin/am startservice --user 0 -n com.termux/com.termux.app.RunCommandService \
		-a com.termux.RUN_COMMAND "$@" >/dev/null 2>&1
	log "supervisor: Termux sensor bridge requested via RUN_COMMAND (rc=$?)"
	set --
fi

# 2026-09-27 (11-review-queue-opus.md R.4): the panel refresh rate (Samsung MCD panel: 120 Hz base + hardware TE
# skip for 60/30) is programmed ONLY by Android HWC mode changes. mutter modesets never reprogram it, so the panel
# kept whatever Android last chose - normally its adaptive idle mode, 30 Hz - and every session scanned out at 30/s
# whatever GNOME asked for. Pin Android to its 120 Hz mode through SurfaceFlinger debug code 1035 (lives only in this
# SF process: the fresh SF started by fedora-restore.sh drops it), confirm on the panel, then tell GNOME the rate
# the panel really runs. Opt-out: touch $T/no-panel-120
PANEL_HZ=""
if [ ! -e "$T/no-panel-120" ]; then
	mid=$(timeout 10 runcon u:r:shell:s0 /system/bin/dumpsys SurfaceFlinger 2>/dev/null |
		sed -n 's/^ *{id=\([0-9]*\), hwcId=[0-9]*, resolution=2960x1848, vsyncRate=120\.00 Hz.*/\1/p' | head -n 1)
	if [ -n "$mid" ]; then
		timeout 5 runcon u:r:shell:s0 /system/bin/service call SurfaceFlinger 1035 i32 "$mid" >/dev/null 2>&1
		pv=0
		while [ "$pv" -lt 12 ]; do
			case "$(cat /sys/class/lcd/panel/vrr 2>/dev/null)" in "120 "*) PANEL_HZ=120; break;; esac
			sleep 0.5
			pv=$((pv + 1))
		done
	fi
	# a refresh-rate change is not a DisplayPowerController transition, but let the pipeline settle before SF stops
	iw=0
	while [ "$iw" -lt 5 ] && ! display_idle; do sleep 1; iw=$((iw + 1)); done
	[ "$iw" -ge 5 ] && log "supervisor: WARNING display not idle 5 s after the refresh-rate switch - continuing (not a DisplayPowerController transition)"
	log "supervisor: panel 120 Hz request (SF mode id '$mid', display idle re-check ${iw}s): vrr=[$(cat /sys/class/lcd/panel/vrr 2>/dev/null)] $(grep panel_mode /sys/class/lcd/panel/display_mode 2>/dev/null) after ${pv:-0} checks"
fi
[ -e "$T/no-panel-120" ] || case "$(cat /sys/class/lcd/panel/vrr 2>/dev/null)" in
	"120 "*) PANEL_HZ=120;;
	"60 "*) PANEL_HZ=60;;
	*) PANEL_HZ=60;;   # 30 Hz: GNOME 60 is the closest mode it has; panel still scans out at 30
esac
MX="$R/root/.config/monitors.xml"
if [ -n "$PANEL_HZ" ] && [ -f "$MX" ] && [ "$(grep -c "<rate>" "$MX")" = 1 ]; then   # only the single built-in-panel config
	sed -i "s|<rate>[0-9.]*</rate>|<rate>$PANEL_HZ.000</rate>|" "$MX"
	log "supervisor: GNOME monitors.xml rate set to $(sed -n 's|.*<rate>\(.*\)</rate>.*|\1|p' "$MX")"
fi

# 2026-09-27: the Termux audio bridge (PulseAudio + 127.0.0.1:4713 listener the chroot tunnels to) starts with the
# session too, same RUN_COMMAND route as the sensor feeder. Idempotent wrapper; opt-out: touch $T/no-audio-bridge
# 2026-09-27 UN-AUDITED hardening: the 22:10:53 session-start trigger never delivered (sensor trigger at :52 and
# audio trigger at :53 are 1 s apart into a possibly cold-starting Termux service — delivery race), and the Termux
# PA that DID exist was WEDGED for hours (listener on 4713 present, connections refused: two PA daemons, CLOSE_WAIT
# queues with unread handshakes). So verify the bridge actually answers from the chroot and re-trigger up to 3x;
# a wedged PA is also caught because its listener refuses connections. Live-proven 22:19-22:21: killing both PAs +
# one re-trigger restored the full chain.
TX_AUDIO=/data/data/com.termux/files/home/fedora-audio-bridge.sh
AWPID=""
if [ ! -e "$T/no-audio-bridge" ] && [ -f "$TX_AUDIO" ]; then
	audio_ok() {
		# timeout: a wedged PA can accept the TCP connect and never answer the handshake (pactl has no own
		# timeout) — without it session bring-up would hang here before SF is even stopped (audit 2026-09-27)
		timeout 6 chroot "$R" /usr/bin/env XDG_RUNTIME_DIR=/run/xdg PATH=/usr/bin:/bin \
			/usr/bin/pactl -s tcp:127.0.0.1:4713 info >/dev/null 2>&1
	}
	ab_try=0
	while [ "$ab_try" -lt 3 ] && ! audio_ok; do
		ab_try=$((ab_try + 1))
		log "supervisor: audio bridge not answering on tcp:4713 (try $ab_try) — triggering via RUN_COMMAND"
		timeout 15 runcon u:r:shell:s0 /system/bin/am startservice --user 0 -n com.termux/com.termux.app.RunCommandService \
			-a com.termux.RUN_COMMAND --es com.termux.RUN_COMMAND_PATH "$TX_AUDIO" --ez com.termux.RUN_COMMAND_BACKGROUND true \
			>/dev/null 2>&1
		sleep 8
	done
	if audio_ok; then
		log "supervisor: Termux audio bridge verified (tcp:4713 answers)"
	else
		log "supervisor: WARNING Termux audio bridge NOT answering after $ab_try try/tries — audio will be silent"
	fi
	# 2026-09-27 UN-AUDITED: Android STREAM_MUSIC speaker volume was 2/15 mid-session (the entire PulseAudio
	# chain was 100% — the attenuation was Android-side, and the rocker is grabbed by GNOME during a session,
	# so Android media volume is unreachable from GNOME). Pin it to max every session; persists in Android
	# otherwise. Pure AudioManager service call (cmd audio): no UI, no window activity, safe with SF stopped.
	av_now=$(runcon u:r:shell:s0 /system/bin/cmd audio get-stream-volume 3 2>/dev/null | awk 'END{print $NF}' | tr -dc 0-9)
	case "$av_now" in ''|*[!0-9]*) av_now=0;; esac
	if [ "$av_now" -lt 15 ]; then
		runcon u:r:shell:s0 /system/bin/cmd audio set-device-volume 3 15 2 >/dev/null 2>&1
		log "supervisor: Android media volume pinned to 15/15 (was $av_now)"
	fi
	# 2026-09-28 (doc 11 §AE): mid-session audio watchdog. Termux module-aaudio-sink can deadlock in AAudioStream_close
	# (seen live 09:51:27: daemon listens on 4713 but never answers -> silent for the rest of the session). Every 20 s
	# check it answers; after 2 misses re-trigger the bridge (its wrapper now force-kills a daemon that does not answer).
	# The chroot parec|pacat loop reconnects by itself. Killed (-9) at teardown; exits when the supervisor dies.
	(
		miss=0
		while kill -0 "$SPID" 2>/dev/null; do
			sleep 20
			if audio_ok; then miss=0; continue; fi
			miss=$((miss + 1))
			[ "$miss" -lt 2 ] && continue
			log "supervisor: audio watchdog: Termux PulseAudio not answering (2 checks) — re-triggering the bridge"
			timeout 15 runcon u:r:shell:s0 /system/bin/am startservice --user 0 -n com.termux/com.termux.app.RunCommandService \
				-a com.termux.RUN_COMMAND --es com.termux.RUN_COMMAND_PATH "$TX_AUDIO" --ez com.termux.RUN_COMMAND_BACKGROUND true \
				>/dev/null 2>&1
			miss=0
			sleep 20
		done
	) &
	AWPID=$!
	log "supervisor: audio watchdog pid $AWPID"
fi

# 2026-09-27 pogo ghost (doc 11 §Z): a uinput clone of the Book Cover Keyboard (same vendor/product/keys) keeps
# Android's pogo status + keyboard config constant, so a physical detach/attach mid-session no longer triggers the
# config-change WM transition that kills system_server with SF stopped (§W). Must exist BEFORE SF stops and go AFTER
# SF is back: runs as pogo-ghost-py (python3 symlink) so the teardown python3 sweep leaves it alone; removed after the
# zygote restart below, and removes itself if this supervisor dies. Opt-out: $T/no-pogo-ghost
PGPID=""
if [ -f "$R/usr/local/bin/pogo_ghost.py" ] && [ ! -e "$T/no-pogo-ghost" ]; then
	ln -sf python3 "$R/usr/bin/pogo-ghost-py"
	chroot "$R" /usr/bin/pogo-ghost-py /usr/local/bin/pogo_ghost.py $$ >> "$T/pogo-ghost.log" 2>&1 &
	PGPID=$!
	sleep 1.5
	log "supervisor: pogo ghost pid $PGPID ($(tail -1 "$T/pogo-ghost.log" 2>/dev/null))"
fi
# 2026-09-28 input hide (doc 11 §AC): input nodes created from now on (keyboard RE-attach, BT/USB HID) are invisible to
# system_server (tmpfs mirror over /dev/input in ITS mount namespace only), so it can never read a key/motion from them
# before input_janitor grabs them (§AB re-attach crash). After the ghost (so the mirror includes it), before SF stops.
# Off at the ghost-removal points below. Opt-out: $T/no-input-hide. Log: $T/input-hide.log
IHON=""
if [ -f "$T/input-hide.sh" ] && [ ! -e "$T/no-input-hide" ]; then
	if sh "$T/input-hide.sh" start "$SPID"; then IHON=1; ihs=ON; else ihs=FAILED; fi
	log "supervisor: input hide $ihs ($(tail -1 "$T/input-hide.log" 2>/dev/null))"
fi
# 2026-09-28 (doc 11 §AH): never stop SF while a Toast window is up. Session 10:31 died this way: the pogo ghost (created
# just above) made Samsung's input ToastDialog show a keyboard-layout toast (10:31:15.97), SF stopped 2.5 s later with it
# on screen, its timeout removal started a WM transition that can never finish with SF stopped -> BLASTSync timeout ->
# SurfaceControl DEAD_OBJECT -> system_server died (10:31:37). Wait (max 6 s) until no Toast window is left, then let
# the removal transition settle 1 s.
# audit F06: each query is time-bounded and the whole wait is wall-clock bounded; a query that fails or times out is
# "unknown", never "no toast". If absence is not confirmed, abort before SF is stopped.
toast_state() {   # 0 = toast present, 1 = confirmed absent, 2 = unknown (query failed/timed out)
	_o=$(timeout 5 runcon u:r:shell:s0 /system/bin/dumpsys window windows 2>/dev/null) || return 2
	[ -n "$_o" ] || return 2
	echo "$_o" | grep -qE "Window[{][0-9a-f]+ u0 Toast[}]" && return 0
	return 1
}
tw=0
tdl=$(( $(mono) + 12 ))
toast_state; tst=$?
while [ "$tst" != 1 ] && [ "$(mono)" -lt "$tdl" ]; do
	sleep 0.5; tw=$((tw + 1))
	toast_state; tst=$?
done
if [ "$tst" = 1 ] && [ "$tw" -gt 0 ]; then
	sleep 1
	toast_state; tst=$?
fi
if [ "$tst" != 1 ]; then
	abort_before_sf "Android window state not settled (toast state=$tst: 0=toast still up, 2=dumpsys unavailable) after ~12 s"
fi
[ "$tw" -gt 0 ] && log "supervisor: waited for Android toast(s) to clear before stopping SF ($tw checks + 1 s settle)"
phase sf-stopped 150
runcon_shell stop surfaceflinger
log "supervisor: surfaceflinger stopped (panel vrr=[$(cat /sys/class/lcd/panel/vrr 2>/dev/null)])"

# Hand the two shared Android-owned peripheral stacks to Fedora for this
# session.  NetworkManager supplies GNOME's Wi-Fi panel; iio-sensor-proxy
# supplies orientation/ambient-light data.  fedora-restore.sh starts all three
# Android services again on normal cleanup *and* watchdog recovery.
# Wi-Fi: use the PROVEN handoff (driver-build/05, 2026-09-18): `svc wifi disable`
# cleanly stops wpa_supplicant and idles wificond and leaves wlan0 admin-UP.
# Raw `stop wificond/wpa_supplicant` confuses the framework and leaves wlan0
# DOWN with ENODEV on IFF_UP (seen 2026-09-24: "Could not set interface wlan0
# UP: No such device"). Restored with `svc wifi enable` after the session.
runcon_shell svc wifi disable
touch "$T/.wifi-svc-disabled"
we=0
while [ "$we" -lt 10 ] && [ "$(getprop init.svc.wpa_supplicant)" != "stopped" ]; do sleep 1; we=$((we + 1)); done
log "supervisor: svc wifi disable done (wpa_supplicant=$(getprop init.svc.wpa_supplicant) after ${we}s)"
# 2026-09-26: the sensors HAL is NO LONGER stopped by default. With it stopped, system_server's
# SensorService blocks forever on android.hardware.sensors.ISensors/default (init refuses
# ctl.interface_start while the service is stopped), PowerManagerService's auto-brightness
# call (SystemSensorManager.setOperationParameter) hangs, the framework watchdog kills
# system_server ~90 s in, netd restarts and its RouteController flushes the pref 21000 policy
# rule -> no internet in the session (seen in the 09-24/25 dropbox logs and 2026-09-26 09:57:28).
# Fedora loses nothing: iio-sensor-proxy finds no usable sensors in the chroot anyway.
# Opt back in with:  touch /data/local/tmp/stop-sensors-hal
if [ -e "$T/stop-sensors-hal" ]; then
	for svc in vendor.sensors-hal-multihal; do
		runcon_shell stop "$svc"
		elapsed=0
		while [ "$elapsed" -lt 10 ] && [ "$(getprop init.svc.$svc)" != "stopped" ]; do
			sleep 1
			elapsed=$((elapsed + 1))
		done
		log "supervisor: Android peripheral service $svc stop state=$(getprop init.svc.$svc) after ${elapsed}s"
	done
else
	log "supervisor: sensors HAL left running (opt-in stop: touch $T/stop-sensors-hal)"
fi
/system/bin/ip link set wlan0 up 2>/dev/null
log "supervisor: Android Wi-Fi handed to Fedora"

# SfSentinel (2026-09-20 root-loss fix): with the real SF gone, its servicemanager
# names disappear, and system_server's PowerManagerService.userActivity ->
# nativeSetPowerBoost -> SurfaceComposerClient::notifyPowerBoost ->
# ComposerServiceAIDL::getComposerService() blocks FOREVER in libbinder's
# waitForService() while holding the PowerManager lock -> every service handler
# piles up -> system_server Watchdog fires (60s) -> framework restart (root
# lapse) -> RescueParty escalation -> full reboot. Both 2026-09-20 root-loss
# incidents went through exactly this chain (dropbox watchdog dumps).
# Registering a fail-fast placeholder binder under both SF names makes every
# such call return an error immediately instead of hanging.
# Run in surfaceflinger's SELinux domain (permissive masks kernel avc checks,
# but servicemanager's userspace selinux_check_access is enforced regardless;
# kernel:s0/shell:s0/surfaceflinger-less domains are all denied "add" there).
# MUST be started only AFTER SF init is 'stopped' (addService OVERWRITES
# existing registrations on this Android version), and fedora-restore.sh's
# start-surfaceflinger re-registers the real names before this is killed.
SFSENTINEL_PID=""
setsid runcon u:r:surfaceflinger:s0 "$T/sfsentinel" >/dev/null 2>&1 &
SFSENTINEL_PID=$!
echo -1000 > /proc/$SFSENTINEL_PID/oom_score_adj 2>/dev/null
log "supervisor: sfsentinel placeholder started (pid $SFSENTINEL_PID, domain surfaceflinger)"
# audit F04: the placeholder must be alive AND have registered the SurfaceFlinger name before HWC goes down. If not,
# hand Android back now through the shared restore (HWC still up, nothing else stopped yet except SF/Wi-Fi).
sw=0
while [ "$sw" -lt 6 ]; do
	sleep 1
	kill -0 "$SFSENTINEL_PID" 2>/dev/null || break
	runcon u:r:shell:s0 /system/bin/service check SurfaceFlinger 2>/dev/null | grep -q ": found" && break
	sw=$((sw + 1))
done
if ! kill -0 "$SFSENTINEL_PID" 2>/dev/null || ! runcon u:r:shell:s0 /system/bin/service check SurfaceFlinger 2>/dev/null | grep -q ": found"; then
	log "supervisor: ABORT — sfsentinel placeholder not running/registered after ${sw}s; handing Android back (HWC is still up, so SurfaceFlinger is simply started again — the shared restore cannot be used here: HWC holds DRM master)"
	kill -9 "$WPID" 2>/dev/null
	[ -n "$AWPID" ] && kill -9 "$AWPID" 2>/dev/null
	[ -n "$IHON" ] && { sh "$T/input-hide.sh" off; IHON=""; }
	sfok=0
	for sfa in 1 2 3; do
		runcon_shell start surfaceflinger
		sfw=0
		while [ "$sfw" -lt 15 ] && [ "$(getprop init.svc.surfaceflinger)" != "running" ]; do sleep 1; sfw=$((sfw + 1)); done
		[ "$(getprop init.svc.surfaceflinger)" = "running" ] && { sfok=1; break; }
		runcon_shell stop surfaceflinger
		sleep 2
	done
	if [ "$sfok" != 1 ]; then
		# keep the placeholder (fail-fast names) and the phase file out of the way; the watchdog is already gone, so say so loudly
		log "supervisor: sentinel-abort: SurfaceFlinger did NOT start after 3 attempts — running the shared restore (it stops HWC, retakes DRM master, restarts HWC + SF); placeholder kept until SF is up"
		rm -f "$PH"
		sh "$T/fedora-restore.sh"
		log "supervisor: sentinel-abort: shared restore rc=$? (sf=$(getprop init.svc.surfaceflinger)); hardware fallback POWER + VOL DOWN ~10 s"
		exit 1
	fi
	kill -9 "$SFSENTINEL_PID" 2>/dev/null
	kill_sentinel
	# SF was stopped and restarted under a live system_server: replay surfaceflinger.rc "onrestart restart zygote"
	runcon_shell stop zygote
	sleep 2
	runcon_shell start zygote
	zw=0
	while [ "$zw" -lt 60 ] && [ "$(getprop init.svc.zygote)" != "running" ]; do sleep 1; zw=$((zw + 1)); done
	sleep 5
	runcon_shell stop bootanim
	runcon u:r:shell:s0 /system/bin/cmd power set-wakelock release FULL_WAKE_LOCK >/dev/null 2>&1
	runcon u:r:shell:s0 /system/bin/cmd power suppress-ambient-display fedora-session false >/dev/null 2>&1
	st=0
	while [ -f "$T/.fedora-android-settings" ] && [ "$st" -lt 6 ]; do
		sleep 5
		sh "$T/fedora-android-settings.sh" restore && break
		st=$((st + 1))
	done
	if [ -f "$T/.wifi-svc-disabled" ]; then
		bw=0
		while [ "$bw" -lt 90 ] && [ "$(getprop sys.boot_completed)" != "1" ]; do sleep 1; bw=$((bw + 1)); done
		wt=0
		while [ "$wt" -lt 8 ]; do
			sleep 4
			timeout 20 runcon u:r:shell:s0 /system/bin/svc wifi enable
			sleep 2
			case "$(runcon u:r:shell:s0 /system/bin/settings get global wifi_on 2>/dev/null)" in 1|2) break;; esac
			wt=$((wt + 1))
		done
		rm -f "$T/.wifi-svc-disabled"
	fi
	[ -n "$PGPID" ] && { kill "$PGPID" 2>/dev/null; sleep 1; kill -9 "$PGPID" 2>/dev/null; }
	rm -f "$PH"
	log "supervisor: sentinel-abort recovery done (sf=$(getprop init.svc.surfaceflinger) zygote=$(getprop init.svc.zygote))"
	exit 1
fi
log "supervisor: sfsentinel placeholder verified (pid alive, SurfaceFlinger name registered, ${sw}s)"

runcon_shell stop vendor.hwcomposer-3-2
elapsed=0
while [ "$elapsed" -lt 30 ]; do
	[ "$(getprop init.svc.vendor.hwcomposer-3-2)" = "stopped" ] && break
	sleep 1
	elapsed=$((elapsed + 1))
done
log "supervisor: hwcomposer-3-2 stop requested (state=$(getprop init.svc.vendor.hwcomposer-3-2) after ${elapsed}s)"
# Same 8s settle as every proven script (driver-build/06) — unrelated to this
# test's actual change, kept identical on purpose.
sleep 8

( while :; do kill -0 "$SPID" 2>/dev/null || exit 0; sleep 3; touch "$HB"; done ) &
HBT=$!

chroot "$R" /bin/sh -c 'export PATH=/usr/bin:/usr/sbin; mkdir -p /run/xdg /tmp/.X11-unix; chmod 700 /run/xdg; chmod 1777 /tmp/.X11-unix'

# 2026-09-19: gbm_surface_fill_slot()'s AHardwareBuffer_allocate() (real Binder
# IAllocator call) was failing with "SELinux denied for service" from
# ServiceManagerCppClient — NOT the global enforce/permissive toggle (still
# Permissive; unaffected), but servicemanager's own isDeclared/checkService
# check, which is enforced independent of it. Root cause confirmed via
# plat_service_contexts: android.hardware.graphics.allocator.IAllocator/default
# is type hal_graphics_allocator_service; our chroot's process context
# (u:r:kernel:s0, from the existing root environment) has no grant to find it. Fix:
# run the whole chroot'd session (gnome-shell/mutter inherit this across
# exec) as u:r:untrusted_app:s0 — the SAME domain every ordinary Android app
# already runs under to call this exact NDK API, already permitted by the
# loaded policy (isolated-tested standalone via test_gbm_alloc: real
# AHardwareBuffer_allocate + gbm_surface_create/destroy PASS, 3 buffers,
# numFds=3 numInts=60 matching the documented gralloc_extra shape). No new
# sepolicy needed — this is standard, precedented access, not a policy change.
setsid runcon u:r:untrusted_app:s0 chroot "$R" /bin/bash /root/gnome-shell-session-runner.sh >> "$T/gnome-shell-session.log" 2>&1 &
CPID=$!
log "supervisor: runner launched, pgid $CPID"
phase run 0
echo -1000 > /proc/$SPID/oom_score_adj 2>/dev/null   # after the runner fork: GNOME keeps its default

(
	sleep $((MINS * 60))
	log "supervisor: time-box of ${MINS}m reached — ending session"
	kill -KILL -- -"$CPID" 2>/dev/null
	kill -KILL "$CPID" 2>/dev/null
	kill_compositors
) &
TPID=$!

# ---- 2026-09-26 audit #3: daily-use safety (see fedora-native/06-safety-fallbacks.md, 2026-09-26 IMPLEMENTED) ----
# Panic chord: hold VOL UP + VOL DOWN 3 s -> $R/tmp/.fedora-panic (panic_chord.py polls key STATE via EVIOCGKEY,
# so it works while gnome-shell is frozen and while fake_logind grabs the buttons for GNOME).
rm -f "$R/tmp/.fedora-panic"
PCPID=""
if [ -f "$R/usr/local/bin/panic_chord.py" ]; then
	chroot "$R" /usr/bin/python3 /usr/local/bin/panic_chord.py >> "$T/panic-chord.log" 2>&1 &
	PCPID=$!
	echo -1000 > /proc/$PCPID/oom_score_adj 2>/dev/null
	log "supervisor: panic-chord watcher pid $PCPID (hold VOL UP + VOL DOWN 3 s to end the session)"
fi
# 2026-09-27: Android-side request helper (Bluetooth on/off/status for the chroot; fixed whitelist). Opt-out: $T/no-android-bridge
ABPID=""
if [ -f "$T/android-bridge.sh" ] && [ ! -e "$T/no-android-bridge" ]; then
	sh "$T/android-bridge.sh" $$ >> "$T/android-bridge.log" 2>&1 &
	ABPID=$!
	log "supervisor: android bridge pid $ABPID"
fi
# 2026-09-27: USB host support (device log + Android-mounted drives shown in GNOME Files). Opt-out: $T/no-usb-watch
UWPID=""
if [ -f "$T/usb-watch.sh" ] && [ ! -e "$T/no-usb-watch" ]; then
	sh "$T/usb-watch.sh" $$ >> "$T/usb-watch.log" 2>&1 &
	UWPID=$!
	log "supervisor: usb watcher pid $UWPID"
fi
# 2026-09-27 UN-AUDITED: input janitor — EVIOCGRAB every /dev/input node Android could still read.
# Why: a single MotionEvent reaching Android mid-session ANRs the (frozen) SystemUI gesture monitors,
# and system_server showing that ANR dialog does addWindow -> SurfaceControl.nativeCreate -> DEAD_OBJECT
# (SurfaceFlinger stopped) -> framework dies -> every restart dies at boot (DisplayManagerService has no
# default display) -> 30 s crash loop -> session ends. Seen live 2026-09-27 sessions 19:56 + 21:12 (BLE HID
# mouse uhid node predating the session was never grabbed). EBUSY = mutter/fake_logind already hold the
# device — fine. The grab dies with this process, so session end / restore is unaffected. KNOWN LIMIT: BT /
# USB HID devices also cannot reach GNOME until mutter hotplug lands (doc 11 §T — untested anyway).
# Opt-out: $T/no-input-janitor
IJPID=""
if [ -f "$R/usr/local/bin/input_janitor.py" ] && [ ! -e "$T/no-input-janitor" ]; then
	chroot "$R" /usr/bin/env JANITOR_SESSION_PID="$CPID" /usr/bin/python3 /usr/local/bin/input_janitor.py >> "$T/input-janitor.log" 2>&1 &
	IJPID=$!
	echo -1000 > /proc/$IJPID/oom_score_adj 2>/dev/null
	log "supervisor: input janitor pid $IJPID (grabs input nodes Android could read)"
fi

# Session monitor: ends the session (same path as the time-box) on the panic chord, a gnome-shell main loop that
# stops answering D-Bus for ~60 s (process alive but frozen: the 2026-09-19 stall class), or battery <= 5 % while
# discharging (hand back to Android before the tablet dies with SurfaceFlinger stopped). Also re-adds the
# pref 21000 routing rule if an Android framework restart (netd RouteController) flushed it, and caps log growth.
gs_pid() {
	for p in $(timeout 2 pgrep -x gnome-shell 2>/dev/null); do
		[ "$(proc_root "$p")" = "$R" ] && { echo "$p"; return; }
	done
}
(
	n=0; miss=0; alive_seen=0; rule_seen=0
	ss_pid=$(pidof system_server 2>/dev/null); ss_restarts=""
	while kill -0 "$CPID" 2>/dev/null; do
		sleep 5
		n=$((n + 1))
		reason=""
		[ -f "$R/tmp/.fedora-panic" ] && reason="panic chord (VOL UP + VOL DOWN held)"
		if [ -z "$reason" ] && [ $((n % 3)) -eq 0 ]; then
			GS=$(gs_pid)
			if [ -n "$GS" ]; then
				ADDR=$(tr '\0' '\n' < /proc/$GS/environ 2>/dev/null | sed -n 's/^DBUS_SESSION_BUS_ADDRESS=//p')
				if [ -n "$ADDR" ] && timeout 8 nsenter -t "$GS" -m -- chroot "$R" /usr/bin/env DBUS_SESSION_BUS_ADDRESS="$ADDR" \
						/usr/bin/gdbus call --session --timeout 5 --dest org.gnome.Shell --object-path /org/gnome/Shell \
						--method org.freedesktop.DBus.Properties.Get org.gnome.Shell ShellVersion >/dev/null 2>&1; then
					[ "$alive_seen" = 0 ] && log "monitor: gnome-shell main loop answering (liveness armed)"
					alive_seen=1; miss=0
				elif [ "$alive_seen" = 1 ]; then
					miss=$((miss + 1))
					log "monitor: gnome-shell main loop did not answer ($miss/4)"
					[ "$miss" -ge 4 ] && reason="gnome-shell unresponsive for ~60 s"
				fi
			fi
		fi
		if [ -z "$reason" ] && [ $((n % 6)) -eq 0 ]; then
			cap=$(cat /sys/class/power_supply/battery/capacity 2>/dev/null)
			bst=$(cat /sys/class/power_supply/battery/status 2>/dev/null)
			case "$cap" in ''|*[!0-9]*) ;; *) [ "$bst" = Discharging ] && [ "$cap" -le 5 ] && reason="battery ${cap}% and discharging";; esac
			# 2026-09-27: Android framework crash loop (system_server restarting while SF is stopped — e.g. a
			# window transition that waits for SF, or the ANR-dialog DEAD_OBJECT path). With SF stopped NO
			# restart can survive boot (DisplayManagerService phase-100 display timeout — proven 6x live on
			# 2026-09-27), so a first restart is already unrecoverable: hand back to Android immediately
			# instead of riding out the 30 s crash cycle (this is what made sessions look like a fixed
			# 4-5 min timer). Threshold was 3.
			now_ss=$(pidof system_server 2>/dev/null)
			# audit fix 2026-09-27: an empty baseline (pidof raced a respawn at monitor start) must not make the
			# first real pid count as a restart — with threshold 1 that ended the session on a false positive
			[ -z "$ss_pid" ] && ss_pid=$now_ss
			if [ -n "$now_ss" ] && [ "$now_ss" != "$ss_pid" ]; then
				ss_pid=$now_ss
				t=$(date +%s)
				ss_restarts="$t $(for x in $ss_restarts; do [ $((t - x)) -lt 600 ] && echo $x; done)"
				cnt=$(echo $ss_restarts | wc -w)
				log "monitor: Android system_server restarted (pid $now_ss; $cnt in 10 min)"
				# the session wakelock and AOD suppression lived in the old system_server
				$PWR suppress-ambient-display fedora-session true >/dev/null 2>&1
				$PWR set-wakelock acquire FULL_WAKE_LOCK >/dev/null 2>&1
				[ "$cnt" -ge 1 ] && reason="Android framework restart (unrecoverable with SF stopped)"
			fi
			if /system/bin/ip -4 rule show 2>/dev/null | grep -q '^21000:'; then
				rule_seen=1
			elif [ "$rule_seen" = 1 ]; then
				/system/bin/ip -4 rule add pref 21000 lookup main 2>/dev/null
				/system/bin/ip -6 rule show 2>/dev/null | grep -q '^21000:' || /system/bin/ip -6 rule add pref 21000 lookup main 2>/dev/null
				log "monitor: pref 21000 routing rule was flushed (Android framework restart?) — re-added"
			fi
		fi
		if [ $((n % 60)) -eq 0 ]; then
			for f in "$R"/gnome-shell-attempt.log "$R"/gsd-*.log "$R"/fake-*.log "$R"/gnome-peripherals.log \
					"$R"/dbus-system.log "$R"/audio-bridge-loop.log "$T/gnome-shell-session.log"; do
				[ -f "$f" ] || continue
				if [ "$(wc -c < "$f" 2>/dev/null || echo 0)" -gt 104857600 ]; then
					tail -n 5000 "$f" > "$f.tail" 2>/dev/null; : > "$f"
					log "monitor: $f exceeded 100 MB — last 5000 lines kept in $f.tail, truncated"
				fi
			done
		fi
		if [ -n "$reason" ]; then
			log "monitor: ENDING SESSION — $reason"
			rm -f "$R/tmp/.fedora-panic"
			kill -KILL -- -"$CPID" 2>/dev/null
			kill -KILL "$CPID" 2>/dev/null
			kill_compositors
			exit 0
		fi
	done
) &
MONPID=$!
echo -1000 > /proc/$MONPID/oom_score_adj 2>/dev/null

wait "$CPID"
RC=$?
log "supervisor: wait returned rc=$RC — cleanup begins"
# 2026-09-25: reap anything left in the runner group (input-hotplug loop) on early exit too.
kill -KILL -- -"$CPID" 2>/dev/null
# SIGKILL: these subshells inherit the supervisor's `trap "" TERM`, so plain kill did nothing and a
# surviving time-box could kill a LATER session at the old deadline (independent audit finding).
kill -9 "$TPID" "$HBT" "$MONPID" 2>/dev/null
[ -n "$PCPID" ] && kill -9 "$PCPID" 2>/dev/null
[ -n "$ABPID" ] && { kill "$ABPID" 2>/dev/null; sleep 1; kill -9 "$ABPID" 2>/dev/null; }   # TERM first: its trap stops the camera
# 2026-09-28 (doc 11 §AC): TERM is ignored here (inherited), so the bridge trap never ran and camera servers were orphaned.
pkill -9 -f scid=6e6f7630 2>/dev/null
pkill -9 -f scid=6d696330 2>/dev/null   # 2026-09-28 (§AE): Android mic server, same reason
[ -n "$UWPID" ] && kill -9 "$UWPID" 2>/dev/null
[ -n "$AWPID" ] && kill -9 "$AWPID" 2>/dev/null   # 2026-09-28 audio watchdog (§AE)
[ -n "$IJPID" ] && kill -9 "$IJPID" 2>/dev/null   # 2026-09-27 input janitor (fd close auto-releases its grabs)
# $R/run is a shared mount: the watcher's drive binds propagate back here and would keep the USB drive busy
grep " $R/run/media/root/" /proc/mounts | awk '{print $2}' | while read -r m; do sync; umount -l "$m" 2>/dev/null && log "supervisor: drive bind $m released"; done
# audit F02: the sfsentinel placeholder is NOT killed here. It must stay up while HWC/SF are restarted; the shared
# restore dismisses it only after the real SurfaceFlinger is confirmed running (and keeps it if that fails).
log "supervisor: dismissing watchdog ($WPID)"
kill -9 "$WPID" 2>/dev/null
log "supervisor: watchdog dismissed"
rm -f "$PH"
log "supervisor: invoking shared restore (F14)"
# 2026-09-26 audit #3: an aborted restore (master held / HWC down) used to be final. Retry it; each attempt
# escalates on its own (kills chroot card0 holders, restarts HWC).
ra=0
while :; do
	sh "$T/fedora-restore.sh"
	rrc=$?
	ra=$((ra + 1))
	[ "$rrc" = 0 ] || [ "$ra" -ge 3 ] && break
	log "supervisor: restore attempt $ra failed (rc=$rrc) — retrying in 10 s"
	sleep 10
done
log "supervisor: compositor exited rc=$RC — restore returned rc=$rrc after $ra attempt(s)"
# Belt-and-suspenders: the restore's start surfaceflinger re-registers the real
# names (overwriting any lingering placeholder). If the stub somehow survived,
# remove it ONLY after SF is confirmed back so the real registration wins.
[ -n "$SFSENTINEL_PID" ] && [ "$(getprop init.svc.surfaceflinger)" = "running" ] && { kill_sentinel; }

# 2026-09-24 (sfsentinel v2): the stub now ANSWERS createDisplayEventConnection,
# so system_server/apps survive the session holding a fake vsync channel that
# will never deliver a frame from the REAL SF. Stock Android handles SF death
# with `onrestart restart zygote` in surfaceflinger.rc (which does not fire for
# our stop/start), so replay exactly that once real SF is confirmed back:
# restart zygote (framework + apps re-init against the real SF). Root survived
# 43 such framework restarts in the 2026-09-24 21:47 session, so this does not
# lapse root. Only when the stub ran and SF is really up; bootanim cleared after.
if [ -n "$SFSENTINEL_PID" ] && [ "$(getprop init.svc.surfaceflinger)" = "running" ]; then
	log "supervisor: restarting zygote so framework re-binds to the real SF (sfsentinel v2 fake channels)"
	runcon_shell stop zygote
	sleep 2
	runcon_shell start zygote
	zw=0
	while [ "$zw" -lt 60 ] && [ "$(getprop init.svc.zygote)" != "running" ]; do sleep 1; zw=$((zw + 1)); done
	sleep 5
	runcon_shell stop bootanim
	log "supervisor: zygote restart done (init.svc.zygote=$(getprop init.svc.zygote) after ${zw}s, sf=$(getprop init.svc.surfaceflinger))"
	# input hide off first (its mount died with the old system_server; this stops the watcher), then the ghost
	[ -n "$IHON" ] && { sh "$T/input-hide.sh" off; IHON=""; log "supervisor: input hide off ($(tail -1 "$T/input-hide.log" 2>/dev/null))"; }
	# pogo ghost goes only now, with SF and the framework back (a config change here is rendered normally)
	[ -n "$PGPID" ] && { kill "$PGPID" 2>/dev/null; sleep 1; kill -9 "$PGPID" 2>/dev/null; log "supervisor: pogo ghost removed"; PGPID=""; }
fi
# fedora-restore.sh already tried; the framework was restarting right after it, so retry until verified.
st=0
while [ -f "$T/.fedora-android-settings" ] && [ "$st" -lt 6 ]; do
	sleep 5
	sh "$T/fedora-android-settings.sh" restore && break
	st=$((st + 1))
done

# Give Android its Wi-Fi back (pairs with `svc wifi disable` at session start).
if [ -f "$T/.wifi-svc-disabled" ]; then
	bw=0
	while [ "$bw" -lt 90 ] && [ "$(getprop sys.boot_completed)" != "1" ]; do sleep 1; bw=$((bw + 1)); done
	# boot_completed can be 1 before system_server accepts `svc` (2026-09-24: the
	# first enable was lost, wifi_on stayed 0) — retry until the setting sticks.
	wt=0
	while [ "$wt" -lt 8 ]; do
		sleep 4
		timeout 20 runcon u:r:shell:s0 /system/bin/svc wifi enable
		sleep 2
		case "$(runcon u:r:shell:s0 /system/bin/settings get global wifi_on 2>/dev/null)" in 1|2) break;; esac
		wt=$((wt + 1))
	done
	rm -f "$T/.wifi-svc-disabled"
	log "supervisor: svc wifi enable done after ${bw}s wait + $wt retries (wifi_on=$(runcon u:r:shell:s0 /system/bin/settings get global wifi_on 2>/dev/null))"
fi
# Pogo ghost fallback (audit 2026-09-27): if the SF-running branch above was skipped (no sfsentinel, or SF not back),
# remove it here at the very end, after restore/settings/Wi-Fi — never earlier in cleanup, where SF is still stopped
# and removing it with the real keyboard detached would re-create the config change it exists to prevent.
# Input hide fallback, same reasoning; before the ghost so a keyboard re-attached mid-session is re-announced first.
# audit: never with SF down (that is the one thing the ghost exists to prevent) - if SF did not come back, leave them;
# the ghost removes itself when this supervisor exits and the input-hide watcher turns itself off when its owner dies.
if [ "$(getprop init.svc.surfaceflinger)" = "running" ]; then
	[ -n "$IHON" ] && { sh "$T/input-hide.sh" off; log "supervisor: input hide off (end of supervisor; $(tail -1 "$T/input-hide.log" 2>/dev/null))"; }
	[ -n "$PGPID" ] && { kill "$PGPID" 2>/dev/null; sleep 1; kill -9 "$PGPID" 2>/dev/null; log "supervisor: pogo ghost removed (end of supervisor)"; }
else
	log "supervisor: SurfaceFlinger not running at the end — leaving the pogo ghost / input hide to remove themselves"
fi
