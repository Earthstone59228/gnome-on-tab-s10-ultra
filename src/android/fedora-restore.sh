#!/system/bin/sh
# fedora-restore.sh — F14 (02-display-session-stack.md): the ONE restore path,
# called by BOTH the supervisor's normal-exit cleanup and the watchdog's fire
# path in fedora-session.sh. Never duplicate this logic inline again.
#
# Order (fixed, do not reorder):
#   1. kill -9 compositors, retry-until-gone (F12)
#   2. master-free check via chroot mastercheck, retry <=15s (F11) —
#      on timeout: ABORT, log "master held — manual reboot required",
#      do NOT start HWC or SF
#   3. start vendor.hwcomposer-3-2 (runcon)
#   4. poll init.svc.vendor.hwcomposer-3-2 == running, <=30s —
#      on timeout: ABORT, never start SF
#   5. settle 3s
#   6. start surfaceflinger (runcon), poll running <=15s
#   7. stop bootanim (runcon) — SF start always drags it in
#
# Exit 0 = fully restored (real SurfaceFlinger confirmed running). Exit 1 = aborted before touching
# HWC/SF, OR HWC/SF failed to come up (device left in whatever state it was — safe to retry or reboot).
# On a failed SF start the sfsentinel placeholder, wake lock and Android-settings snapshot are KEPT so a
# retry still has its fallback (audit F03). A failed bootanim-clear is not fatal.

R=/data/fedora
T=/data/local/tmp
LOG=$T/fedora-session.log
log() { echo "$(date) restore: $*" >>"$LOG"; }

# F12: retry-until-gone pkill loop. pkill rc: 0=matched(retry), 1=none
# left(done), 124=timeout(retry). Max 5 rounds, then give up and continue —
# the master-free check below is the real gate, not this loop.
#
# 2026-09-19 morning fix: python3/dbus-daemon added after a real live incident
# — mutter's own process list is covered (gnome-shell/mutter), but a crashed
# gnome-shell session leaves fake_logind.py (runs as "python3") and its
# dbus-daemon (system+session bus) still holding the SAME DRM fd it handed
# out via TakeDevice (SCM_RIGHTS-shared struct file, so DRM master stays
# held even after gnome-shell itself exits) — this is exactly what tripped
# the F11 "master held — manual reboot required" abort below, on a device
# that didn't actually need a reboot; it needed these two process names
# added here. Safe to add for every other session type too (labwc/wlroots
# sessions don't run python3 or a standalone dbus-daemon at this point in
# their lifecycle, so this is a pure addition, not a behavior change for
# them).
# 2026-09-25: kills are scoped to processes whose root is the Fedora chroot, so
# Android/Termux processes that share a name (python3, parec, pacat, pipewire,
# wpa_supplicant) are never touched. Returns 0 if something was killed, 1 if
# nothing matched, 124 if pgrep timed out (same contract the pkill loop used).
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
		rr=$(proc_root "$pid")
		[ "$rr" = "$R" ] || { log "chroot_kill: skipped $1 pid $pid (root='$rr')"; continue; }
		kill -9 "$pid" 2>/dev/null && hit=0
	done
	return $hit
}

# Leftovers the name list cannot know about (e.g. gvfs's wsdd, found alive under
# Android on 2026-09-25): every process that runs in the session's SELinux domain
# (the supervisor launches the runner as u:r:untrusted_app:s0) AND is rooted in the
# chroot. sshd from fedora-enter.sh runs as u:r:kernel:s0, so it survives; Termux
# apps are untrusted_app but not chrooted, so they survive too.
sweep_session_domain() {
	hit=1
	for d in /proc/[0-9]*; do
		[ "$(proc_root "${d#/proc/}")" = "$R" ] || continue
		[ "$(cat $d/attr/current 2>/dev/null)" = "u:r:untrusted_app:s0" ] || continue
		kill -9 "${d#/proc/}" 2>/dev/null && hit=0
	done
	return $hit
}

# NOTE (audit F01): this function must NOT touch the variables of the master-free loop below (it used to
# share "round" with it and reset the escalation counter on every call) — keep its counter private.
kill_compositors() {
	kc_round=0
	while [ "$kc_round" -lt 5 ]; do
		any=0
	for p in firefox ffmpeg wireplumber pipewire-pulse pipewire parec pacat NetworkManager wpa_supplicant iio-sensor-proxy upowerd seatd gnome-shell gnome-shell-bin mutter labwc foot udevd python3 dbus-daemon; do
			chroot_kill "$p"
			rc=$?
			[ "$rc" = 0 ] && any=1
			[ "$rc" = 124 ] && any=1
		done
		sweep_session_domain && any=1
		[ "$any" = 0 ] && return 0
		kc_round=$((kc_round + 1))
		sleep 1
	done
	log "kill_compositors: gave up after 5 rounds — survivors may remain, proceeding to master check anyway"
	return 1
}

# 2026-09-27: during a session the chroot holds EVIOCGRAB on Android's touchscreen, pen, keyboard, buttons and
# hall sensors; the kernel drops a grab only when the last copy of that open file closes. If any chroot process
# survived kill_compositors still holding an input fd, Android would come back with no touch/buttons. Kill
# chroot processes that still have an evdev node (char major 13 = 0xd) open. Chroot pids only.
# Chroot pids in one pass (~0.04 s vs ~15 s of per-process readlinks on ~1100 processes).
chroot_pids() {
	ls -l /proc/[0-9]*/root 2>/dev/null | grep -- "-> $R\$" | sed 's|.*/proc/\([0-9]*\)/root.*|\1|'
}
kill_input_holders() {
	hit=1
	for pid in $(chroot_pids); do
		d=/proc/$pid
		for fdl in $d/fd/*; do
			case "$(stat -L -c %t "$fdl" 2>/dev/null):$(readlink "$fdl" 2>/dev/null)" in
			d:*|*:*/input/event*)
				log "WARNING input device still held by chroot pid $pid ($(cat $d/comm 2>/dev/null)) — killing (Android input grab)"
				kill -9 "$pid" 2>/dev/null && hit=0
				break
				;;
			esac
		done
	done
	return $hit
}

log "=== restore begin === (selinux=$(getenforce 2>/dev/null))"
kill_compositors
kill_input_holders && { sleep 1; kill_input_holders; }
log "compositors clear"

# Restore Android's owners of shared radio/sensor hardware before the desktop
# comes back.  These starts are idempotent, which is important because this
# shared restore path also protects non-GNOME sessions and watchdog recovery.
for svc in vendor.sensors-hal-multihal wpa_supplicant wificond; do
	if [ "$svc" != vendor.sensors-hal-multihal ] && [ -f "$T/.wifi-svc-disabled" ]; then
		log "Wi-Fi was handed off via svc wifi disable; skipping raw start of $svc (session script runs svc wifi enable)"
		continue
	fi
	runcon u:r:shell:s0 /system/bin/start "$svc"
	elapsed=0
	while [ "$elapsed" -lt 15 ] && [ "$(getprop init.svc.$svc)" != "running" ]; do
		sleep 1
		elapsed=$((elapsed + 1))
	done
	log "Android peripheral service $svc restore state=$(getprop init.svc.$svc) after ${elapsed}s"
done

# 2026-09-26 audit #3 (daily use): a stray holder of the DRM fd used to end in "ABORT — manual reboot
# required" with the panel dark. Before giving up, find every CHROOT-rooted process that still has the
# DRM primary node (char 226:0) open and kill it. Only chroot processes are ever inspected or killed.
# Seen from here a chroot process's fd reads as /data/fedora/dev/dri/card0 (its private tmpfs node), so
# the match is on the device number (stat %t:%T = e2:0), with a path fallback. Chroot pids are selected
# first: scanning every fd of every process cost ~6 min on this device (independent audit finding).
kill_card_holders() {
	hit=1
	for pid in $(chroot_pids); do
		d=/proc/$pid
		for fdl in $d/fd/*; do
			dev=$(stat -L -c %t:%T "$fdl" 2>/dev/null)
			case "$dev:$(readlink "$fdl" 2>/dev/null)" in
			e2:0:*|*:*/dri/card0)
				log "card0 still held by chroot pid $pid ($(cat $d/comm 2>/dev/null)) — killing"
				kill -9 "$pid" 2>/dev/null && hit=0
				break
				;;
			esac
		done
	done
	return $hit
}

# F11: master-free check. Never touch HWC/SF while master is held.
# Monotonic seconds (CLOCK_BOOTTIME-ish /proc/uptime), immune to wall-clock steps.
mono() { read -r _up _ </proc/uptime; echo "${_up%%.*}"; }
master_free=0
round=0
total=0
master_deadline=$(( $(mono) + 240 ))   # hard cap independent of the round counter (audit F01)
while [ "$round" -lt 4 ] && [ "$(mono)" -lt "$master_deadline" ]; do
	elapsed=0
	while [ "$elapsed" -lt 15 ]; do
		if chroot "$R" /usr/local/bin/mastercheck >>"$LOG" 2>&1; then
			master_free=1
			break
		fi
		sleep 1
		elapsed=$((elapsed + 1))
	done
	total=$((total + elapsed))
	[ "$master_free" = 1 ] && break
	round=$((round + 1))
	[ "$round" -lt 4 ] || break
	[ "$(mono)" -lt "$master_deadline" ] || break
	log "master still held after ${total}s — escalation round $round: kill chroot card0 holders + compositors"
	kill_card_holders
	kill_compositors
done
if [ "$master_free" != 1 ]; then
	log "ABORT: master held after ${total}s and $((round - 1)) escalation round(s). HWC/SF NOT started. (The supervisor retries" \
		"this restore; hardware fallback: hold POWER + VOLUME DOWN ~10 s to force-restart the tablet.)"
	echo "restore ABORTED: DRM master held — see $LOG; force-restart = POWER + VOL DOWN ~10 s" >&2
	exit 1
fi
log "master free confirmed (${total}s)"

hwc_up=0
for attempt in 1 2 3; do
	runcon u:r:shell:s0 /system/bin/start vendor.hwcomposer-3-2
	elapsed=0
	while [ "$elapsed" -lt 30 ]; do
		state=$(getprop init.svc.vendor.hwcomposer-3-2)
		if [ "$state" = "running" ]; then
			hwc_up=1
			break
		fi
		sleep 1
		elapsed=$((elapsed + 1))
	done
	[ "$hwc_up" = 1 ] && break
	log "vendor.hwcomposer-3-2 not running after ${elapsed}s (attempt $attempt/3, state=$(getprop init.svc.vendor.hwcomposer-3-2)) — stop + retry"
	runcon u:r:shell:s0 /system/bin/stop vendor.hwcomposer-3-2
	sleep 3
done
if [ "$hwc_up" != 1 ]; then
	log "ABORT: vendor.hwcomposer-3-2 not running after 3 attempts. SF NOT started. (hardware fallback: POWER + VOL DOWN ~10 s)"
	echo "restore ABORTED: hwcomposer-3-2 did not come up — force-restart = POWER + VOL DOWN ~10 s" >&2
	exit 1
fi
log "vendor.hwcomposer-3-2 running (attempt $attempt, ${elapsed}s)"

sleep 3

runcon u:r:shell:s0 /system/bin/start surfaceflinger
elapsed=0
sf_up=0
while [ "$elapsed" -lt 15 ]; do
	state=$(getprop init.svc.surfaceflinger)
	if [ "$state" = "running" ]; then
		sf_up=1
		break
	fi
	sleep 1
	elapsed=$((elapsed + 1))
done
if [ "$sf_up" = 1 ]; then
	log "surfaceflinger running (${elapsed}s)"
else
	# audit F03: do NOT dismiss the sfsentinel placeholder, release the wake lock or restore settings, and do
	# NOT report success — the supervisor retries (each retry restarts HWC/SF), and the placeholder keeps
	# system_server from wedging on the missing SurfaceFlinger names meanwhile.
	log "ABORT: surfaceflinger not running after ${elapsed}s (state=$(getprop init.svc.surfaceflinger)) — sfsentinel placeholder, wake lock and settings snapshot KEPT; rc=1 so the caller retries"
	echo "restore INCOMPLETE: surfaceflinger did not start — see $LOG; force-restart = POWER + VOL DOWN ~10 s" >&2
	exit 1
fi

# SfSentinel handoff (2026-09-20 root-loss fix): while SF was stopped, the
# session supervisor's sfsentinel placeholder owned the "SurfaceFlinger" /
# "SurfaceFlingerAIDL" servicemanager names, so system_server's
# getComposerService() failed fast instead of wedging the PowerManager lock
# (the watchdog->framework-restart->RescueParty->root-loss chain). The real
# SF's addService OVERWRITES a placeholder registration, so the correct order
# is: confirm real SF is up FIRST, then kill the placeholder. Killing it
# earlier would reopen the wedge window during HWC/SF startup.
timeout 2 pkill -9 -x sfsentinel 2>/dev/null
log "sfsentinel placeholder dismissed (real SF owns its names again)"

# 2026-09-25: drop the policy rule the Fedora session adds (gnome-peripherals.sh) so Android
# routing is exactly as before; bounded loop, "ip rule del" fails once none are left.
for _f in -4 -6; do
	_i=0
	while [ "$_i" -lt 5 ] && /system/bin/ip $_f rule del pref 21000 2>/dev/null; do _i=$((_i + 1)); done
done
log "session policy rule (pref 21000) removed"

runcon u:r:shell:s0 /system/bin/stop bootanim

# 2026-09-26 audit #3: Android settings the session changed (auto-brightness, stay-on, screen timeout).
# Idempotent; the supervisor retries after its zygote restart if the framework was busy here.
rm -f "$R/tmp/.fedora-panic"
[ -f "$T/fedora-android-settings.sh" ] && sh "$T/fedora-android-settings.sh" restore
# 2026-09-27: release the session's screen wakelock and AOD suppression (idempotent; also leftover-safe)
runcon u:r:shell:s0 /system/bin/cmd power set-wakelock release FULL_WAKE_LOCK >/dev/null 2>&1
runcon u:r:shell:s0 /system/bin/cmd power suppress-ambient-display fedora-session false >/dev/null 2>&1
log "session wakelock released, AOD suppression lifted"
log "=== restore complete (bootanim cleared) ==="
exit 0
