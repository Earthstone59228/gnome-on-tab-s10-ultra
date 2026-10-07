# Deep-sleep experiment (failed, 2026-10-07)

`fedora-suspend-test.sh <A|B|C> [secs]` arms the RTC wake alarm, optionally powers the panel down (mode C), then writes `mem` to `/sys/power/state`. Run it from a root shell during a session, detached (adb may drop).

Result: mode A (panel powered, 10 s RTC wake, SurfaceFlinger stopped, GNOME running) did **not** hit a kernel fault. `last_kmsg` shows suspend entering `s2idle`, a warning in `drm_kms_helper_poll_disable` called from `mtk_drm_sys_suspend`, and then the suspending task blocked forever in `mtk_atomic_commit` (the MediaTek DRM driver's own "disable everything" commit), with display power-controller counters unbalanced (`dpc_pm unbalanced`, `Runtime PM usage count underflow`, `idlemgr_async_put: invalid put w/o get`). Suspend never completed, so the RTC alarm could not resume it, and ~100 s later the software watchdog (`softdog.soft_margin=100`, fed from frozen userspace) panicked the kernel. The watchdog worked as designed; the bug is that the display pipeline state is owned by a userspace compositor with SurfaceFlinger/HWC stopped.

Modes B and C were not run. Next idea: bring the display to an idle, powered-down state through the compositor before suspending (so the driver's suspend commit is a no-op) and extend or pause the watchdog feeder around the suspend. Capture `/proc/last_kmsg` before the next attempt. Do not run this on a daily-driver tablet without a supervised recovery plan.

## Update: display-off-first works (test D)

Samsung's kernel source explains the failure: the MediaTek DRM PM notifier blocks all atomic commits from `PM_SUSPEND_PREPARE`, and `mtk_drm_sys_suspend` issues its own atomic commit whenever a CRTC wakelock (`disp_crtc0_wakelock`) is still active, which deadlocks. Disabling the CRTC first releases that wakelock (`mtk_drm_crtc_suspend`), and the suspend callback becomes a no-op, which is what stock Android does.

`fedora-suspend-dpms.sh [secs] [total]` asks mutter for `PowerSaveMode=3` (an atomic CRTC `ACTIVE=0` commit), waits until `/proc/mtkfb` reports `CRTC0 wk active:0`, arms the RTC alarm, then retries `echo mem` on the transient `EBUSY` (`alarmtimer`) and `clkchk` refusals, as Android's suspend service does. About 108 s2idle cycles ran in a live session with no panic and the display restored each time. Each sleep currently lasts only ~2 s because the modem raises `CCIF_AP_DATA0` roughly every 2 s. Power-button wake and integration into the screen-blank flow are not done.
