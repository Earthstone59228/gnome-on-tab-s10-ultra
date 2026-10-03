#!/system/bin/sh
# android-bridge.sh (2026-09-27) — Android-side root helper for the Fedora GNOME session. Started by the supervisor,
# killed with the session. Serves a FIXED whitelist of requests the chroot drops into $Q (root-only dir on /data):
#   bt-on | bt-off | bt-status | bt-list (Android keeps the Bluetooth stack; never BlueZ — kernel-panic hazard)
#   cam-front | cam-back | cam-0..cam-3 | cam-stop | cam-list | mic-start | mic-stop
#                                  (Android camera via the open-source scrcpy server v4.1, sha256-verified jar;
#                                   H.264 on abstract socket scrcpy_6e6f7630, read by the chroot's android-camera)
# Request: $Q/<id>.req containing one word. Reply: $Q/<id>.rsp. Anything else is answered "unknown request".
R=/data/fedora
SUP=${1:-}   # supervisor pid: exit when it is gone (a crashed supervisor must not leave this running)
Q=$R/var/lib/android-bridge
echo 0 > /proc/self/oom_score_adj 2>/dev/null   # do not inherit the supervisor's -1000
[ -L "$Q" ] && rm -f "$Q"
mkdir -p "$Q" && chmod 700 "$Q" && rm -f "$Q"/*.req "$Q"/*.rsp "$Q"/*.tmp
sh_cmd() { timeout 20 runcon u:r:shell:s0 "$@" </dev/null 2>&1; }
T=/data/local/tmp
JAR=$T/scrcpy-server-v4.1.jar
JAR_MASTER=$T/scrcpy/scrcpy-server-v4.1.jar   # the server deletes the jar it ran from unless cleanup=false
trap 'cam_stop; mic_stop' EXIT INT TERM HUP
ensure_jar() { [ -f "$JAR" ] || cp "$JAR_MASTER" "$JAR" 2>/dev/null; }
CAMPID=$T/.android-camera.pid
# app_process needs init's mount namespace (the root tool's own namespace cannot resolve /apex -> CANNOT LINK)
cam_stop() {
	# SIGKILL (doc 11 §AC): this bridge inherits the supervisor's ignored SIGTERM, and an ignored signal survives exec,
	# so a plain kill never reached app_process. A server orphaned that way held the socket name for 8 h -> every later
	# start failed "Address already in use" and the reader got the stale server. The sweep catches earlier orphans.
	[ -f "$CAMPID" ] && kill -9 "$(cat "$CAMPID")" 2>/dev/null
	rm -f "$CAMPID"
	pkill -9 -f scid=6e6f7630 2>/dev/null
}
cam_start() {   # $1 = camera_facing=front|back or camera_id=N
	cam_stop
	ensure_jar; [ -f "$JAR" ] || { echo "camera server jar missing"; return; }
	setsid sh -c 'exec "$@"' sh nsenter -t 1 -m -- runcon u:r:shell:s0 /system/bin/env CLASSPATH="$JAR" \
		/system/bin/app_process / com.genymobile.scrcpy.Server 4.1 scid=6e6f7630 tunnel_forward=true video_source=camera \
		"$1" camera_size=1280x720 max_fps=30 video_codec_options=profile:int=1 audio=false control=false raw_stream=true cleanup=false log_level=info \
		>> $T/android-camera.log 2>&1 < /dev/null &
	echo $! > "$CAMPID"
	echo ok
}
# 2026-09-28 (doc 11 §AE): Android microphone for GNOME (android_mic_node.py). Same scrcpy server as the camera, as the
# shell uid (Termux cannot record in the background: its RECORD_AUDIO is while-in-use). Raw PCM s16le 48 kHz stereo on
# abstract socket scrcpy_6d696330. Separate scid/pid file: camera and mic run independently.
MICPID=$T/.android-mic.pid
mic_stop() {
	[ -f "$MICPID" ] && kill -9 "$(cat "$MICPID")" 2>/dev/null
	rm -f "$MICPID"
	pkill -9 -f scid=6d696330 2>/dev/null
}
mic_start() {
	mic_stop
	ensure_jar; [ -f "$JAR" ] || { echo "mic server jar missing"; return; }
	setsid sh -c 'exec "$@"' sh nsenter -t 1 -m -- runcon u:r:shell:s0 /system/bin/env CLASSPATH="$JAR" \
		/system/bin/app_process / com.genymobile.scrcpy.Server 4.1 scid=6d696330 tunnel_forward=true video=false audio=true \
		audio_source=mic audio_codec=raw control=false raw_stream=true cleanup=false log_level=info \
		>> $T/android-mic.log 2>&1 < /dev/null &
	echo $! > "$MICPID"
	echo ok
}
bt_status() {
	case "$(sh_cmd /system/bin/settings get global bluetooth_on)" in 1) echo "state on";; *) echo "state off";; esac
	sh_cmd /system/bin/dumpsys bluetooth_manager | sed -n '/Bonded devices:/,/^  [A-Z]/p' | grep '=>' | while read -r line; do
		name=$(echo "$line" | sed 's/.*\] //')
		case "$line" in *"BR/EDR:Y"*|*"LE:Y"*) c=1;; *) c=0;; esac
		[ -n "$name" ] && echo "device $c $name"
	done
}
# 2026-09-27 bt-list: machine-readable adapter + bonded-device state for fake_bluetooth.py (GNOME Bluetooth
# integration). Read-only (dumpsys). Samsung masks the first 4 MAC bytes (XX:XX:XX:XX:ab:cd) in dumpsys.
# Lines: "state ON|OFF|TURNING_ON|TURNING_OFF", "adapter <addr>", "adaptername <name>",
#        "dev <addr> <connected 0|1> <class-of-device hex> <name>"
bt_list() {
	sh_cmd /system/bin/dumpsys bluetooth_manager | awk '
/^  state: / && !s { print "state " $2; s = 1 }
/^  address: / && !a { print "adapter " $2; a = 1 }
/^  name: / && !n { v = $0; sub(/^  name: /, "", v); print "adaptername " v; n = 1 }
/^  Bonded devices:/ { b = 1; next }
b && /^  [A-Za-z]/ { b = 0 }
b && /^    [0-9A-FX][0-9A-FX]:[0-9A-FX][0-9A-FX]:/ {
	addr = substr($1, 1, 17)
	c = ($0 ~ /BR\/EDR:Y/ || $0 ~ /LE:Y\]/) ? 1 : 0
	cod = "0x0"
	if (match($0, /\] \[0x[0-9A-Fa-f]+\] \[ACL/)) cod = substr($0, RSTART + 3, RLENGTH - 9)
	nm = $0; sub(/.*\] /, "", nm)
	print "dev " addr " " c " " cod " " nm
}'
}
while [ -z "$SUP" ] || kill -0 "$SUP" 2>/dev/null; do
	for f in "$Q"/*.req; do
		[ -e "$f" ] || continue
		req=$(head -c 32 "$f" | tr -dc 'a-z0-9-')
		id=${f##*/}; id=${id%.req}
		rm -f "$f"
		case "$id" in *[!0-9a-f]*|"") continue;; esac
		case "$req" in
			bt-on)     out=$(sh_cmd /system/bin/cmd bluetooth_manager enable; echo "ok");;
			bt-off)    out=$(sh_cmd /system/bin/cmd bluetooth_manager disable; echo "ok");;
			bt-status) out=$(bt_status);;
			bt-list)   out=$(bt_list);;
			cam-front) out=$(cam_start camera_facing=front);;
			cam-back)  out=$(cam_start camera_facing=back);;
			cam-0|cam-1|cam-2|cam-3) out=$(cam_start camera_id=${req#cam-});;
			cam-stop)  cam_stop; out="ok";;
			mic-start) out=$(mic_start);;
			mic-stop)  mic_stop; out="ok";;
			cam-list)  ensure_jar; out=$(timeout 20 nsenter -t 1 -m -- runcon u:r:shell:s0 /system/bin/env CLASSPATH="$JAR" /system/bin/app_process / \
			                   com.genymobile.scrcpy.Server 4.1 list_cameras=true cleanup=false log_level=info </dev/null 2>&1 | grep -- '--camera-id=');;
			*)         out="unknown request";;
		esac
		rm -f "$Q/$id.tmp"; printf '%s\n' "$out" > "$Q/$id.tmp" && mv -f "$Q/$id.tmp" "$Q/$id.rsp"
	done
	sleep 0.5
done
cam_stop   # supervisor gone: never leave the camera running
mic_stop   # ... nor the microphone
