#!/system/bin/sh
# usb-watch.sh (2026-09-27) — USB host support for the Fedora GNOME session. Started by the supervisor with its pid;
# exits when the supervisor is gone. Every 2 s:
#  * logs USB-C data-role changes and every USB device that appears/disappears (ADB drops while an OTG device is
#    plugged in, so the log is how the host side learns what happened): /data/local/tmp/usb-watch.log
#  * USB drives: Android's vold mounts them (/mnt/media_rw/<id> in init's mount namespace). Each such mount is
#    BIND-mounted into the session at /run/media/root/<id> (GNOME Files lists it); removed when vold drops it.
#    The block device is never mounted a second time (two RW mounts of one filesystem = corruption).
#  Serial (ttyACM/ttyUSB) and HID devices need nothing here: /dev is shared, input goes through the hotplug path.
R=/data/fedora
T=/data/local/tmp
LOG=$T/usb-watch.log
SUP=${1:-}
log() { echo "$(date +%T) $*" >> "$LOG"; }
gs_pid() { for p in $(pgrep -x gnome-shell); do [ "$(readlink /proc/$p/root)" = "$R" ] && { echo "$p"; return; }; done; }
role_old=""; devs_old=""; binds=""
echo 0 > /proc/self/oom_score_adj 2>/dev/null
log "usb-watch started (supervisor $SUP)"
while [ -z "$SUP" ] || kill -0 "$SUP" 2>/dev/null; do
	role=$(cat /sys/class/typec/port0/data_role 2>/dev/null)
	[ "$role" != "$role_old" ] && { log "USB-C data role: $role"; role_old=$role; }
	devs=""
	for d in /sys/bus/usb/devices/*; do
		[ -f "$d/idVendor" ] || continue
		devs="$devs ${d##*/}=$(cat $d/idVendor):$(cat $d/idProduct):$(cat $d/product 2>/dev/null | tr ' ' '_')"
	done
	if [ "$devs" != "$devs_old" ]; then
		log "USB devices:${devs:- none}"
		log "  nodes: $(ls /dev/ttyACM* /dev/ttyUSB* /dev/block/sd[a-z] 2>/dev/null | tr '\n' ' ')"
		devs_old=$devs
	fi
	GS=$(gs_pid)
	if [ -n "$GS" ]; then
		for m in /proc/1/root/mnt/media_rw/*; do
			id=${m##*/}
			[ "$id" = "*" ] && continue
			grep -q " /mnt/media_rw/$id " /proc/1/mountinfo || continue          # vold has not mounted it
			case " $binds " in *" $id "*) continue;; esac
			if nsenter -t "$GS" -m -- sh -c "mkdir -p '$R/run/media/root/$id' && mount --bind '$m' '$R/run/media/root/$id'"; then
				binds="$binds $id"; log "drive $id: shown in GNOME at /run/media/root/$id"
			fi
		done
		for id in $binds; do
			if ! grep -q " /mnt/media_rw/$id " /proc/1/mountinfo; then
				nsenter -t "$GS" -m -- sh -c "sync; umount -l '$R/run/media/root/$id'; rmdir '$R/run/media/root/$id'" 2>/dev/null
				binds=$(echo " $binds " | sed "s/ $id / /; s/^ *//; s/ *$//"); log "drive $id: removed"
			fi
		done
	fi
	sleep 2
done
GS=$(gs_pid)
for id in $binds; do [ -n "$GS" ] && nsenter -t "$GS" -m -- umount -l "$R/run/media/root/$id" 2>/dev/null; done
log "usb-watch exiting"
