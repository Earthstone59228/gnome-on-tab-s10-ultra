#!/bin/sh
set -eu
D=/tmp/mobilegl-20261001
export MOBILEGL_X11_COPY=1 MOBILEGL_BACKEND_TYPE=DirectGLES
export MOBILEGL_GLES_LIBRARY=/usr/local/lib/hybris-wl/libGLESv2.so.2.0.0
export MOBILEGL_EGL_LIBRARY=/usr/local/lib/hybris-wl/libEGL.so.1.0.0
export LD_LIBRARY_PATH="$D/glxlib:/usr/local/lib/hybris-vendor${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LD_PRELOAD="/usr/local/lib/libtls-padding.so${LD_PRELOAD:+:$LD_PRELOAD}"
export LIBEGL=/usr/local/lib/hybris-vendor/libGLES_mali.so LIBGLESV2=/usr/local/lib/hybris-vendor/libGLES_mali.so HYBRIS_EGLPLATFORM=null
export GDK_BACKEND=x11
unset LIBGL_ALWAYS_SOFTWARE GALLIUM_DRIVER MESA_LOADER_DRIVER_OVERRIDE
exec "$@"
