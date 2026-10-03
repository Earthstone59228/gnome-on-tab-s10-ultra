#!/bin/bash
# Wrapper: forward the aarch64 link to the DDK container's ld.lld.
# Mounts /tmp at the same path inside the container so absolute paths
# under /tmp (sysroot, build dir, object files) resolve identically on
# both sides -- no path rewriting needed.
exec podman run --rm -v /tmp:/tmp:Z -w "$PWD" \
  ghcr.io/ylarod/ddk-min:android14-6.1-20260313 \
  /usr/bin/ld.lld "$@"
