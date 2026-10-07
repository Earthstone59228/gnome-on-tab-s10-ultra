#!/system/bin/sh
set -eu
R=/data/fedora/usr/share/gnome-shell/org.gnome.Shell.Screencast.src.gresource
B=$R.pre-mtk-hwenc-20261007
expected=89f8954d2302e8c9eb22b4fcfe587ebddf2c51f1de64565358e2e43b6239a0a0
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

[ "$(recorder_file_sha256 "$B")" = "$expected" ] || exit 2
current=$(recorder_file_sha256 "$R")
[ "$current" = "$expected" ] || [ "$current" = "$candidate" ] || { echo "Unknown installed recorder; refusing rollback" >&2; exit 3; }
recorder_idle || exit 4
cp -p "$B" "$R.rollback"
[ "$(recorder_file_sha256 "$R.rollback")" = "$expected" ] || exit 5
recorder_idle || exit 4
mv "$R.rollback" "$R"
sync
[ "$(recorder_file_sha256 "$R")" = "$expected" ] || exit 6
sha256sum "$R"
