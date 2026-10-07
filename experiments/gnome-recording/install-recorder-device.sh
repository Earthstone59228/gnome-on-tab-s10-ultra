#!/system/bin/sh
# Apply only the tested recorder resource; does not restart GNOME or Android.
set -eu
R=/data/fedora/usr/share/gnome-shell/org.gnome.Shell.Screencast.src.gresource
S=/data/local/tmp/gnome-screencast-mtk-v3.gresource
B=$R.pre-mtk-hwenc-20261007
original=89f8954d2302e8c9eb22b4fcfe587ebddf2c51f1de64565358e2e43b6239a0a0
candidate=cb8d431acd9f1370ec7ff9f3f87d30703132b9025ecd17161363feceafd51b88
recorder_file_sha256() { sha256sum "$1" | cut -d " " -f 1; }
recorder_idle() {
 processes=$(ps -A -o NAME,ARGS) || { echo "Cannot read process inventory" >&2; return 1; }
 if printf '%s\n' "$processes" | awk '$1 == "gjs" && index($0, "/usr/share/gnome-shell/org.gnome.Shell.Screencast") {found=1} END {exit !found}'; then
  echo "Recorder service is active; wait for it to exit" >&2; return 1
 else
  parse_rc=$?
  [ "$parse_rc" = 1 ] || { echo "Cannot parse process inventory" >&2; return 1; }
 fi
 return 0
}

[ "$(recorder_file_sha256 "$S")" = "$candidate" ] || { echo "Staged candidate checksum mismatch"; exit 2; }
current=$(recorder_file_sha256 "$R")
[ "$current" = "$original" ] || {
 [ "$current" = "$candidate" ] && { echo "Hardware recorder already installed"; exit 0; }
 echo "Installed recorder differs from pinned GNOME 50.5 source; refusing"; exit 3
}
recorder_idle || exit 4
if [ -e "$B" ]; then
 [ "$(recorder_file_sha256 "$B")" = "$original" ] || { echo "Backup checksum mismatch"; exit 5; }
else
 cp -p "$R" "$B"
fi
cp -p "$R" "$R.mtk-new"
cat "$S" > "$R.mtk-new"
[ "$(recorder_file_sha256 "$R.mtk-new")" = "$candidate" ] || exit 6
recorder_idle || exit 4
mv "$R.mtk-new" "$R"
sync
[ "$(recorder_file_sha256 "$R")" = "$candidate" ] || exit 7
echo "Installed DMA hardware recorder; original backed up"
