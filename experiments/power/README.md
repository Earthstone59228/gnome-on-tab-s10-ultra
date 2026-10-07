# Deep-sleep experiment (failed, 2026-10-07)

> **STATUS: SHELVED — do not deploy.** This experiment wedged the tablet twice (hard restart + re-root each time). Root cause: a leaked `uart_mutex` in MediaTek's `8250_mtk` serial driver (`mtk8250_set_flush_flag` returns without unlocking when the UART DMA channels are NULL) self-deadlocks with the Bluetooth UART driver's non-freezable TX thread during suspend; the BT suspend notifier, its workers and the HAL then queue behind it, and the display driver's PM notifier blocks every atomic commit, so the screen cannot come back. It is a vendor bug in a locked stock kernel; the engine's ~40 suspends/min hit the ~1-in-3,600 race. The scripts are kept here for reference only; the display-off-first suspend sequence itself (the display-driver fix) worked.
>
> Ideas if someone revives it: sleep only when the BT driver is truly closed (no `btmtk_uart_tx_thread`), refuse to suspend when the driver is already stuck, capture kmsg continuously, cut the suspend rate (airplane mode), and add a sysrq-reboot watchdog.


`fedora-suspend-test.sh <A|B|C> [secs]` arms the RTC wake alarm, optionally powers the panel down (mode C), then writes `mem` to `/sys/power/state`. Run it from a root shell during a session, detached (adb may drop).

Result: mode A (panel powered, 10 s RTC wake, SurfaceFlinger stopped, GNOME running) did **not** hit a kernel fault. `last_kmsg` shows suspend entering `s2idle`, a warning in `drm_kms_helper_poll_disable` called from `mtk_drm_sys_suspend`, and then the suspending task blocked forever in `mtk_atomic_commit` (the MediaTek DRM driver's own "disable everything" commit), with display power-controller counters unbalanced (`dpc_pm unbalanced`, `Runtime PM usage count underflow`, `idlemgr_async_put: invalid put w/o get`). Suspend never completed, so the RTC alarm could not resume it, and ~100 s later the software watchdog (`softdog.soft_margin=100`, fed from frozen userspace) panicked the kernel. The watchdog worked as designed; the bug is that the display pipeline state is owned by a userspace compositor with SurfaceFlinger/HWC stopped.

Modes B and C were not run. Next idea: bring the display to an idle, powered-down state through the compositor before suspending (so the driver's suspend commit is a no-op) and extend or pause the watchdog feeder around the suspend. Capture `/proc/last_kmsg` before the next attempt. Do not run this on a daily-driver tablet without a supervised recovery plan.

## Update: display-off-first works (test D)

Samsung's kernel source explains the failure: the MediaTek DRM PM notifier blocks all atomic commits from `PM_SUSPEND_PREPARE`, and `mtk_drm_sys_suspend` issues its own atomic commit whenever a CRTC wakelock (`disp_crtc0_wakelock`) is still active, which deadlocks. Disabling the CRTC first releases that wakelock (`mtk_drm_crtc_suspend`), and the suspend callback becomes a no-op, which is what stock Android does.

`fedora-suspend-dpms.sh [secs] [total]` asks mutter for `PowerSaveMode=3` (an atomic CRTC `ACTIVE=0` commit), waits until `/proc/mtkfb` reports `CRTC0 wk active:0`, arms the RTC alarm, then retries `echo mem` on the transient `EBUSY` (`alarmtimer`) and `clkchk` refusals, as Android's suspend service does. About 108 s2idle cycles ran in a live session with no panic and the display restored each time. Each sleep currently lasts only ~2 s because the modem raises `CCIF_AP_DATA0` roughly every 2 s. Power-button wake and integration into the screen-blank flow are not done.

## Wake and safety findings (Oct 7, later)

* Benign wake reasons seen while the display is off: modem (`CCIF_AP_DATA0`, ~2 s cadence), `vcp_mboxdev`, `MBOX_SCP_ISR`, `adsp_mailbox` (audio DSP, sometimes every ~0.8 s), grip sensor (`A96T3X6`, fires when the tablet is held). Unclear: `spm-irq`, `rcs_irq` (the loop ends on anything not in its benign list; display returned within about a second each time).
* **The volume-key panic chord does NOT work while the tablet is asleep with this helper.** The chord watcher is a userspace poller (key-state ioctl every 0.5 s, 3 s hold) that only runs in the short awake windows, and a key press landing in an awake window leaves no wake reason. The hardware force-restart and the software watchdog are unaffected. Before this goes anywhere near a power-button integration, the sleep loop itself must sample the volume and power key state every cycle and treat any pressed key as a wake, and cap total sleep time.
* The session supervisor's time-box timer does not advance while suspended, so a session with suspend cycles lasts longer than requested.

## Key-state guard (added after the chord-asleep failure)

`keystate.py` samples the key-state bitmap (same ioctl as the panic-chord watcher: no event reads, no grabs) for VOL UP (`gpio-keys`), VOL DOWN and POWER (`mtk-pmic-keys`). The sleep helper runs it before blanking (refuses to sleep if the state is unreadable), before every suspend, and after every resume; any held key ends the sleep. Hardware facts from the device tree: VOL UP and POWER are wake sources, VOL DOWN (the PMIC `home` node) is not, so the chord wakes the tablet through VOL UP and is then seen as held. A fresh audit caught a function-order bug (the precheck called the sampler before it was defined) that the syntax check could not. Not yet verified live.

Live result (Oct 7): with the guard, the panic chord works while the tablet sleeps. VOL UP (`gpio-keys`) wakes it (wake reason `255 Volume_Up`), the guard sees both keys held and ends the sleep, and the normal chord watcher then ends the session. VOL DOWN alone is not a wake source. The power-button wake path is not verified yet.

Live result (Oct 7): the power button wakes the sleeping tablet. Wake reason `306 rcs_irq` (the PMIC interrupt line; the `mtk-pmic-keys` IRQs count the press and release), reproduced twice. A combined reason such as `44 A96T3X6;306 rcs_irq` must be checked against user-wake reasons before benign ones, otherwise the press is swallowed by the grip-sensor pattern. Still open: wiring the power key into the sleep flow, the 1–2 s modem/audio-DSP wake cadence, a battery comparison, and keyboard-cover behaviour with the display off.

## Sleep daemon (experimental, not live-tested yet)

`fedora-sleepd.sh` follows the screen-blank state (power button / cover), waits a few seconds after the screen blanks, then runs `fedora-suspend-dpms.sh` (display off first, then an s2idle loop with a per-cycle key-state guard). Any user wake (power, VOL UP, a held key, cover) restores the display and unblanks; the daemon will not sleep again until the screen has been seen unblanked. It must be started by hand inside a live session. Because the shell defers signals, a sleeping daemon can only be stopped after a wake (stop file `/data/local/tmp/sleepd.stop`).

Live result (Oct 7): with the daemon running, a power press blanks the screen and, 4 s later, the tablet starts suspending with the display off; a second power press or VOL UP wakes it, restores the display and unblanks, and the daemon does not sleep again until the screen has been seen unblanked. The panic chord still works and the daemon exits when gnome-shell goes away. Not done: automatic start with the session, cover (hall) wake, keyboard cover with the display off, and a battery comparison (sleeps are still cut short to ~1–2 s by modem and audio-DSP wakes).
