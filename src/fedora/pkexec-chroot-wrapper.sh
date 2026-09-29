#!/bin/sh
# pkexec pass-through: the chroot has no polkitd, and real pkexec returns
# 127 (not authorized) without it — which mutter sees as a failed backlight
# helper spawn (2026-09-20). Sessions already run as uid 0, so just exec.
# NOTE: $@ in a script already excludes argv[0], so no shift here.
exec "$@"
