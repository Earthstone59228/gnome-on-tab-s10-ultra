#!/system/bin/sh
# fedora-android-settings.sh save|restore — Android settings the Fedora GNOME session changes (2026-09-26, audit #3).
#
# save (supervisor, BEFORE `svc power stayon true` and before SurfaceFlinger stops):
#   snapshot  system screen_brightness_mode, global stay_on_while_plugged_in, system screen_off_timeout,
#             system accelerometer_rotation
#   then set  screen_brightness_mode=0   -> Android's auto-brightness stops writing the panel backlight that GNOME
#                                           now owns (two controllers on /sys/class/backlight/panel = P.1)
#             screen_off_timeout=max     -> Android never starts its idle screen-off while unplugged (stayon only
#                                           covers "plugged in"); screen-off = ColorFade exceptions + backlight off
#             accelerometer_rotation=0  -> 2026-09-27 (§V): Android's own auto-rotate is untouched by anything else
#                                           in this project and stays live all session; physically rotating the
#                                           tablet made Android's WindowManager attempt a real display reconfig
#                                           against the stopped SurfaceFlinger -> DEAD_OBJECT crash loop, same class
#                                           as a leaked touch. Disabling it for the session's duration removes that
#                                           trigger; GNOME's own auto-rotate (sensorproxy-accel) is unrelated and
#                                           unaffected.
#             hide_error_dialogs=1       -> 2026-10-03: plugging USB-C in mid-session makes Samsung's MtpReceiver start
#                                           MtpService, whose Toast.show() fails (SurfaceFlinger stopped) and crashes it
#                                           twice in ~1 s; the repeated crash makes system_server show an "app has
#                                           stopped" dialog -> addWindow -> SurfaceControl DEAD_OBJECT -> system_server
#                                           dies and the session ends. With the dialog hidden, the same repeated MTP
#                                           crashes are survived. Unset on the stock device (null), so restore deletes it.
# restore (fedora-restore.sh after SF is back, and again by the supervisor after the zygote restart): put every value
#   back (unset -> delete), verify it, and remove the snapshot only when all three verified. Idempotent.
# A snapshot left behind by a session that never restored is kept and NOT overwritten (it holds the real originals).
T=/data/local/tmp
F=$T/.fedora-android-settings
LOG=$T/fedora-session-gnome-shell.log
log() { echo "$(date) settings: $*" >> "$LOG"; }
s() { timeout 15 runcon u:r:shell:s0 /system/bin/settings "$@" </dev/null 2>/dev/null; }
valid() { case "$1" in null) return 0;; ''|*[!0-9-]*) return 1;; *) return 0;; esac; }

case "$1" in
save)
	if [ -f "$F" ]; then
		# A snapshot written before hide_error_dialogs existed lacks it; the stock value is unset (null).
		grep -q ' hide_error_dialogs ' "$F" || printf 'global hide_error_dialogs null\n' >> "$F"
		log "snapshot from an unrestored earlier session kept: $(tr '\n' ' ' < "$F")"
	else
		bm=$(s get system screen_brightness_mode)
		so=$(s get global stay_on_while_plugged_in)
		to=$(s get system screen_off_timeout)
		ar=$(s get system accelerometer_rotation)
		hd=$(s get global hide_error_dialogs)
		if valid "$bm" && valid "$so" && valid "$to" && valid "$ar" && valid "$hd"; then
			printf 'system screen_brightness_mode %s\nglobal stay_on_while_plugged_in %s\nsystem screen_off_timeout %s\nsystem accelerometer_rotation %s\nglobal hide_error_dialogs %s\n' \
				"$bm" "$so" "$to" "$ar" "$hd" > "$F.tmp" && mv -f "$F.tmp" "$F"
			log "snapshot saved: brightness_mode=$bm stay_on=$so screen_off_timeout=$to accelerometer_rotation=$ar hide_error_dialogs=$hd"
		else
			log "snapshot NOT saved (unreadable: bm='$bm' so='$so' to='$to' ar='$ar' hd='$hd') — leaving Android settings untouched"
			exit 1
		fi
	fi
	# audit F05: every put is checked, and the values are read back; a value that did not stick fails the save
	# (rc=1) so the supervisor aborts before SurfaceFlinger is touched and restores the snapshot.
	ok=1
	for kv in "system screen_brightness_mode 0" "system screen_off_timeout 2147483647" \
			"system accelerometer_rotation 0" "global hide_error_dialogs 1"; do
		set -- $kv
		s put "$1" "$2" "$3" >/dev/null
		now=$(s get "$1" "$2")
		if [ "$now" != "$3" ]; then
			s put "$1" "$2" "$3" >/dev/null   # one retry; framework may have been busy
			now=$(s get "$1" "$2")
		fi
		[ "$now" = "$3" ] || { ok=0; log "session value $1 $2: wanted '$3', reads '$now'"; }
	done
	if [ "$ok" != 1 ]; then
		log "session values NOT applied — failing save so the supervisor aborts (snapshot kept for restore)"
		exit 1
	fi
	log "session values set and verified: brightness_mode=$(s get system screen_brightness_mode) screen_off_timeout=$(s get system screen_off_timeout) accelerometer_rotation=$(s get system accelerometer_rotation) hide_error_dialogs=$(s get global hide_error_dialogs)"
	;;
restore)
	[ -f "$F" ] || exit 0
	ok=1
	while read -r ns key val; do
		[ -n "$key" ] || continue
		if [ "$val" = null ]; then s delete "$ns" "$key" >/dev/null; else s put "$ns" "$key" "$val"; fi
		now=$(s get "$ns" "$key")
		if [ "$now" != "$val" ]; then ok=0; log "restore $ns $key: wanted '$val', reads '$now' (will retry)"; fi
	done < "$F"
	if [ "$ok" = 1 ]; then
		rm -f "$F"
		log "Android settings restored and verified"
	else
		exit 1
	fi
	;;
*)
	echo "usage: $0 save|restore" >&2
	exit 2
	;;
esac
exit 0
