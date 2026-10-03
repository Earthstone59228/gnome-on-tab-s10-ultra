#!/bin/bash
# Reproducible host build of the Wayland/glvnd libhybris stack for the gts10u Fedora chroot
# (2026-09-25). x86_64 host -> aarch64 glibc via clang + a koji RPM sysroot + the NDK's ld.lld.
# Output: $S/instA (non-glvnd libEGL.so.1 etc. for mutter) and $S/instB (glvnd vendor
# libEGL_libhybris.so.0 for apps); both --libdir=/usr/local/lib/hybris-wl.
set -euo pipefail
REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
S=${S:-$PWD/work}
# audit F18: the clang/configure command strings below cannot carry whitespace; refuse before changing anything.
AH_CHECK=${AH:-}
for _p in "$S" "$AH_CHECK"; do
  case "$_p" in *[[:space:]]*) echo "build.sh: path contains whitespace ('$_p'); set S=/space-free/dir (and AH likewise)" >&2; exit 1;; esac
done
mkdir -p "$S/rpms" "$S/sysroot"; cd "$S/rpms"
K=https://kojipkgs.fedoraproject.org/packages
for u in glibc/2.43/8.fc44/aarch64/glibc-2.43-8.fc44 glibc/2.43/8.fc44/aarch64/glibc-devel-2.43-8.fc44 \
  kernel-headers/7.2.4/300.fc45/aarch64/kernel-headers-7.2.4-300.fc45 \
  gcc/16.2.1/2.fc44/aarch64/{gcc,libstdc++,libstdc++-devel,libstdc++-static,libgcc}-16.2.1-2.fc44 \
  wayland/1.26.0/1.fc44/aarch64/{libwayland-client,libwayland-server,libwayland-egl,libwayland-cursor,wayland-devel}-1.26.0-1.fc44 \
  libglvnd/1.7.0/9.fc44/aarch64/{libglvnd,libglvnd-devel,libglvnd-egl,libglvnd-gles,libglvnd-core-devel,libglvnd-glx,libglvnd-opengl}-1.7.0-9.fc44 \
  libffi/3.5.2/2.fc44/aarch64/{libffi,libffi-devel}-3.5.2-2.fc44; do
  [ -f $(basename $u).aarch64.rpm ] || curl -sfLO $K/$u.aarch64.rpm; done
cd "$S/sysroot"; for r in ../rpms/*.rpm; do bsdtar -xf $r; done
[ -L lib64 ] || { mv lib64/* usr/lib64/ 2>/dev/null || true; rm -rf lib64; ln -s usr/lib64 lib64; }; ln -sfn usr/lib lib
SR=$S/sysroot; LLD=/opt/android-ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/ld.lld
AH=${AH:?path to android-headers, copied to a path without spaces}
if [ ! -d "$S/hyb-src" ]; then
  git clone https://github.com/libhybris/libhybris.git "$S/hyb-src"
  git -C "$S/hyb-src" checkout 7079712
  for p in "$REPO"/src/hybris/patches/*.patch; do
    git -C "$S/hyb-src" apply "$p"
  done
fi
(cd $S/hyb-src/hybris && autoreconf -fi)
export PKG_CONFIG_SYSROOT_DIR=$SR PKG_CONFIG_LIBDIR=$SR/usr/lib64/pkgconfig
export CC="clang --target=aarch64-redhat-linux --sysroot=$SR -fuse-ld=$LLD" CXX="clang++ --target=aarch64-redhat-linux --sysroot=$SR -fuse-ld=$LLD"
export CFLAGS="-O2 -g1 -D_FORTIFY_SOURCE=2" CXXFLAGS="-O2 -g1 -D_FORTIFY_SOURCE=2 -Wno-non-pod-varargs" LDFLAGS="-Wl,-rpath,/usr/local/lib/hybris-wl"
for t in A B; do rm -rf $S/b$t $S/inst$t; mkdir $S/b$t; cd $S/b$t
  $S/hyb-src/hybris/configure --host=aarch64-linux-gnu --prefix=/usr/local --libdir=/usr/local/lib/hybris-wl \
    --with-android-headers=$AH --enable-arch=arm64 --enable-wayland --with-default-egl-platform=null $([ $t = B ] && echo --enable-glvnd)
  for d in common hardware ui gralloc libsync platforms egl glesv2; do
    make -j$(nproc) -C $d WAYLAND_SCANNER=/usr/bin/wayland-scanner CPPFLAGS="-I$AH -I$S/b$t/platforms/common"
    make -C $d install DESTDIR=$S/inst$t WAYLAND_SCANNER=/usr/bin/wayland-scanner CPPFLAGS="-I$AH -I$S/b$t/platforms/common"; done; done
# libui_compat_layer.so (bionic): NDK clang against the DEVICE's /system/lib64/{libui,libnativewindow}.so
# /opt/android-ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/aarch64-linux-android34-clang -shared -fPIC -O2 -Wall -Wextra \
#   -Wl,-soname,libui_compat_layer.so -o libui_compat_layer.so "$REPO/src/hybris/ui_compat_ahb.c" libui.so libnativewindow.so -llog -Wl,--no-undefined
