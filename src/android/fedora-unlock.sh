#!/system/bin/sh
# fedora-unlock.sh — emergency unlock of the GNOME lock screen over adb with the 32-digit recovery code.
#   sh /data/local/tmp/fedora-unlock.sh 1234 5678 ...    (run from the Android side, outside the session)
# (spaces are ignored). The code was printed once by fake-gdm-recovery set; only its hash is stored.
# If the lock screen itself is broken, the panic chord (hold VOL UP + VOL DOWN 3 s) still ends the session.
echo "$*" | chroot /data/fedora /usr/bin/python3 /usr/local/bin/fake-gdm-recovery unlock
sleep 1
tail -2 /data/fedora/fake-gdm.log
