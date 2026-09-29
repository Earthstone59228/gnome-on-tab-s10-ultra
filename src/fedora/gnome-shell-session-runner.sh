#!/bin/bash
# gnome-shell-session-runner.sh — GNOME Shell (mutter 50.4) session inside the
# Fedora chroot, launched by /data/local/tmp/fedora-session-gnome-shell.sh as
# u:r:untrusted_app:s0.
#
# Pass 1 (UDEV_NS_ENTERED unset): enter a private mount namespace, give it its own
# /dev/input and /dev/dri tmpfs with mknod'd nodes (eudev in here must never touch
# the real /dev — see fedora-native/03), bind binderfs, start udevd and the input
# hotplug loop, then re-exec this script. Pass 2: session + system D-Bus,
# fake_logind.py (org.freedesktop.login1), peripherals, then gnome-shell.
#
# Pieces this depends on (sources in artifacts/defex-off/):
#   - gnome_gbm_shim.so: replaces libgbm for mutter (AHardwareBuffer-backed
#     gbm_surface, EGL platform reroute, env scrub for spawned apps)
#   - liboutline-atomics-shim.so: 4 outline-atomics symbols the hybris'd Mali
#     blob needs from glibc (driver-build/04)
#   - fake_logind.py: TakeDevice/ReleaseDevice + DRM master for mutter
# Full bring-up history: fedora-native/driver-build/02 and 10.
if [ -z "$UDEV_NS_ENTERED" ]; then
	# 2026-09-27: pass 1 inherits Android's PATH (no /usr/bin) -> "mv: command not found", so this log
	# rotation never ran and gnome-shell-attempt.log grew to 465 MB since 09-20.
	export PATH=/usr/local/bin:/usr/bin:/usr/sbin
	# Pass 1 only: keep one previous copy of each session log (the old append-forever
	# gnome-shell-attempt.log reached 465 MB / 4.3 M lines of per-frame debug output).
	for f in /gnome-shell-attempt.log /fake-logind.log /dbus-system.log /fake-sessionmanager.log /fake-bluetooth.log /android-camera-node.log /android-mic-node.log /android-autobrightness.log /gsd-*.log; do
		case "$f" in *.prev) continue;; esac
		[ -s "$f" ] && mv -f "$f" "$f.prev"
	done
	# 2026-09-27: runtime S Pen rotation rule from a previous session (pen_rotate.py) must not apply at start
	rm -f /run/udev/rules.d/92-spen-rotation.rules
	export UDEV_NS_ENTERED=1
	exec /usr/bin/unshare --mount --propagation unchanged /bin/bash -c '
		export PATH=/usr/local/eudev/bin:/usr/bin:/usr/sbin
		mount --make-rprivate /dev
		mount -t tmpfs tmpfs /dev/input
		mount -t tmpfs tmpfs /dev/dri
		mknod /dev/input/event0 c 13 64
		mknod /dev/input/event1 c 13 65
		mknod /dev/input/event2 c 13 66
		mknod /dev/input/event3 c 13 67
		mknod /dev/input/event4 c 13 68
		mknod /dev/input/event5 c 13 69
		mknod /dev/input/event6 c 13 70
		mknod /dev/input/event7 c 13 71
		mknod /dev/input/event8 c 13 72
		mknod /dev/input/event9 c 13 73
		mknod /dev/input/event10 c 13 74
		mknod /dev/input/event11 c 13 75
		mknod /dev/input/event12 c 13 76
		mknod /dev/input/event13 c 13 77
		mknod /dev/input/event14 c 13 78
		mknod /dev/input/event15 c 13 79
		mknod /dev/input/event16 c 13 80
		# 2026-09-24: the Book Cover Keyboard enumerates as event17 (past the static
		# list) so its node was missing from this private /dev. Add a node for every
		# event device the kernel lists at session start (event N = minor 64+N).
		for d in /sys/class/input/event*; do n=${d##*event}; [ -e /dev/input/event$n ] || mknod /dev/input/event$n c 13 $((64 + n)); done
		mknod /dev/dri/card0 c 226 0
		# 2026-09-18 night finding: attempt #5 (strace -f) showed a Mali/MTK
		# vendor GED thread stuck in an infinite 1s retry loop trying to
		# reach Android binder ("Could not connect socket at path" /
		# "Waiting 1s on context object(s)"), holding up the WHOLE compositor
		# forever (every other thread just waits on it) — this, not a KMS/
		# fencing issue, is why "Queue mode set" was the last log line.
		# /dev/binderfs is its own separate mount on the real Android side
		# (`binder /dev/binderfs binder ...`), which the chroot plain
		# (non-recursive) `mount -o bind /dev "$R/dev"` in fedora-enter.sh
		# never reliably captures — same root cause class as the earlier
		# /dev/input findings, different subsystem. Fix: mknod the binder
		# devices directly. FIRST ATTEMPT (raw mknod matching the real
		# major:minor, same technique as /dev/dri and /dev/input above)
		# FAILED live: "Binder driver /dev/binder could not be opened.
		# Error: 6 (No such device or address)" — binderfs is NOT a
		# simple global-singleton chrdev like DRM/evdev; each binderfs
		# MOUNT INSTANCE gets its own dynamically-registered minor
		# range, so a bare mknod with a matching number fails to
		# resolve to a valid open target from a different mount
		# context (ENXIO).
		# REAL fix, live-verified working (open() returns a valid fd):
		# bind-mount the actual binderfs directory itself.
		mount --bind /dev/binderfs /dev/binderfs
		udevd &
		sleep 1
		udevadm trigger --subsystem-match=input --subsystem-match=drm
		udevadm settle --timeout=10
		echo "$(date) runner(gnome-shell): udev namespace ready" >> /gnome-shell-attempt.log
		# 2026-09-25 input hotplug (cover keyboard attached mid-session). A device
		# that appears after start gets an eventN with no node in this private
		# /dev/input tmpfs, so mutter/libinput (via fake_logind TakeDevice, which
		# opens /dev/input/eventN by path) fails the open on the first "add" and
		# never retries. Poll sysfs; for any eventN lacking a node: mknod it (N ->
		# minor 64+N, private tmpfs only), wait for udevd to finish its first pass
		# (db file c13:<minor>), then re-send an "add" for that ONE device so
		# libinput retries the open. Only fires for a node created here, and only
		# touches this namespace tmpfs copy (never the real /dev node).
		(
			while :; do
				for d in /sys/class/input/event*; do
					[ -e "$d" ] || continue
					n=${d##*event}
					[ -e /dev/input/event$n ] && continue
					mknod /dev/input/event$n c 13 $((64 + n)) || continue
					i=0
					while [ ! -e /run/udev/data/c13:$((64 + n)) ] && [ $i -lt 15 ]; do sleep 0.2; i=$((i + 1)); done
					udevadm trigger --action=add --sysname-match=event$n
					echo "$(date) runner(gnome-shell): hotplug event$n node created + add re-sent (db wait ${i}x0.2s)" >> /gnome-shell-attempt.log
				done
				sleep 1
			done
		) &
		exec /bin/bash /root/gnome-shell-session-runner.sh
	'
fi

export PATH=/usr/local/bin:/usr/bin:/usr/sbin TMPDIR=/tmp HOME=/root LANG=C.utf8
export XDG_CURRENT_DESKTOP=GNOME XDG_SESSION_DESKTOP=gnome XDG_SESSION_TYPE=wayland
export XDG_RUNTIME_DIR=/run/xdg

# 2026-09-20: app-drawer-empty fix (driver-build/02's "REAL root cause" section).
# GLib's documented G_RESOURCE_OVERLAYS mechanism (since 2.50, confirmed present
# in this device's libgio-2.0.so via `strings`, and confirmed live via a
# standalone offline test — Gio.resources_lookup_data() returns the overlay
# content, sibling resources unaffected) swaps just this one compiled-in JS
# module for our patched copy on disk — NOT a libshell-18.so binary patch. Falls
# back to the untouched built-in copy if the overlay file is ever missing, so a
# bad push here fails safe. Patch: harden _translatePreviousPageIcons/
# _translateNextPageIcons's guards so an invalid this._currentPage (which a
# single-page app grid + touch swipe can produce via a real, source-verified
# upstream SwipeTracker bug) can't reach getItemsAtPage(NaN) and throw mid-
# _syncPageIndicators, which used to abort before the icon-translationX reset
# ran and left icons stuck off-screen.
#
# 2026-09-20, same evening: THE REAL primary cause of the empty/grey app grid
# (confirmed live via temporary instrumentation: Shell.AppSystem.get_installed()
# correctly returns 69 apps, but zero survive AppDisplay._loadApps()'s own
# filter). misc/parentalControlsManager.js's shouldShowApp() hides EVERY app
# until this._initialized becomes true, which only happens after a REAL D-Bus
# call to malcontent's parental-controls service succeeds -- and on failure,
# upstream's catch block logs an error and `return`s WITHOUT ever setting
# this._initialized, permanently blocking 100% of apps. This chroot has no
# malcontent/AccountsService D-Bus stack at all, so this catch fires every
# single session. Fix: treat an unreachable parental-controls service the
# same as the already-handled "globally disabled" case elsewhere in the same
# file (fail open, not closed).
export G_RESOURCE_OVERLAYS="/org/gnome/shell/ui/appDisplay.js=/usr/local/share/gnome-shell-fixes/ui/appDisplay.js:/org/gnome/shell/misc/parentalControlsManager.js=/usr/local/share/gnome-shell-fixes/misc/parentalControlsManager.js"
# 2026-09-27: brightness changes fade over ~250 ms (like Android) instead of jumping. Overlay of
# misc/brightnessManager.js (base byte-identical to gnome-shell 50.4); each fade step goes through mutter itself,
# so the P.1 feedback loop cannot come back. Opt-out: /usr/local/etc/no-brightness-ramp
if [ -f /usr/local/share/gnome-shell-fixes/misc/brightnessManager.js ] && [ ! -e /usr/local/etc/no-brightness-ramp ]; then
	G_RESOURCE_OVERLAYS="$G_RESOURCE_OVERLAYS:/org/gnome/shell/misc/brightnessManager.js=/usr/local/share/gnome-shell-fixes/misc/brightnessManager.js"
fi

mkdir -p /run/xdg /tmp/.X11-unix /run/dbus
chmod 700 /run/xdg
chmod 1777 /tmp/.X11-unix

# Session bus (dconf, portals, gnome-shell's own session-level D-Bus calls).
eval "$(dbus-launch --sh-syntax 2>/dev/null)"
echo "$(date) runner(gnome-shell): session bus at $DBUS_SESSION_BUS_ADDRESS" >> /gnome-shell-attempt.log

# System bus (fake_logind.py registers org.freedesktop.login1 here — mutter's
# meta-launcher.c/meta-device-pool.c only ever talk to logind over the system
# bus, never the session bus).
rm -f /run/dbus/system_bus_socket
dbus-daemon --system --nofork --nopidfile >> /dbus-system.log 2>&1 &
for i in $(seq 1 20); do [ -S /run/dbus/system_bus_socket ] && break; sleep 0.2; done
echo "$(date) runner(gnome-shell): system bus socket present: $([ -S /run/dbus/system_bus_socket ] && echo yes || echo NO)" >> /gnome-shell-attempt.log

# fake_logind.py — must own org.freedesktop.login1 before gnome-shell starts.
python3 /usr/local/bin/fake_logind.py >> /fake-logind.log 2>&1 &
FAKELOGIND_PID=$!
for i in $(seq 1 30); do
	gdbus call --system --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
		--method org.freedesktop.DBus.GetNameOwner org.freedesktop.login1 >/dev/null 2>&1 && break
	sleep 0.2
done
echo "$(date) runner(gnome-shell): fake_logind.py pid=$FAKELOGIND_PID, org.freedesktop.login1 owned: $(gdbus call --system --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.GetNameOwner org.freedesktop.login1 2>&1)" >> /gnome-shell-attempt.log

# Lock screen (2026-09-27). gnome-shell unlocks only through GDM (UserVerifier); fake_gdm.py serves that and checks
# the root password in /etc/shadow. The lock is armed ONLY when fake_gdm owns org.gnome.DisplayManager AND root has a
# real password hash (set with: passwd root, inside the chroot). Otherwise the lock screen stays disabled exactly as
# before (a lock without a way to answer it would be a lock-out). Power button then locks; auto-lock on idle stays off.
# Recovery: fake-gdm-recovery (see 00-CURRENT-STATE.md) or the panic chord. Opt-out: /usr/local/etc/no-lock-screen
rm -f /run/fedora-lock-ready
LOCK_READY=0
if [ -f /usr/local/bin/fake_gdm.py ] && [ ! -e /usr/local/etc/no-lock-screen ] && grep -q "^root:[$]" /etc/shadow; then
	# respawned if it ever dies: a locked screen must always have its unlock service (audit F4)
	( while :; do python3 /usr/local/bin/fake_gdm.py >> /fake-gdm.log 2>&1; sleep 1; done ) &
	for i in $(seq 1 40); do
		gdbus call --system --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
			--method org.freedesktop.DBus.NameHasOwner org.gnome.DisplayManager 2>/dev/null | grep -q true && { LOCK_READY=1; break; }
		sleep 0.25
	done
fi
if [ "$LOCK_READY" = 1 ] && [ -f /usr/local/share/gnome-shell-fixes/misc/loginManager.js ]; then
	export G_RESOURCE_OVERLAYS="$G_RESOURCE_OVERLAYS:/org/gnome/shell/misc/loginManager.js=/usr/local/share/gnome-shell-fixes/misc/loginManager.js"
	gsettings set org.gnome.desktop.lockdown disable-lock-screen false 2>/dev/null
	gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null
	touch /run/fedora-lock-ready
	echo "$(date) runner(gnome-shell): lock screen ARMED (fake_gdm up, root password set)" >> /gnome-shell-attempt.log
else
	gsettings set org.gnome.desktop.lockdown disable-lock-screen true 2>/dev/null
	gsettings set org.gnome.desktop.screensaver lock-enabled false 2>/dev/null
	echo "$(date) runner(gnome-shell): lock screen disabled (no root password or no fake_gdm; disable-lock-screen=$(gsettings get org.gnome.desktop.lockdown disable-lock-screen 2>&1))" >> /gnome-shell-attempt.log
fi

# 2026-09-27 (screenblank): power button + book cover = pure-black screen drawn by the shell extension
# screen-blank@fedora-tab (panel stays powered: a real panel power-down detaches the pogo keyboard and crash-loops
# Android). fake_sessionmanager.py / fake_logind.py call it on the session bus. Opt-out: /usr/local/etc/no-screen-blank
SB_UUID=screen-blank@fedora-tab
SB_CUR=$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null)
if [ -d /usr/local/share/gnome-shell/extensions/$SB_UUID ] && [ ! -e /usr/local/etc/no-screen-blank ]; then
	case "$SB_CUR" in
	*$SB_UUID*) ;;
	""|"@as []"|"[]") gsettings set org.gnome.shell enabled-extensions "[\"$SB_UUID\"]" ;;
	*) gsettings set org.gnome.shell enabled-extensions "${SB_CUR%]}, \"$SB_UUID\"]" ;;
	esac
	gsettings set org.gnome.shell disable-user-extensions false
else
	case "$SB_CUR" in
	*$SB_UUID*) gsettings set org.gnome.shell enabled-extensions "$(echo "$SB_CUR" | sed -e "s/, *.$SB_UUID.//; s/.$SB_UUID., *//; s/.$SB_UUID.//")" ;;
	esac
fi
echo "$(date) runner(gnome-shell): screen-blank extension: enabled-extensions=$(gsettings get org.gnome.shell enabled-extensions 2>&1)" >> /gnome-shell-attempt.log

# Start GNOME-facing peripheral services only after the private system bus and
# fake logind are live.  This gives GNOME the real battery/backlight path,
# iio-sensor-proxy, NetworkManager with its internal DHCP client, and the
# existing Android PulseAudio audio bridge as the default sink.
if [ -x /root/gnome-peripherals.sh ]; then
	/root/gnome-peripherals.sh
	peripheral_rc=$?
	echo "$(date) runner(gnome-shell): peripheral integration rc=$peripheral_rc" >> /gnome-shell-attempt.log
else
	echo "$(date) runner(gnome-shell): WARNING /root/gnome-peripherals.sh missing" >> /gnome-shell-attempt.log
fi

# 2026-09-27: GNOME Bluetooth UI backed by Android (fake org.bluez on the system bus + Rfkill on the session bus;
# no BlueZ, no /dev/rfkill). Must own both names BEFORE gnome-shell starts: the shell builds its Rfkill proxy at
# startup and hides the Bluetooth toggle unless BluetoothHasAirplaneMode. Opt-out: /usr/local/etc/no-fake-bluetooth
if [ -f /usr/local/bin/fake_bluetooth.py ] && [ ! -e /usr/local/etc/no-fake-bluetooth ]; then
	python3 /usr/local/bin/fake_bluetooth.py >> /fake-bluetooth.log 2>&1 &
	echo "$(date) runner(gnome-shell): started fake_bluetooth pid=$!" >> /gnome-shell-attempt.log
	for i in $(seq 1 20); do
		gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
			--method org.freedesktop.DBus.NameHasOwner org.gnome.SettingsDaemon.Rfkill 2>/dev/null | grep -q true && break
		sleep 0.25
	done
fi
# 2026-09-28: on-demand PipeWire camera nodes for the front and back Android cameras
# (android_camera_node.py). The front and back camera nodes are ALWAYS listed in GNOME (Snapshot, Firefox
# via xdg-desktop-portal-camera); the daemon only asks Android to turn its camera on while something is
# actually linked to one of the two nodes (a WirePlumber link count), and off again a few seconds after the
# last consumer leaves. Independent of fake_bluetooth.py; order relative to it does not matter, only that
# pipewire and wireplumber (started above by gnome-peripherals.sh) are already up. Opt-out:
# /usr/local/etc/no-android-camera-node
if [ -f /usr/local/bin/android_camera_node.py ] && [ ! -e /usr/local/etc/no-android-camera-node ]; then
	python3 /usr/local/bin/android_camera_node.py >> /android-camera-node.log 2>&1 &
	echo "$(date) runner(gnome-shell): started android_camera_node pid=$!" >> /gnome-shell-attempt.log
fi
# 2026-09-28 (doc 11 AE): Android microphone as the GNOME source android_mic (android_mic_node.py). Always listed;
# Android mic (scrcpy server as the shell uid, via android-bridge.sh mic-start) runs only while something records.
# Needs pipewire-pulse (started above by gnome-peripherals.sh). Opt-out: /usr/local/etc/no-android-mic-node
if [ -f /usr/local/bin/android_mic_node.py ] && [ ! -e /usr/local/etc/no-android-mic-node ]; then
	python3 /usr/local/bin/android_mic_node.py >> /android-mic-node.log 2>&1 &
	echo "$(date) runner(gnome-shell): started android_mic_node pid=$!" >> /gnome-shell-attempt.log
fi
# 2026-09-28 (doc 11 AG): Android/Samsung auto-brightness (android_autobrightness.py) instead of the gsd-power curve.
# Gets raw lux from fake_sensorproxy over loopback UDP, follows the GNOME Automatic Brightness toggle, sends the shell
# SetAutoBrightnessTarget. Needs the session bus (above) and the measured map /usr/local/etc/android-brightness-map.json.
# Opt-out (GNOME algorithm again, also in fake_sensorproxy): /usr/local/etc/no-android-autobrightness
if [ -f /usr/local/bin/android_autobrightness.py ] && [ ! -e /usr/local/etc/no-android-autobrightness ]; then
	python3 /usr/local/bin/android_autobrightness.py >> /android-autobrightness.log 2>&1 &
	echo "$(date) runner(gnome-shell): started android_autobrightness pid=$!" >> /gnome-shell-attempt.log
fi

# 2026-09-26: gnome-settings-daemon plugins. There is no gnome-session here, so NOTHING started them (volume keys,
# auto-brightness, night light, keyboard/sound/xsettings were all dead). Started from a subshell forked BEFORE the
# hybris/Mali env below, so they never inherit LD_PRELOAD / the bionic LD_LIBRARY_PATH. Waits for org.gnome.Shell on
# the session bus (media-keys needs the shell's accelerator API). Deliberately NOT started: gsd-rfkill (radio writes;
# Android's airplane_mode_on once flipped by itself), gsd-wwan, gsd-smartcard, gsd-usb-protection, gsd-printer,
# gsd-print-notifications, gsd-sharing. Rollback: touch /usr/local/etc/no-gsd (chroot) = none of this runs.
if [ ! -e /usr/local/etc/no-gsd ]; then
(
	for i in $(seq 1 240); do
		gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
			--method org.freedesktop.DBus.NameHasOwner org.gnome.Shell 2>/dev/null | grep -q true && break
		sleep 0.5
	done
	sleep 4
	# Safe defaults, applied ONCE (marker), so later changes in GNOME Settings are not overwritten:
	# no screen lock (no known password to unlock -> lock-out), no idle blank (untested KMS blank/unblank path),
	# no suspend actions (fake logind has no Suspend), power button does nothing here, no idle dim (untested),
	# ambient-enabled stays on. (Volume statics are NOT swapped: the user prefers inverted tablet buttons over an inverted keyboard.)
	if [ ! -e /root/.gsd-defaults-20260926 ]; then
		P=org.gnome.settings-daemon.plugins
		gsettings set org.gnome.desktop.screensaver lock-enabled false
		gsettings set org.gnome.desktop.session idle-delay 0
		gsettings set $P.power sleep-inactive-ac-type nothing
		gsettings set $P.power sleep-inactive-battery-type nothing
		gsettings set $P.power power-button-action nothing
		gsettings set $P.power idle-dim false
		touch /root/.gsd-defaults-20260926
		echo "$(date) runner(gnome-shell): gsd safe defaults applied" >> /gnome-shell-attempt.log
	fi
	# 2026-09-26 audit #3, once: power button -> "interactive" (gsd -> org.gnome.SessionManager.Shutdown ->
	# fake_sessionmanager shows GNOME's "Log Out" dialog; confirming ends the session = back to Android;
	# letting the countdown run out does NOT). Always show "Log Out" in the system menu (same effect).
	if [ ! -e /root/.gsd-defaults-audit3-20260926 ]; then
		gsettings set org.gnome.settings-daemon.plugins.power power-button-action interactive
		gsettings set org.gnome.shell always-show-log-out true
		touch /root/.gsd-defaults-audit3-20260926
		echo "$(date) runner(gnome-shell): power button = Log Out dialog, Log Out always shown" >> /gnome-shell-attempt.log
	fi
	# org.gnome.SessionManager stand-in: gsd-power's session_is_active (=> light-sensor claim => auto-brightness, idle logic)
	# comes ONLY from its SessionIsActive property; there is no gnome-session here. Started after the shell is up so the
	# shell's own startup is unchanged. Cannot shut down / reboot / suspend / log out (those return errors).
	# Rollback: touch /usr/local/etc/no-fake-sessionmanager
	if [ -f /usr/local/bin/fake_sessionmanager.py ] && [ ! -e /usr/local/etc/no-fake-sessionmanager ]; then
		python3 /usr/local/bin/fake_sessionmanager.py >> /fake-sessionmanager.log 2>&1 &
		echo "$(date) runner(gnome-shell): started fake_sessionmanager pid=$!" >> /gnome-shell-attempt.log
		for i in $(seq 1 20); do
			gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
				--method org.freedesktop.DBus.NameHasOwner org.gnome.SessionManager 2>/dev/null | grep -q true && break
			sleep 0.25
		done
	fi
	# 2026-09-27: keep the S Pen aligned with display rotation (mutter rotates the touchscreen itself, not the pen;
	# measured with virtual clones). Opt-out: /usr/local/etc/no-pen-rotate
	if [ -f /usr/local/bin/pen_rotate.py ] && [ ! -e /usr/local/etc/no-pen-rotate ]; then
		python3 /usr/local/bin/pen_rotate.py >> /pen-rotate.log 2>&1 &
		echo "$(date) runner(gnome-shell): started pen_rotate pid=$!" >> /gnome-shell-attempt.log
	fi
	# 2026-09-27 UN-AUDITED: virtual SW_TABLET_MODE switch — mutter auto-rotate requires touch mode; the S Pen
	# is a permanent pointer-class device which would disable touch mode forever, and this device has no real
	# tablet-mode switch. The virtual switch overrides (mutter honors it above pointer presence), driven by the
	# pogo keyboard state: detached = rotate, attached = no rotation while typing. Opt-out: no-tablet-mode
	if [ -f /usr/local/bin/tablet_mode_keeper.py ] && [ ! -e /usr/local/etc/no-tablet-mode ]; then
		python3 /usr/local/bin/tablet_mode_keeper.py >> /tablet-mode.log 2>&1 &
		echo "$(date) runner(gnome-shell): started tablet_mode_keeper pid=$!" >> /gnome-shell-attempt.log
	fi
	for d in gsd-media-keys gsd-power gsd-color gsd-keyboard gsd-sound gsd-xsettings gsd-housekeeping gsd-a11y-settings gsd-datetime gsd-screensaver-proxy; do
		if [ -x /usr/libexec/$d ]; then
			nohup /usr/libexec/$d >> /$d.log 2>&1 &
			echo "$(date) runner(gnome-shell): started $d pid=$!" >> /gnome-shell-attempt.log
			sleep 0.3
		fi
	done
) &
fi

# ---- hybris/Mali env (driver-build/04) — gnome-shell only. Exported here, after
# D-Bus, fake_logind and the peripheral daemons are up, so those never inherit the
# bionic vendor LD_LIBRARY_PATH. ----
#
# 2026-09-25 client GPU (CLIENT_GPU=1, default): /usr/local/lib/hybris-wl is upstream
# libhybris (7079712, with 1364f45 reverted) built with --enable-wayland, plus its glvnd
# vendor library. mutter's EGL display then offers EGL_WL_bind_wayland_display, mutter
# binds it (android_wlegl global), and apps get the Mali GPU through Fedora's own glvnd
# libEGL: gnome_gbm_shim hands every spawned app the GBMSHIM_CHILD_* variables below as
# plain variables, but only after mutter's eglBindWaylandDisplayWL succeeded. Needs
# hybris-vendor/libui_compat_layer.so (gralloc for hybris; source ui_compat_ahb.c).
# The new build finds its own linker (hybris-wl/libhybris/linker/q.so, no outline-atomics
# imports) and platform plugins by default, so HYBRIS_LINKER_DIR/_EGLPLATFORM_DIR stay unset.
# Rollback without touching libraries: create /usr/local/etc/no-client-gpu in the chroot
# (Android side: touch /data/fedora/usr/local/etc/no-client-gpu) -> previous
# /usr/local/lib/hybris stack, apps on Mesa llvmpipe. Delete the file to re-enable.
CLIENT_GPU=1
[ -e /usr/local/etc/no-client-gpu ] && CLIENT_GPU=0
if [ "$CLIENT_GPU" = 1 ] && [ -e /usr/local/lib/hybris-wl/libEGL.so.1 ] &&
		[ -e /usr/local/lib/hybris-wl/libEGL_libhybris.so.0 ] &&
		[ -e /usr/local/lib/hybris-vendor/libui_compat_layer.so ] &&
		[ -e /usr/local/share/glvnd/egl_vendor.d/10_libhybris.json ]; then
	export LD_LIBRARY_PATH=/usr/local/lib/hybris-vendor:/usr/local/lib/hybris-wl
	export GBMSHIM_CHILD___EGL_VENDOR_LIBRARY_FILENAMES=/usr/local/share/glvnd/egl_vendor.d/10_libhybris.json:/usr/share/glvnd/egl_vendor.d/50_mesa.json
	export GBMSHIM_CHILD_LIBEGL=/usr/local/lib/hybris-vendor/libGLES_mali.so
	export GBMSHIM_CHILD_LIBGLESV2=/usr/local/lib/hybris-vendor/libGLES_mali.so
	# GTK 4.22 defaults to Vulkan (lavapipe here); "gl" = its GL renderer on GLES/Mali.
	export GBMSHIM_CHILD_GSK_RENDERER=gl
	# 2026-09-27: bionic code (Mali blob) reads/zeroes fixed thread-pointer slots (tp+0x18, tp+0x28) that in a
	# glibc process belong to the FIRST TLS module (libc's ctype pointers, libpython's thread state...). A
	# zero-filled TLS block preloaded first turns tp+16..tp+272 into dead space (verified with tlsprobe).
	# Does not help executables with their own TLS (firefox). Opt-out: /usr/local/etc/no-tls-padding
	if [ -f /usr/local/lib/libtls-padding.so ] && [ ! -e /usr/local/etc/no-tls-padding ]; then
		export GBMSHIM_CHILD_LD_PRELOAD=/usr/local/lib/libtls-padding.so
	fi
	# Give newly launched apps the working libhybris Vulkan loader without
	# changing their EGL/GLES or OpenGL libraries. gnome_gbm_shim injects these
	# only after mutter has bound the Wayland display. The directory contains
	# Vulkan and its direct libhybris dependency, and no libEGL or libGL.
	# Rollback: touch /usr/local/etc/no-auto-vulkan before the next session.
	if [ ! -e /usr/local/etc/no-auto-vulkan ] &&
			[ -e /usr/local/lib/mali-vulkan-default/libvulkan.so.1 ] &&
			[ -e /usr/local/lib/mali-vulkan-default/libhybris-common.so.1 ]; then
		export GBMSHIM_CHILD_LD_LIBRARY_PATH=/usr/local/lib/mali-vulkan-default
		export GBMSHIM_CHILD_HYBRIS_LD_LIBRARY_PATH=/usr/local/lib/mali-vulkan/android:/usr/local/lib/hybris-vendor
		export GBMSHIM_CHILD_HYBRIS_VULKANPLATFORM=wayland
		export GBMSHIM_CHILD_HYBRIS_VULKANPLATFORM_DIR=/usr/local/lib/mali-vulkan
		export GBMSHIM_CHILD_LIBVULKAN=/usr/local/lib/mali-vulkan/android/libvulkan-android.so
		echo "$(date) runner(gnome-shell): Vulkan loader enabled for apps" >> /gnome-shell-attempt.log
	fi
	echo "$(date) runner(gnome-shell): client GPU stack /usr/local/lib/hybris-wl" >> /gnome-shell-attempt.log
else
	export LD_LIBRARY_PATH=/usr/local/lib/hybris-vendor:/usr/local/lib/hybris
	export HYBRIS_LINKER_DIR=/usr/local/lib/hybris-vendor
	export HYBRIS_EGLPLATFORM_DIR=/usr/local/lib/hybris
	echo "$(date) runner(gnome-shell): previous hybris stack (CLIENT_GPU=$CLIENT_GPU), apps on Mesa" >> /gnome-shell-attempt.log
fi
export HYBRIS_EGLPLATFORM=null
export HYBRIS_LOGGING_TARGET=stderr
export HYBRIS_LOGGING_LEVEL=warning
export LIBEGL=/usr/local/lib/hybris-vendor/libGLES_mali.so
export LIBGLESV2=/usr/local/lib/hybris-vendor/libGLES_mali.so

# No MUTTER_DEBUG / G_MESSAGES_DEBUG: they were bring-up instrumentation and cost
# ~20 formatted+flushed log lines per frame on the compositor threads. For a debug
# run, export them by hand (e.g. MUTTER_DEBUG=kms) — gnome_gbm_shim scrubs both
# from spawned apps. monitor_debug_shim.so (09-19 stall hunt) is no longer preloaded.
export LD_PRELOAD=/usr/local/lib/liboutline-atomics-shim.so:/usr/local/lib/gnome_gbm_shim.so
# 2026-09-27: the Mali blob inside gnome-shell reads/zeroes tp+0x18 and tp+0x28 too (same bionic TLS slots as in
# the apps); gnome-shell and both shims above have no TLS of their own, so a padding block preloaded FIRST sits at
# tp+16..tp+272 and those writes land in dead space instead of the first real TLS module.
# Opt-out: /usr/local/etc/no-tls-padding or /usr/local/etc/no-tls-padding-shell
if [ -f /usr/local/lib/libtls-padding.so ] && [ ! -e /usr/local/etc/no-tls-padding ] && [ ! -e /usr/local/etc/no-tls-padding-shell ]; then
	export LD_PRELOAD=/usr/local/lib/libtls-padding.so:$LD_PRELOAD
	echo "$(date) runner(gnome-shell): libtls-padding.so preloaded first for gnome-shell" >> /gnome-shell-attempt.log
fi

# Hardware cursor plane faults the SMMU/DEVAPC on this panel (kernel panic, see
# driver-build/06); mutter's own switch, source-verified in 50.4.
export MUTTER_DEBUG_DISABLE_HW_CURSORS=1

# XDG_SESSION_ID deliberately unset: mutter then asks fake_logind.py via
# Manager.GetSessionByPID instead of looking for /run/systemd/sessions files.
# mutter 50.4 only has the gbm_surface path (create_surfaces_gbm); there is no
# direct-BO fallback, so gnome_gbm_shim.so is mandatory.
# 2026-09-29: Xwayland enabled (user-approved) so X11/GLX apps (KiCad, OpenSCAD via mali-gl-x11) can run.
# XWAYLAND_NO_GLAMOR=1 (string verified in the deployed Xwayland) keeps Xwayland on wl_shm buffers: no EGL/GPU
# inside Xwayland itself. Opt-out (back to --no-x11): touch /usr/local/etc/no-xwayland
X11FLAG=""
if [ -e /usr/local/etc/no-xwayland ] || [ ! -x /usr/bin/Xwayland ]; then
	X11FLAG="--no-x11"
else
	export XWAYLAND_NO_GLAMOR=1
fi
echo "$(date) runner(gnome-shell): launching gnome-shell --wayland $X11FLAG" >> /gnome-shell-attempt.log
gnome-shell --wayland $X11FLAG >> /gnome-shell-attempt.log 2>&1
RC=$?
echo "$(date) runner(gnome-shell): gnome-shell exited rc=$RC" >> /gnome-shell-attempt.log

kill "$FAKELOGIND_PID" 2>/dev/null
exit "$RC"
