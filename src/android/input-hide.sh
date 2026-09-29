#!/system/bin/sh
# input-hide.sh — hide input nodes CREATED during a Fedora session from system_server (doc 11 §AC, 2026-09-28).
#
# WHY (§AB): re-attaching the Book Cover Keyboard mid-session makes the pogo driver register a NEW input device and
# emit a key at once (pogo_kpd_event_bypass). Android's EventHub gets the inotify IN_CREATE, opens the new
# /dev/input/eventN and reads that key before input_janitor can EVIOCGRAB it -> interceptKeyBeforeQueueing (Samsung
# keycodes 1075/1076/1100/1101) -> Toast -> addWindow -> SurfaceControl DEAD_OBJECT (SF stopped) -> system_server dies.
# The same race exists for any BT/USB HID node that appears mid-session (§X). It cannot be won from userspace.
#
# WHAT: system_server has its OWN mount namespace whose /dev is a pure SLAVE of init's (mountinfo `master:2`, no
# `shared:`), so a mount made there propagates nowhere. We overmount /dev/input in that namespace only with a tmpfs
# MIRROR of the nodes that exist at session start (same major/minor/owner/mode, input_device label). EventHub's inotify
# watch sits on the real directory inode, so it still gets IN_CREATE/IN_DELETE, but its open("/dev/input/eventN")
# resolves through the mirror: a node created after `start` is simply not there (ENOENT) -> Android never sees it.
# Devices Android already had keep working (open fds), and a rescan/reopen (EventHub::scanDirLocked, the "Reopening
# input device" path) finds exactly the old set, pogo ghost included. ueventd, the janitor, mutter and Fedora are in
# other namespaces and see the real /dev/input — unaffected.
# Minor reuse: when a mirrored node is deleted for real, the watcher deletes its mirror entry, else a device that
# later reuses that minor would be reachable through the stale entry.
#
# LIFETIME: `start OWNER` (supervisor pid) after the pogo ghost exists, before SF stops. `off` at the same points the
# ghost is removed. The watcher runs `off` itself if OWNER dies. The mount dies with that system_server anyway (the
# supervisor's zygote restart); `off` only matters when it is still alive: then it unmounts and re-announces hidden
# nodes (rm + mknod, same attrs) so EventHub picks them up — only when SF is running.
# Opt-out: /data/local/tmp/no-input-hide (checked by the supervisor). Log: /data/local/tmp/input-hide.log
# Never mount anything at /dev/input in any namespace other than system_server's: root-tool/init /dev is `shared:2`.

T=/data/local/tmp
ST=$T/.input-hide
LOG=$T/input-hide.log
log() { echo "$(date +%H:%M:%S) [input-hide] $*" >> "$LOG"; }
is_ss() { [ -n "$1" ] && [ "$(cat /proc/$1/comm 2>/dev/null)" = system_server ]; }
hidden_mounted() { grep -q " /dev/input " /proc/$1/mountinfo 2>/dev/null; }

case "$1" in
start)
	OWNER=$2
	[ -f "$ST" ] && { log "stale state (ss $(cat "$ST")) — running off first"; sh "$0" off; }
	SS=$(pidof system_server)
	case "$SS" in ""|*" "*) log "system_server pid not unique ('$SS') — NOT hiding"; exit 1;; esac
	# Only a pure slave /dev is safe to mount under (no propagation back to init/other namespaces).
	if ! grep " /dev " /proc/$SS/mountinfo | grep -q "master:"  || grep " /dev " /proc/$SS/mountinfo | grep -q "shared:"; then
		log "system_server /dev is not a pure slave — NOT hiding: $(grep ' /dev ' /proc/$SS/mountinfo)"; exit 1
	fi
	if hidden_mounted "$SS"; then log "system_server $SS already has a /dev/input mount — NOT hiding"; exit 1; fi
	cmd="fail=0; mount -t tmpfs -o mode=0755,uid=0,gid=0,context=u:object_r:input_device:s0 fedora-input-hide /dev/input || exit 2"
	n=0
	for f in /dev/input/*; do
		[ -c "$f" ] || continue
		b=${f##*/}
		set -- $(stat -c '%t %T %a %u %g' "$f")
		cmd="$cmd; mknod /dev/input/$b c $((0x$1)) $((0x$2)) && chown $4:$5 /dev/input/$b && chmod $3 /dev/input/$b || fail=1"
		n=$((n + 1))
	done
	cmd="$cmd; exit \$fail"
	nsenter -t "$SS" -m -- /system/bin/sh -c "$cmd"
	rc=$?
	is_ss "$SS" || { log "system_server $SS vanished during setup — NOT hiding"; exit 1; }
	# Belt-and-braces: the mount must be visible ONLY in system_server's namespace.
	if hidden_mounted 1 || hidden_mounted $$; then
		log "FATAL: /dev/input mount leaked into init/root namespace — removing it"
		nsenter -t 1 -m -- umount -l /dev/input
		hidden_mounted "$SS" && nsenter -t "$SS" -m -- umount -l /dev/input
		exit 1
	fi
	m=$(ls /proc/$SS/root/dev/input 2>/dev/null | wc -l)
	if [ "$rc" != 0 ] || [ "$m" != "$n" ]; then
		log "mirror failed (rc=$rc, $m/$n nodes) — undoing, NOT hiding"
		hidden_mounted "$SS" && nsenter -t "$SS" -m -- umount -l /dev/input
		exit 1
	fi
	echo "$SS" > "$ST"
	sh "$0" watch "$OWNER" "$SS" </dev/null >/dev/null 2>&1 &
	echo $! > "$ST.wpid"
	log "ON: system_server $SS sees a frozen mirror of $n node(s); new input nodes are hidden (owner $OWNER, watcher $!)"
	;;
watch)
	OWNER=$2; SS=$3
	inotifyd - /dev/input:dn 2>>"$LOG" | while read -r ev dir name; do
		case "$ev" in
		*d*)
			if [ -e "/proc/$SS/root/dev/input/$name" ]; then
				rm -f "/proc/$SS/root/dev/input/$name" && log "real $name removed -> mirror entry removed"
			else
				log "real $name removed (was hidden)"
			fi;;
		*n*) log "new node $name ($(cat /sys/class/input/$name/device/name 2>/dev/null)) — hidden from Android";;
		esac
	done &
	i=0   # record inotifyd's pid for `off` (toybox inotifyd rewrites its argv, so pkill -f cannot find it)
	while [ "$i" -lt 20 ]; do
		ip=$(pgrep -P $$ -x inotifyd)
		[ -n "$ip" ] && { echo "$ip" > "$ST.ipid"; break; }
		sleep 0.1; i=$((i + 1))
	done
	while [ -f "$ST" ] && kill -0 "$OWNER" 2>/dev/null; do sleep 2; done
	[ -f "$ST" ] && { log "owner $OWNER gone — turning off"; sh "$0" off; }
	;;
off)
	SS=$(cat "$ST" 2>/dev/null)
	W=$(cat "$ST.wpid" 2>/dev/null)
	I=$(cat "$ST.ipid" 2>/dev/null)
	rm -f "$ST" "$ST.wpid" "$ST.ipid"
	[ -n "$W" ] && kill -9 "$W" 2>/dev/null
	[ -n "$I" ] && [ "$(cat /proc/$I/comm 2>/dev/null)" = inotifyd ] && kill -9 "$I"
	# fallback: any inotifyd watching /dev/input. Its argv is rewritten to "inotifyd - /dev/input dn" (":dn" gone, which
	# is why pkill -f "...:dn" missed it), but "/dev/input" survives in /proc/PID/cmdline. Its reader loop exits on EOF.
	for p in $(pgrep -x inotifyd); do
		grep -q /dev/input /proc/$p/cmdline 2>/dev/null && kill -9 "$p"
	done
	if ! is_ss "$SS" || ! hidden_mounted "$SS"; then
		log "OFF: system_server ${SS:-?} gone or unmounted — nothing to undo"
		exit 0
	fi
	hid=""
	for f in /dev/input/*; do
		[ -c "$f" ] || continue
		[ -e "/proc/$SS/root/dev/input/${f##*/}" ] || hid="$hid ${f##*/}"
	done
	nsenter -t "$SS" -m -- umount -l /dev/input
	log "OFF: mirror unmounted in system_server $SS (hidden during session:${hid:- none})"
	[ -z "$hid" ] && exit 0
	if [ "$(getprop init.svc.surfaceflinger)" != running ]; then
		log "WARNING: SF not running — NOT re-announcing$hid: Android will NOT see these input devices (e.g. a re-attached keyboard) until the next framework restart or reboot"
		exit 0
	fi
	for b in $hid; do
		f=/dev/input/$b
		set -- $(stat -c '%t %T %a %u %g %C' "$f" 2>/dev/null)
		[ $# = 6 ] || continue
		# Build the node beside /dev/input (same tmpfs) with final owner/mode/label, then hard-link it in: the link is
		# the IN_CREATE EventHub acts on. mknod in place was opened before chown (EACCES, tested 2026-09-28).
		tmp=/dev/.fedora-ih-$b
		rm -f "$tmp"
		if mknod "$tmp" c $((0x$1)) $((0x$2)) && chown $4:$5 "$tmp" && chmod $3 "$tmp" && chcon "$6" "$tmp" \
			&& rm -f "$f" && ln "$tmp" "$f"; then
			log "re-announced $b to Android"
		else
			log "re-announce $b FAILED"
		fi
		rm -f "$tmp"
	done
	;;
status)
	SS=$(cat "$ST" 2>/dev/null)
	echo "state ss=${SS:-none} watcher=$(cat "$ST.wpid" 2>/dev/null) mounted=$(hidden_mounted "$SS" && echo yes || echo no)"
	[ -n "$SS" ] && ls /proc/$SS/root/dev/input
	;;
*) echo "usage: $0 start OWNER_PID | off | status"; exit 2;;
esac
