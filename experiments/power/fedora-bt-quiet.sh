#!/system/bin/sh
# UN-AUDITED (2026-10-07). Keep the MediaTek BT chip/driver completely idle while the tablet sleeps.
# Why: the 12:25 wedge was a deadlock in the BT UART driver's PM notifier (btmtk_pm_notification ->
# btmtk_intcmd_system_status -> btmtk_uart_driver_own) hit on one of ~3,600 suspend attempts. With the chip powered down the
# driver has nothing to talk to.  NOTE: Android's "Bluetooth scanning always on" (ble_scan_always_enabled=1) keeps the chip
# powered even when Bluetooth is "off", so that setting is turned off for the sleep too.
# usage: fedora-bt-quiet.sh off | restore | status
#   off     snapshot bluetooth_on + ble_scan_always_enabled into $ST (never overwritten while it exists), then BT off + BLE scan off
#           and wait for STATE_OFF.   rc 0 = chip quiet, 1 = could not snapshot (nothing changed), 2 = did not reach OFF (snapshot kept)
#   restore put the snapshot back exactly (ble scan setting first, then Bluetooth on if it was on) and delete the snapshot.
T=${BTQ_DIR:-/data/local/tmp}
ST=$T/sleepd.bt-state
L=${BTQ_LOG:-$T/suspend-test.log}
log() { echo "$(date +%T) BT: $*" >> "$L"; }
if [ "$BTQ_STUB" = 1 ]; then   # host/unit-test mode: never touch Android, record calls, canned settings from $T/stub-*
	sh_cmd() { echo "STUB $*" >> "$T/stub-calls"; case "$*" in *"settings get global bluetooth_on"*) cat "$T/stub-bt";; *"settings get global ble_scan_always_enabled"*) cat "$T/stub-ble";; esac; sleep "${STUB_DELAY:-0}"; return 0; }
else
	sh_cmd() { timeout 25 runcon u:r:shell:s0 "$@" </dev/null 2>&1; }
fi
getset() { sh_cmd /system/bin/settings get global "$1" | tr -d '\r\n'; }

# serialize every `off` / `restore` (audit F1/F2: a late background restore must never land after the next `off`)
LK=$T/sleepd.bt-lock
lock() {
	n=0
	while ! mkdir "$LK" 2>/dev/null; do
		op=$(cat "$LK/pid" 2>/dev/null)
		if [ -n "$op" ] && ! kill -0 "$op" 2>/dev/null; then rm -rf "$LK"; continue; fi    # holder died
		[ -z "$op" ] && [ "$n" -gt 30 ] && { rm -rf "$LK"; continue; }                  # lock dir without a pid (crash between mkdir and echo)
		n=$((n + 1)); [ "$n" -gt 120 ] && return 1
		sleep 1
	done
	echo $$ > "$LK/pid"
	trap 'rm -rf "$LK"' EXIT
	return 0
}

case "$1" in
off)
	lock || { log "cannot take the bt lock (held >120 s), nothing changed"; exit 1; }
	if [ ! -f "$ST" ]; then
		bt=$(getset bluetooth_on); ble=$(getset ble_scan_always_enabled)
		case "$bt" in 0|1) ;; *) log "cannot read bluetooth_on ('$bt'); nothing changed"; exit 1;; esac
		case "$ble" in 0|1|null) ;; *) log "cannot read ble_scan_always_enabled ('$ble'); nothing changed"; exit 1;; esac
		echo "bt=$bt ble=$ble" > "$ST.tmp" && mv "$ST.tmp" "$ST" || { log "cannot write snapshot"; exit 1; }
		log "snapshot $(cat $ST)"
	else
		log "snapshot already present ($(cat $ST)); keeping it"
	fi
	read -r l1 < "$ST"
	case "$l1" in *"ble=1"*) sh_cmd /system/bin/settings put global ble_scan_always_enabled 0 >> "$L";; esac
	sh_cmd /system/bin/cmd bluetooth_manager disable >> "$L"
	if sh_cmd /system/bin/cmd bluetooth_manager wait-for-state:STATE_OFF >> "$L"; then
		log "bluetooth OFF (chip quiet)"
		exit 0
	fi
	log "bluetooth did NOT reach STATE_OFF (snapshot kept; run restore)"
	exit 2
	;;
restore)
	lock || { log "cannot take the bt lock for restore"; exit 1; }
	# a crash between the claim below and the end of restore leaves $ST.restoring behind: take it back after 2 min
	if [ ! -f "$ST" ] && [ -f "$ST.restoring" ]; then
		age=$(( $(date +%s) - $(date -r "$ST.restoring" +%s 2>/dev/null || echo 0) ))
		[ "$age" -gt 120 ] && mv "$ST.restoring" "$ST"
	fi
	[ -f "$ST" ] || exit 0
	mv "$ST" "$ST.restoring" 2>/dev/null || exit 0     # claim (mtime refreshed so the 2-min stale check measures the claim, not the snapshot)
	touch "$ST.restoring"
	read -r l1 < "$ST.restoring"
	log "restoring $l1"
	case "$l1" in
		*"ble=1"*) sh_cmd /system/bin/settings put global ble_scan_always_enabled 1 >> "$L";;
		*"ble=null"*) sh_cmd /system/bin/settings delete global ble_scan_always_enabled >> "$L";;
	esac
	case "$l1" in *"bt=1"*) sh_cmd /system/bin/cmd bluetooth_manager enable >> "$L";; esac
	rm -f "$ST.restoring"
	log "restored"
	exit 0
	;;
status)
	echo "snapshot: $(cat $ST 2>/dev/null || echo none)"
	echo "bluetooth_on=$(getset bluetooth_on) ble_scan_always_enabled=$(getset ble_scan_always_enabled)"
	;;
*) echo "usage: $0 off|restore|status"; exit 64;;
esac
