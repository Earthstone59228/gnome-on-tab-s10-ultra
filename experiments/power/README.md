# Deep-sleep experiment (failed, 2026-10-07)

`fedora-suspend-test.sh <A|B|C> [secs]` arms the RTC wake alarm, optionally powers the panel down (mode C), then writes `mem` to `/sys/power/state`. Run it from a root shell during a session, detached (adb may drop).

Result: mode A (panel powered, 10 s RTC wake, SurfaceFlinger stopped, GNOME running) caused a **kernel panic**. Do not run this on a daily-driver tablet. Modes B and C were not run. Untested hypotheses: DRM/Mali state owned by the userspace compositor does not survive the driver's suspend callbacks; Android services expect SurfaceFlinger. Capture pstore/kdump output before the next attempt.
