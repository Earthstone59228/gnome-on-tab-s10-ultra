#!/system/bin/sh
set -eu
R=/data/fedora; D=/tmp/mobilegl-20261001
if pidof gnome-shell >/dev/null; then echo 'Refuse: GNOME running'; exit 1; fi
cp /data/local/tmp/mobilegl-glx-triangle.c "$R$D/glx-triangle.c"
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin /usr/bin/gcc -O2 -I"$D/include" "$D/glx-triangle.c" -L"$D/build" -lMobileGL -l:libX11.so.6 -Wl,-rpath,"$D/build" -o "$D/glx-triangle"
mkdir -p "$R/tmp/.X11-unix"; chmod 1777 "$R/tmp/.X11-unix"
runcon u:r:untrusted_app:s0 chroot "$R" /usr/bin/env -i PATH=/usr/bin:/usr/sbin HOME=/tmp /usr/bin/Xvfb :98 -screen 0 1280x900x24 -nolisten tcp > /data/local/tmp/mobilegl-xvfb.log 2>&1 &
XP=$!; trap 'kill -9 "$XP" 2>/dev/null || true' EXIT
sleep 2
set +e
runcon u:r:untrusted_app:s0 chroot "$R" /usr/bin/env -i PATH=/usr/bin:/usr/sbin HOME=/tmp DISPLAY=:98 MOBILEGL_X11_COPY=1 MOBILEGL_X11_COPY_TRACE=1 MOBILEGL_BACKEND_TYPE=DirectGLES MOBILEGL_GLES_LIBRARY=/usr/local/lib/hybris-wl/libGLESv2.so.2.0.0 MOBILEGL_EGL_LIBRARY=/usr/local/lib/hybris-wl/libEGL.so.1.0.0 LD_LIBRARY_PATH=/usr/local/lib/hybris-vendor:/usr/local/lib/hybris-wl LD_PRELOAD=/usr/local/lib/libtls-padding.so LIBEGL=/usr/local/lib/hybris-vendor/libGLES_mali.so LIBGLESV2=/usr/local/lib/hybris-vendor/libGLES_mali.so HYBRIS_EGLPLATFORM=null /usr/bin/timeout -s KILL 30 "$D/glx-triangle"
echo GLX_EXIT=$?
echo SF=$(pidof surfaceflinger)
