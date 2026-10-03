#!/system/bin/sh
set -eu
R=/data/fedora
mkdir -p "$R/tmp/mobilegl-20261001"
tar -xzf /data/local/tmp/mobilegl-20261001-source.tar.gz -C "$R/tmp/mobilegl-20261001"
cp /data/local/tmp/mobilegl-patch-loader.py "$R/tmp/mobilegl-20261001/patch-loader.py"
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin /usr/bin/python3 /tmp/mobilegl-20261001/patch-loader.py
cp /data/local/tmp/mobilegl-patch-gles.py "$R/tmp/mobilegl-20261001/patch-gles.py"
chroot "$R" /usr/bin/python3 /tmp/mobilegl-20261001/patch-gles.py
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin /usr/bin/cmake -S /tmp/mobilegl-20261001 -B /tmp/mobilegl-20261001/build -DMOBILEGL_BUILD_TEST=OFF -DMOBILEGL_BUILD_BENCHMARK=OFF -DMOBILEGL_BUILD_INTEGRATION_TEST=OFF -DCMAKE_BUILD_TYPE=Release -DVulkan_LIBRARY=/usr/lib64/libvulkan.so.1 -DVulkan_INCLUDE_DIR=/tmp/mobilegl-20261001/3rdparty/Vulkan-Headers/include
chroot "$R" /usr/bin/env PATH=/usr/bin:/usr/sbin /usr/bin/cmake --build /tmp/mobilegl-20261001/build --target MobileGL -j4
