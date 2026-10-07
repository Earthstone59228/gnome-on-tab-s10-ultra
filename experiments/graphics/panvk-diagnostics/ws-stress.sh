#!/bin/sh
# Fast glitch detector: Layout "rest" frame vs llvmpipe reference after repeated 11-workspace cycles.
# usage: ws-stress.sh TAG CYCLES "ENV=VAL ..." "blender extra args"   (needs $D/cpu/001.png from menu-stress.sh cpu)
set -eu
TAG=$1; CYCLES=$2; VENV=${3-}; EXTRA=${4-}
D=/tmp/blender-menu-stress-20261006; B=/tmp/blender-render-fix-20261005
O=$D/ws-$TAG; mkdir -p "$O/config"; rm -f "$O/results.csv" "$O/progress.log"
export PATH=/usr/bin:/usr/sbin
df=$(mktemp /tmp/ws-stress-display.XXXXXX); xp=; ap=
trap 'for p in $ap $xp; do kill -KILL $p 2>/dev/null || true; done; rm -f "$df"' EXIT HUP INT TERM
Xvfb -displayfd 3 -screen 0 1400x1000x24 -nolisten tcp 3>"$df" >"$O/xvfb.log" 2>&1 & xp=$!
i=0; while [ ! -s "$df" ]; do i=$((i+1)); [ $i -lt 10 ] || exit 1; sleep 1; done
export DISPLAY=":$(cat "$df")"
cp "$D/cpu/init.py" "$O/init.py"
if [ "${CPUMODE-}" = 1 ]; then RUNNER="$B/direct-cpu-env cpu"; BE=opengl; else RUNNER="$B/vulkan-env gpu"; BE=vulkan; fi
BLENDER_USER_CONFIG="$O/config" sh $RUNNER env OCIO=/opt/blender-render-fix-20261005/ocio-4.5.3/config.ocio $VENV \
  timeout -s KILL 1200 /usr/bin/blender --gpu-backend $BE $EXTRA --factory-startup --window-geometry 0 0 1400 1000 \
  --python "$O/init.py" >"$O/app.log" 2>&1 & ap=$!
sleep 25; kill -0 $ap
SLP=$(cat "$D/ws-sleep" 2>/dev/null || echo 2.5)
cyc=0
while [ $cyc -lt $CYCLES ]; do
  cyc=$((cyc+1))
  xdotool mousemove 700 500; sleep 0.3; xdotool key Escape; sleep $SLP
  import -window root -depth 8 "$O/cur.png"
  ae=$(compare -metric AE -fuzz 8% "$O/cur.png" "$D/cpu/001.png" null: 2>&1 | awk '{print int($1)}' || echo -1)
  echo "$cyc,$ae" >> "$O/results.csv"
  cp "$O/cur.png" "$O/rest-c$cyc.png"
  k=0; while [ $k -lt 11 ]; do xdotool key ctrl+Next; sleep $SLP; k=$((k+1)); done
done
echo DONE > "$O/progress.log"
