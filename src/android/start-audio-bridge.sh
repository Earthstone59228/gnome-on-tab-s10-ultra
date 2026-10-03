#!/data/data/com.termux/files/usr/bin/bash
set -e
pulseaudio --check >/dev/null 2>&1 && pulseaudio --kill
sleep 1
pulseaudio --start --exit-idle-time=-1
sleep 2

# Add AAudio sink alongside the default sles-sink, try it as the low-latency path
pactl load-module module-aaudio-sink || echo "aaudio-sink failed to load, will stay on sles-sink"
echo "--- sinks available ---"
pactl list short sinks

AAUDIO_SINK=$(pactl list short sinks | awk '/aaudio/ {print $2; exit}')
if [ -n "$AAUDIO_SINK" ]; then
  pactl set-default-sink "$AAUDIO_SINK"
  echo "default sink set to $AAUDIO_SINK"
else
  echo "no aaudio sink found, leaving default (sles) sink in place"
fi

# Loopback-only TCP listener for the Fedora chroot to connect to
pactl load-module module-native-protocol-tcp listen=127.0.0.1 port=4713 auth-ip-acl=127.0.0.1 auth-anonymous=1
echo "--- pulseaudio ready, listening on 127.0.0.1:4713 ---"
pactl info | grep -E "Server Name|Default Sink"
