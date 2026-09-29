#!/bin/bash
# GNOME peripheral integration for the Fedora chroot.
#
# The Android-side handoff is done by fedora-session-gnome-shell.sh.  This
# script starts only Fedora-owned services after its private system D-Bus is
# available.  Everything is process-scoped to the session and is reaped by
# fedora-restore.sh.
set -u

export PATH=/usr/local/bin:/usr/bin:/usr/sbin
export XDG_RUNTIME_DIR=/run/xdg
export DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket
LOG=/gnome-peripherals.log
RUNNER_PID=$PPID   # session runner; setsid-ed helpers below exit when it is gone

mkdir -p "$XDG_RUNTIME_DIR" /run/NetworkManager /var/lib/NetworkManager
chmod 700 "$XDG_RUNTIME_DIR"
# keep one previous log (NM/wpa run at their default log levels; -dd once reached 1.5 GB)
[ -s "$LOG" ] && mv -f "$LOG" "$LOG.prev"
echo "$(date) peripheral integration: start" >> "$LOG"

start_daemon() {
    name=$1
    shift
    if pidof "$name" >/dev/null 2>&1; then
        echo "$(date) $name already running" >> "$LOG"
        return 0
    fi
    "$@" >> "$LOG" 2>&1 &
    echo "$(date) launched $name pid=$!" >> "$LOG"
}

# Standard system-bus services: UPower exposes the real kernel power-supply
# tree to GNOME; iio-sensor-proxy exposes the Fedora handoff of the sensorhub.
# 2026-09-25: /data is nosuid, so dbus-daemon can never activate PolicyKit1/Accounts through its
# setuid launch helper ("The permission of the setuid helper is not correct": PolicyKit1 214x,
# Accounts 90x, UPower 125x per session), and every GNOME panel that asks polkit/accountsservice
# waits on a failed activation. We are already root here, so start the real daemons directly
# (same approach as upowerd). polkitd must be up BEFORE upowerd (UPower checks polkit).
start_daemon polkitd /usr/lib/polkit-1/polkitd
# accounts-daemon and upowerd connect to polkit synchronously at startup, so wait (bounded, 5 s)
# until polkitd owns its name. No --no-debug: it would hide polkitd's startup errors.
for _i in $(seq 1 25); do
    gdbus call --system --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
        --method org.freedesktop.DBus.NameHasOwner org.freedesktop.PolicyKit1 2>/dev/null | grep -q true && break
    sleep 0.2
done
echo "$(date) polkitd name owned: $(gdbus call --system --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus --method org.freedesktop.DBus.NameHasOwner org.freedesktop.PolicyKit1 2>&1)" >> "$LOG"
start_daemon accounts-daemon /usr/libexec/accounts-daemon
start_daemon upowerd /usr/libexec/upowerd
# 2026-09-26: stock iio-sensor-proxy can never work here (Samsung sensorhub IIO devices have no standard
# channels). fake_sensorproxy.py serves net.hadess.SensorProxy from Android's sensors, fed by
# sensor-bridge-termux.sh over loopback:47820. Rollback: touch /usr/local/etc/no-fake-sensorproxy
if [ -f /usr/local/bin/fake_sensorproxy.py ] && [ ! -e /usr/local/etc/no-fake-sensorproxy ]; then
    start_daemon fake_sensorproxy /usr/bin/python3 /usr/local/bin/fake_sensorproxy.py
else
    start_daemon iio-sensor-proxy /usr/libexec/iio-sensor-proxy
fi

# 2026-09-24 Wi-Fi in GNOME: NetworkManager marked wlan0 unmanaged ("link is not
# initialized by udev", reason 71) because this private eudev namespace never
# writes a udev db entry for net devices. Verified against systemd v259
# sd-device.c: "devices with a database entry are initialized" and a netdev's db
# id is "n<ifindex>" (/run/udev/data/n<ifindex>). Write that one tiny file in
# the session-private /run/udev (chroot tmpfs) BEFORE NM starts. No udevadm
# trigger, no real /dev involvement, nothing outside this namespace.
if [ -r /sys/class/net/wlan0/ifindex ]; then
    wl_idx=$(cat /sys/class/net/wlan0/ifindex)
    mkdir -p /run/udev/data
    if [ ! -e "/run/udev/data/n$wl_idx" ]; then
        printf 'I:%s000000\n' "$(date +%s)" > "/run/udev/data/n$wl_idx"
    fi
    echo "$(date) wifi: udev db entry for wlan0 (ifindex $wl_idx) present" >> "$LOG"
fi
# NM's wpa_supplicant backend needs the D-Bus-enabled daemon; there is no systemd
# to activate it, so start it explicitly (Android's copy is stopped by the harness).
mkdir -p /run/wpa_supplicant
start_daemon wpa_supplicant env LD_PRELOAD=/usr/local/lib/wlan_keepup_shim.so /usr/sbin/wpa_supplicant -u -O /run/wpa_supplicant
sleep 1
# 2026-09-25: Android netd deletes the "lookup main" policy rule and routes via per-network
# tables, so NM's main-table routes were never used ("connected" but no internet, and the
# kernel even rejected NM's default route: "Nexthop has invalid gateway"). Add a main-table
# rule after Android's fwmark rules; fedora-restore.sh removes it again at session end.
ip -4 rule show | grep -q '^21000:' || ip -4 rule add pref 21000 lookup main
ip -6 rule show | grep -q '^21000:' || ip -6 rule add pref 21000 lookup main
echo "$(date) policy rule pref 21000 lookup main added" >> "$LOG"
# --no-daemon keeps NM in this process tree (reaped by fedora-restore.sh). For Wi-Fi
# debugging run `nmcli general logging level DEBUG domains WIFI,SUPPLICANT,DEVICE`
# by hand; the per-second /wifi-trace.log poller from 2026-09-25 is gone.
start_daemon NetworkManager env LD_PRELOAD=/usr/local/lib/wlan_keepup_shim.so /usr/bin/NetworkManager --no-daemon

# Keep audio entirely inside the proven Android PulseAudio bridge.  A missing
# Termux listener is harmless: the resilient parec|pacat loop retries until
# the user starts its Termux-side helper.
start_daemon pipewire /usr/bin/pipewire
sleep 1
start_daemon pipewire-pulse /usr/bin/pipewire-pulse
sleep 1
start_daemon wireplumber /usr/bin/wireplumber
# wait for pipewire-pulse to answer instead of a fixed sleep (<= 5 s)
for _i in 1 2 3 4 5 6 7 8 9 10; do /usr/bin/pactl info >/dev/null 2>&1 && break; sleep 0.5; done
if /usr/bin/pactl load-module module-null-sink sink_name=bridge \
    sink_properties=device.description=Bridge >> "$LOG" 2>&1; then
    /usr/bin/pactl set-default-sink bridge >> "$LOG" 2>&1 || true
    echo "$(date) bridge sink is GNOME default" >> "$LOG"
fi
if [ -x /usr/bin/parec ] && [ -x /usr/bin/pacat ]; then
    # was append-forever: 694 MB of "Connection refused" by 2026-09-25
    [ -s /audio-bridge-loop.log ] && mv -f /audio-bridge-loop.log /audio-bridge-loop.log.prev
    setsid /bin/bash -c '
        # Back off (1 s -> 16 s) while the Termux listener is absent, so an idle
        # bridge does not fork parec+pacat every second all session.
        delay=1
        while kill -0 '"$RUNNER_PID"' 2>/dev/null; do
            t0=$(date +%s)
            /usr/bin/parec -d bridge.monitor --latency-msec=60 |
                /usr/bin/pacat --server=tcp:127.0.0.1:4713 --latency-msec=60
            if [ $(( $(date +%s) - t0 )) -ge 10 ]; then delay=1
            elif [ "$delay" -lt 16 ]; then delay=$((delay * 2)); fi
            sleep "$delay"
        done
    ' >> /audio-bridge-loop.log 2>&1 &
    echo "$(date) audio bridge retry loop pid=$!" >> "$LOG"
fi

echo "$(date) peripheral integration: ready" >> "$LOG"
