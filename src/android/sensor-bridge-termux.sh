#!/data/data/com.termux/files/usr/bin/bash
# sensor-bridge-termux.sh — runs INSIDE Termux. Streams Android's ambient-light sensor (and, with --accel, the
# accelerometer) via Termux:API to the chroot's fake_sensorproxy.py on loopback 127.0.0.1:47820.
# Default is LIGHT ONLY (accelerometer = auto-rotate is off by default; see fake_sensorproxy.py).
#
# v2 (2026-09-26, audit #3). Started automatically by fedora-session-gnome-shell.sh through Termux's RUN_COMMAND
# service (needs `allow-external-apps = true` in ~/.termux/termux.properties), which also brings up Termux's
# foreground service — without it Android hands background apps empty sensor data ({}). Still fine to run by hand.
# Fixes vs v1: (1) single instance (flock) — v1 piled up one copy per manual start; (2) `termux-sensor -c` can hang
# forever when Termux:API has nothing registered, which left v1 stuck after every session (no light data the next
# time, plus a leaked process each time) — now bounded by `timeout`; (3) no `kill 0` (it killed the caller's whole
# process group); (4) exits after IDLE_EXIT_S without a GNOME session instead of probing forever; (5) a silent
# termux-sensor stream can no longer hang the loop.
PORT=${SENSORPROXY_PORT:-47820}
IDLE_EXIT_S=${SENSOR_BRIDGE_IDLE_EXIT_S:-1800}
SENSORS="STK31610 Light"
[ "${1:-}" = "--accel" ] && SENSORS="LSM6DSVTR Accelerometer,STK31610 Light"

exec 9>"${PREFIX:-/data/data/com.termux/files/usr}/tmp/sensor-bridge.lock"
flock -n 9 || { echo "sensor-bridge: already running"; exit 0; }

release() { timeout 5 termux-sensor -c >/dev/null 2>&1; }
trap 'release; exit 0' INT TERM HUP

idle=0
while :; do
	# No listener yet (GNOME session not running)? Probe cheaply; do NOT register sensors for nothing.
	until socat -u /dev/null "TCP:127.0.0.1:$PORT,connect-timeout=1" 2>/dev/null; do
		sleep 10
		idle=$((idle + 10))
		if [ "$idle" -ge "$IDLE_EXIT_S" ]; then
			echo "sensor-bridge: no session for ${IDLE_EXIT_S}s, exiting"
			exit 0
		fi
	done
	idle=0
	echo "sensor-bridge: streaming '$SENSORS' to 127.0.0.1:$PORT"
	# socat runs in the FOREGROUND and ends when the session closes the socket: 'STDIN!!OPEN:/dev/null' makes it
	# read the (empty) return direction, so it notices the close within -t 2 s even while the sensor is silent
	# (with -u it only noticed on its next write, i.e. never for a silent stream). termux-sensor is a separate
	# background process (process substitution) killed by pid afterwards — `wait` on a background pipeline
	# waits for EVERY member, so the v2 draft still hung (independent audit + host simulation, 2026-09-27).
	exec 3< <(exec termux-sensor -s "$SENSORS" -d 500)
	sensor_pid=$!
	socat -t 2 'STDIN!!OPEN:/dev/null' "TCP:127.0.0.1:$PORT,connect-timeout=3,keepalive" <&3
	exec 3<&-
	pkill -P "$sensor_pid" 2>/dev/null   # its termux-api child first (reparented once the parent dies)
	kill "$sensor_pid" 2>/dev/null
	release
	sleep 5
done
