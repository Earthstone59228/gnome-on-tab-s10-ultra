#!/system/bin/sh
set -eu
R=/data/fedora; D=/tmp/mobilegl-20261001
if pidof gnome-shell >/dev/null; then echo 'Refuse: GNOME running'; exit 1; fi
mkdir -p "$R$D/labwc" "$R$D/xdg"; chmod 700 "$R$D/xdg"
cat > "$R$D/labwc/autostart" <<'APP'
#!/bin/sh
echo "DISPLAY=$DISPLAY" > /tmp/mobilegl-20261001/normal-display.env
echo $$ > /tmp/mobilegl-20261001/normal-app.pid
exec /usr/bin/prusa-slicer --datadir /tmp/mobilegl-20261001/profile --gcodeviewer /tmp/mobilegl-20261001/cube.gcode > /tmp/mobilegl-20261001/normal-app.log 2>&1
APP
chmod 755 "$R$D/labwc/autostart"
runcon u:r:untrusted_app:s0 chroot "$R" /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/usr/sbin HOME=/tmp XDG_RUNTIME_DIR="$D/xdg" WLR_BACKENDS=headless WLR_RENDERER=pixman WLR_LIBINPUT_NO_DEVICES=1 WLR_HEADLESS_OUTPUTS=1 MOBILEGL_X11_COPY_TRACE=1 /usr/bin/labwc -C "$D/labwc" > /data/local/tmp/mobilegl-normal-labwc.log 2>&1 &
LP=$!; trap 'kill -9 "$LP" 2>/dev/null || true' EXIT
sleep 12
APPID=$(cat "$R$D/normal-app.pid")
DISPLAY=$(cut -d= -f2 "$R$D/normal-display.env")
echo "NORMAL_APP_PID=$APPID DISPLAY=$DISPLAY SF=$(pidof surfaceflinger)"
grep -E 'libMobileGL|libGLES_mali.so|hybris-wl/libGLESv2' /proc/$APPID/maps | head -8
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin XDG_RUNTIME_DIR="$D/xdg" WAYLAND_DISPLAY=wayland-0 /usr/bin/grim "$D/normal-first.png"
WIN=$(chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin DISPLAY="$DISPLAY" /usr/bin/xdotool search --onlyvisible --name 'PrusaSlicer G-code Viewer' | head -1)
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin DISPLAY="$DISPLAY" /usr/bin/xdotool windowmove "$WIN" 0 0 windowsize "$WIN" 1100 760
sleep 2
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin DISPLAY="$DISPLAY" /usr/bin/xdotool mousemove 550 400 mousedown 1
sleep 0.3
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin DISPLAY="$DISPLAY" /usr/bin/xdotool mousemove 620 440
sleep 0.3
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin DISPLAY="$DISPLAY" /usr/bin/xdotool mouseup 1
sleep 2
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin XDG_RUNTIME_DIR="$D/xdg" WAYLAND_DISPLAY=wayland-0 /usr/bin/grim "$D/normal-resized.png"
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin DISPLAY="$DISPLAY" /usr/bin/xdotool windowclose "$WIN"
sleep 3
if [ -e /proc/$APPID ]; then echo NORMAL_CLOSE=STILL_RUNNING; kill -9 "$APPID"; else echo NORMAL_CLOSE=EXITED; fi
cp "$R$D/normal-first.png" /data/local/tmp/mobilegl-normal-first.png
cp "$R$D/normal-resized.png" /data/local/tmp/mobilegl-normal-resized.png
tail -6 "$R$D/normal-app.log"
echo SF_AFTER=$(pidof surfaceflinger)
