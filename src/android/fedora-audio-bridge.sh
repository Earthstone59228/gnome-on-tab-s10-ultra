#!/data/data/com.termux/files/usr/bin/bash
# fedora-audio-bridge.sh (2026-09-27) — started by the Fedora GNOME session supervisor through Termux RUN_COMMAND.
# Idempotent: if this Termux PulseAudio already serves the loopback TCP listener, do nothing; otherwise run the
# normal ~/start-audio-bridge.sh (PulseAudio + AAudio sink + 127.0.0.1:4713 listener for the chroot).
# 2026-09-28 (doc 11 §AE): the check now has a timeout, and a PulseAudio that does not answer is force-killed first.
# Seen live: module-aaudio-sink deadlocks in AAudioStream_close (AudioTrack stop() never returns) -> the daemon still
# listens on 4713 but never answers; the old check (pactl, no timeout) then hung forever, and `pulseaudio --kill`
# in start-audio-bridge.sh cannot stop a deadlocked daemon (its signal handling runs in the stuck main loop).
# Audit 2026-09-28 (doc 11 §AI): a PulseAudio that is still STARTING (the 4713 listener loads last, ~3-5 s) does not
# answer either; a second trigger in that window (late RUN_COMMAND delivery, supervisor re-try) must not kill it.
STAMP="$HOME/.fedora-audio-bridge.starting"
if timeout 5 pactl -s tcp:127.0.0.1:4713 info >/dev/null 2>&1; then   # the listener the chroot needs is already up
	echo "$(date) already running" >> "$HOME/.fedora-audio-bridge.log"
	exit 0
fi
if pgrep -x pulseaudio >/dev/null; then
	age=$(( $(date +%s) - $(stat -c %Y "$STAMP" 2>/dev/null || echo 0) ))
	if [ "$age" -ge 0 ] && [ "$age" -lt 20 ]; then
		echo "$(date) pulseaudio not answering yet, but a start began ${age}s ago — leaving it" >> "$HOME/.fedora-audio-bridge.log"
		exit 0
	fi
	echo "$(date) pulseaudio present but not answering — force-killing it (deadlocked)" >> "$HOME/.fedora-audio-bridge.log"
	pkill -9 -x pulseaudio
	sleep 1
fi
touch "$STAMP"
echo "$(date) starting start-audio-bridge.sh" >> "$HOME/.fedora-audio-bridge.log"
exec bash "$HOME/start-audio-bridge.sh" >> "$HOME/.fedora-audio-bridge.log" 2>&1
