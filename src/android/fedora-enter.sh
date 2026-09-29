#!/system/bin/sh
# Fedora Phase-0 session (re)entry — gts10u (SM-X926B).
# Run from an already-rooted shell: sh /data/local/tmp/fedora-enter.sh
# Safe to re-run: every step is idempotent.
# Requires: active root access and a /data/fedora rootfs (persistent on /data).
R=/data/fedora
T=/data/local/tmp

# 1. DEFEX bypass module — required before ANY chroot exec, else every
#    uid-0 exec of a non-/system binary gets SIGKILLed by Samsung DEFEX.
if grep -q defex_off /proc/modules; then
  echo "defex_off: already loaded"
else
  insmod "$T/defex_off.ko" && echo "defex_off: loaded (DEFEX enforcement off)"
fi

# 2. chroot mounts (guard against stacked duplicates)
mkdir -p "$R/dev/pts" "$R/proc" "$R/sys" "$R/run"
grep -q " $R/dev " /proc/mounts || mount -o bind /dev "$R/dev"
grep -q " $R/dev/pts " /proc/mounts || mount -o bind /dev/pts "$R/dev/pts"
grep -q " $R/proc " /proc/mounts || mount -t proc proc "$R/proc"
grep -q " $R/sys " /proc/mounts || mount -o bind /sys "$R/sys"
grep -q " $R/run " /proc/mounts || mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs "$R/run"
# wlroots needs POSIX shm for keymaps; Android's /dev has none. Missing this
# causes "Failed to allocate shm file for keymap" -> "cannot set keymap" and
# a fatal labwc exit during seat init (found 2026-09-18: this step was never
# actually persisted here before, only ever done ad hoc in a live session,
# and silently regressed on the next reboot).
mkdir -p "$R/dev/shm"
grep -q " $R/dev/shm " /proc/mounts || mount -t tmpfs -o mode=1777 tmpfs "$R/dev/shm"
# Binder must be reachable from the chroot or libbinder falls back to its
# RPC-over-UDS path (/dev/socket/rpc_servicemanager, never present here) and
# retries forever on the main thread — mutter stalls at "Queue mode set"
# (found 2026-09-20: this mount was applied ad hoc during the 2026-09-19
# morning session's bug #2 fix, never persisted here, and silently regressed
# on that evening's panic reboot; same class as the /dev/shm gap above).
# /dev/binderfs is its own separate mount on the Android side, so the plain
# /dev bind above never captures it.
mkdir -p "$R/dev/binderfs"
grep -q " $R/dev/binderfs " /proc/mounts || mount -o bind /dev/binderfs "$R/dev/binderfs"
echo "mounts: $(grep -c " $R/" /proc/mounts) fedora entries"

# 3. sshd (port 22 = :0016 in /proc/net/tcp hex)
mkdir -p "$R/run/sshd"
if grep -q ":0016 " /proc/net/tcp /proc/net/tcp6 2>/dev/null; then
  echo "sshd: already listening on :22"
else
  chroot "$R" /usr/sbin/sshd && echo "sshd: started on :22"
fi

echo "access: adb forward tcp:2222 tcp:22 && ssh -p 2222 root@localhost (key auth)"

# 4. GNOME switcher (2026-09-27): VOL UP, DOWN, UP, DOWN within 2 s in unlocked Android starts the GNOME session.
#    Single instance; opt-out: touch /data/local/tmp/no-gnome-switcher
if [ -f "$T/gnome-switcher.sh" ] && [ ! -e "$T/no-gnome-switcher" ]; then
  setsid sh "$T/gnome-switcher.sh" </dev/null >/dev/null 2>&1 &
  echo "gnome switcher: armed (VOL UP, DOWN, UP, DOWN within 2 s in unlocked Android)"
fi
