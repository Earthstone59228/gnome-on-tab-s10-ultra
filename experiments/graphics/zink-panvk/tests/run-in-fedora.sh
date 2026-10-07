#!/bin/sh
set -eu
cd /tmp/zink-theory-20261005
export PATH=/usr/bin:/usr/sbin
gcc -O2 -Wall -Wextra -Werror -Iinclude zink_pixels.c -o zink_pixels -Wl,-l:libepoxy.so.0 -Wl,-l:libEGL.so.1
sha256sum zink_pixels.c zink_pixels
for mode in control gpu; do
    if [ "$mode" = control ]; then
        icd=/usr/share/vulkan/icd.d/lvp_icd.aarch64.json
        software=1
    else
        icd=/opt/panvk-unaudited-20261004/candidate-aafix/icd.json
        software=0
    fi
    [ -r "$icd" ] || { echo "Missing ICD: $icd"; exit 1; }
    echo "BEGIN $mode"
    set +e
    /usr/bin/env -i PATH=/usr/bin:/usr/sbin HOME=/tmp/zink-theory-20261005 \
        __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json \
        VK_DRIVER_FILES="$icd" VK_LOADER_LAYERS_DISABLE='~implicit~' \
        PAN_I_WANT_A_BROKEN_VULKAN_DRIVER=1 PANVK_KBASE_DRI3=0 \
        LIBGL_ALWAYS_SOFTWARE="$software" \
        GALLIUM_DRIVER=zink MESA_LOADER_DRIVER_OVERRIDE=zink EGL_PLATFORM=surfaceless \
        /usr/bin/timeout -s KILL 30 ./zink_pixels "$mode" >"$mode.log" 2>&1
    rc=$?
    set -e
    cat "$mode.log"
    echo "END $mode rc=$rc"
    printf '%s\n' "$rc" >"$mode.exit"
    # A broken software control makes a hardware result difficult to interpret.
    if [ "$rc" -ne 0 ]; then exit "$rc"; fi
done
