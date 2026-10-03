#!/system/bin/sh
set -eu
R=/data/fedora
D=/tmp/mobilegl-20261001
cp /data/local/tmp/mobilegl-triangle.c "$R$D/triangle.c"
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin /usr/bin/gcc -O2 -I"$D/include" "$D/triangle.c" -L"$D/build" -lMobileGL -Wl,-rpath,"$D/build" -o "$D/triangle"
echo SURFACEFLINGER_BEFORE=$(pidof surfaceflinger)
set +e
runcon u:r:untrusted_app:s0 chroot "$R" /usr/bin/env -i PATH=/usr/bin:/usr/sbin HOME=/tmp MOBILEGL_BACKEND_TYPE=DirectGLES MOBILEGL_GLES_LIBRARY=/usr/local/lib/hybris-wl/libGLESv2.so.2.0.0 MOBILEGL_EGL_LIBRARY=/usr/local/lib/hybris-wl/libEGL.so.1.0.0 LD_LIBRARY_PATH=/usr/local/lib/hybris-vendor:/usr/local/lib/hybris-wl LD_PRELOAD=/usr/local/lib/libtls-padding.so LIBEGL=/usr/local/lib/hybris-vendor/libGLES_mali.so LIBGLESV2=/usr/local/lib/hybris-vendor/libGLES_mali.so HYBRIS_EGLPLATFORM=null HYBRIS_LOGGING_TARGET=stderr HYBRIS_LOGGING_LEVEL=warning /usr/bin/timeout -s KILL 30 "$D/triangle"
echo TRIANGLE_EXIT=$?
echo SURFACEFLINGER_AFTER=$(pidof surfaceflinger)
