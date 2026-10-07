#!/system/bin/sh
set -u
R=/data/fedora/usr/share/gnome-shell/org.gnome.Shell.Screencast.src.gresource
B=$R.pre-mtk-hwenc-20261007
[ -f "$B" ] || exit 2
expected=89f8954d2302e8c9eb22b4fcfe587ebddf2c51f1de64565358e2e43b6239a0a0
actual=$(sha256sum "$B" | cut -d " " -f 1)
[ "$actual" = "$expected" ] || exit 3
P=$(pidof gnome-shell)
case "$P" in ""|*" "*) exit 4;; esac
rollback() {
 cp -p "$B" "$R.rollback" && mv "$R.rollback" "$R"
 sync
 sha256sum "$R"
}
trap rollback EXIT
cp /data/local/tmp/gnome-screencast-mtk-v3.gresource "$R.mtk-new" || exit 5
chmod 644 "$R.mtk-new" || exit 6
mv "$R.mtk-new" "$R" || exit 7
cp /data/local/tmp/gnome-hw-autotest.py /data/fedora/tmp/gnome-hw-autotest.py || exit 8
sync
nsenter -t "$P" -m -- chroot /data/fedora /usr/bin/python3 /tmp/gnome-hw-autotest.py --gnome-pid "$P" --seconds 4 "$@"
rc=$?
exit "$rc"
