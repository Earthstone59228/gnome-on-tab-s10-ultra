# Deep sleep for the GNOME session (2026-10-07)

**Status: power button = real deep sleep WORKS (confirmed live on the device), auto-start deployed, several items still open (end of page).**
Everything was audited by fresh agents (4 passes, every one found real bugs). Scripts live in `experiments/power/`.

## Result in one paragraph
With the daemon running, a power press blanks the screen and about 4 s later the tablet enters s2idle with the display powered **off** (the only suspend this kernel offers). A second power press, VOL UP, or a held key wakes it, restores the display and unblanks; the daemon never re-sleeps until it has seen the screen unblanked. The VOL UP + VOL DOWN panic chord still works asleep (proven twice). Android restores cleanly after the session. The Book Cover Keyboard keeps working across sleep.

## Why it did not work before
* The power button only toggled the `screen-blank@fedora-tab` black layer; panel on, CPUs busy, Android held `FULL_WAKE_LOCK` believing the screen was on (~3 W flat overnight, earlier power notes).
* `/sys/power/mem_sleep` = `[s2idle]` only. Android itself suspends fine (thousands of cycles).
* **Test A (suspend with the display on) wedged and the project's own softdog panicked the kernel.** `last_kmsg` timeline: `PM: suspend entry (s2idle)` → `dpc_pm unbalanced`, `Runtime PM usage count underflow` → WARNING in `drm_kms_helper_poll_disable` ← `mtk_drm_sys_suspend` → the suspend task sleeps forever in `mtk_atomic_commit [mediatek_drm]` → 100 s later `softdog: Initiating panic` (`softdog.soft_margin=100`, fed from frozen userspace). 
## Root cause (Samsung kernel source, `Kernel.tar.gz` → `kernel_device_modules-6.1/drivers/gpu/drm/mediatek/mediatek_v2/`)
1. `mtk_drm_pm_notifier` sets `kernel_pm.status = SUSPEND` at `PM_SUSPEND_PREPARE`; `mtk_atomic_commit` then blocks (`wait_event_interruptible(kernel_pm.wq, status != SUSPEND)`).
2. `mtk_drm_sys_suspend` runs `drm_atomic_helper_suspend` (itself an atomic commit) **only if a CRTC wakelock `disp_crtc0_wakelock` is active** → self-deadlock.
3. `mtk_drm_crtc_suspend` (the CRTC disable) releases that wakelock inside the synchronous commit. Stock Android turns the display off through SF/HWC first, so `sys_suspend` is a no-op. In our session mutter keeps the CRTC on.
**Fix = turn the display off first, the way Android does:** mutter `org.gnome.Mutter.DisplayConfig PowerSaveMode=3` (blocking atomic commit, CRTC `ACTIVE=0`; mutter 50.5 `meta_kms_device_disable`). `dpms=Off` alone is not proof (connector->dpms is set before the CRTC is disabled); the real readiness check is `/proc/mtkfb` → `CRTC0 wk active:0`.

## Components (repo: `experiments/power/`; on the tablet: `/data/local/tmp/`)
| File | Role | md5 (deployed) |
|---|---|---|
| `fedora-sleepd.sh` | daemon: follows `org.fedoratab.ScreenBlank.Blanked` (gdbus monitor → state file), 4 s settle, fresh Get, runs the engine, never re-arms until unblanked, instance lock, gives up after 3 engine failures, retries PowerSaveMode 0 if the engine reports the display is stuck off | 31c48358… |
| `fedora-suspend-dpms.sh [secs] [total]` | engine: key precheck → PowerSaveMode 3 → wait for `wk active:0` → RTC alarm (30 s) → retry `echo mem` on EBUSY/`clkchk` → per-cycle guards → restore (8 tries, exit 2 if display won't return) → Unblank | 5d710d01… |
| `keystate.py` (chroot `/usr/local/bin/`) | EVIOCGKEY sampler: VOL UP (`gpio-keys`), VOL DOWN + POWER (`mtk-pmic-keys`); rc 0 held / 1 none / 2 error | f4f4bc5d… |
| supervisor patch (`experiments/power/supervisor-sleepd.patch`) | autostart from the monitor on "liveness armed" (opt-out `touch /data/local/tmp/no-sleepd`); teardown before the Android restore | deployed `ea0b618b…`, backup `fedora-session-gnome-shell.sh.pre-sleepd-20261007` = `598b853b…` |

Engine per-cycle guards: key state before blanking, before **every** `echo mem`, and after every resume (any held key or unreadable state = wake, which protects the panic chord); user-wake reasons are checked **before** benign ones; power-key log count; state-file check; gnome-shell-alive check; log rotation at 1 MB.

## Wake reasons observed (`/sys/kernel/wakeup_reasons/last_resume_reason`)
| Reason | Meaning | Loop action |
|---|---|---|
| `594 CCIF_AP_DATA0` | modem (CCCI), ~every 2 s | keep sleeping |
| `-1 … IRQ 317 vcp_mboxdev`, `547 MBOX_SCP_ISR`, `-1 … IRQ 605 adsp_mailbox` | VCP / SCP / audio-DSP mailboxes (adsp up to ~every 0.8 s) | keep sleeping |
| `44 A96T3X6` | grip sensor (fires when the tablet is held) | keep sleeping |
| `306 rcs_irq` | **PMIC interrupt line = POWER key** (`spmi-pmic-irq 48/51 mtk-pmic-keys`, +1 per edge) | user wake |
| `255 Volume_Up` | **VOL UP** (`gpio-keys`) | user wake |
| `707 spm-irq` | unknown (0.0006 s immediate wake) | user wake (fail-safe) |
A combined reason such as `44 A96T3X6;306 rcs_irq` once hid a power press behind the benign grip pattern → user-wake patterns are tested first. Device-tree facts: VOL UP and POWER have `wakeup-source`; VOL DOWN (PMIC `home` node, code 114) does **not**, so the chord wakes the tablet through VOL UP.

## Live test log (all 2026-10-07)
* 07:54 test D #1: display off + `echo mem` → `EBUSY` (`alarmtimer.0.auto`) once, `clkchk` once, then success; retry loop added.
* 07:56–08:00 ~108 s2idle cycles in one session, no panic, no framework restart, display restored every time.
* 08:31 chord asleep (after the key guard): loop ended on `255 Volume_Up` + both keys seen held, chord ended the session, restore clean.
* 08:42 / 08:45 power button wake: reproduced twice.
* 09:14–09:18 daemon: blank → sleep (5, 19 and 5 cycles; 7 s, 50 s, 12 s) → power / VOL UP wake → unblank, no re-sleep loop.
* 09:22 chord with the daemon running: worked; the tester was already holding the chord when the screen blanked, so the engine's key precheck aborted before sleeping (rc 1), daemon exited when gnome-shell went away, lock removed, no leftover processes.
* 09:35–09:37 with the **Book Cover Keyboard attached**: types before sleep, 26 s sleep, power wake, types after wake, no popup/beep; a keyboard key does **not** wake the tablet (expected: not a wake source).
* Cover (hall) test: **not valid** — the tester had already blanked the screen with the power button, and the switch probe (`swprobe.py`, EVIOCGSW poll) saw no change in a 30 s window; `fake_logind` has never logged a `book cover closed/opened` line (flagged in September). Still open.

## Audit trail (what each fresh-agent pass caught)
1. Helper A–C: alarm-armed check, trap handling, restore-on-failure.
2. DPMS-first helper: readiness must use `/proc/mtkfb`; stale RTC base; trap semantics; bus hardening.
3. Key guard: **function used before it was defined** (`sh -n` cannot catch it); `timeout`; fail-safe exit codes.
4. Daemon + engine: late power Toggle re-blanking after Unblank; press in the pre-suspend window; psm 0 failure left the display off with rc 0; gnome-shell restart; stale monitor state; two instances; polling cost.
5. Diff review: stale lock after a crash; re-resolve gnome-shell in the daemon retry; atomic state file; force dpms On after any engine return.
6. Supervisor patch: stale deployed daemon (no retry loop); **reap the engine before clearing the RTC alarm** (a SIGKILL does not abort a suspend already entering); add panel-state logging around the restore.

## Operations
* Start by hand (needed only if the session started without the patched supervisor): `<root shell> "setsid sh /data/local/tmp/fedora-sleepd.sh >> /data/local/tmp/sleepd.out 2>&1 &'`. Logs: `/data/local/tmp/sleepd.log`, engine `/data/local/tmp/suspend-test.log`.
* Stop: `touch /data/local/tmp/sleepd.stop` — mksh defers signals, so it only takes effect after the next wake (or within the 4 s settle when idle); a sleeping daemon cannot be stopped without waking the tablet.
* Disable autostart: `touch /data/local/tmp/no-sleepd`. Rollback: copy the `.pre-sleepd-20261007` supervisor backup back (atomic `mv`, never overwrite in place while a session runs).
* If the display stays dark: power button; or in the session `gdbus … Set org.gnome.Mutter.DisplayConfig PowerSaveMode <int32 0>`; panic chord; hardware force-restart (vol-down + power) always works independently of Linux.

## Gotchas learned
* The session supervisor's time-box timer does not advance while suspended; a session with sleep cycles lasts longer than requested (a `sup 720` session is up to 12 h of awake time).
* Never overwrite the supervisor/runner scripts in place during a session (sh reads incrementally); deploy with an atomic rename.
* The softdog (100 s) is the backstop for a hung suspend and fired exactly as designed in test A.

## Open / unverified
1. **Cover (hall) wake and blank** — never observed; needs a clean test (screen on, close cover; read `fake-logind.log` for `book cover closed`).
2. **Time-box or session end while asleep** — the supervisor now logs `pre-restore` / `post-restore panel state` (dpms + `CRTC0 wk`); whether HWC/SF re-enable the panel from a CRTC-off state is unproven (restore script has no explicit dpms step). Test with a short session ending mid-sleep.
3. **Battery A/B** (not measured yet). Sleeps are still cut to ~1–2 s by modem (`CCIF_AP_DATA0`) and audio-DSP wakes; duty cycle was estimated ~25 % awake, vs 100 % before. Likely Android keeps the modem reporting because it thinks the screen is on; untried levers: radio off (snapshot settings first), masking the wake, fewer awake-side calls per cycle.
4. VOL DOWN alone is not a wake source (a VOL DOWN-only hold is only noticed at the next wake, ≤ the 30 s RTC alarm).
5. SIGKILL of the engine mid-`echo mem` untested (only stubs).
6. The repo's `src/android/fedora-session-gnome-shell.sh` is a different base from the deployed supervisor; the sleep patch is provided as a diff, not merged into `src/`.
