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
| `707 spm-irq` | SPM system-timer wake (not a person) | keep sleeping |
| `-1 … IRQ 599 gpueb_mboxdev` | GPU EB mailbox | keep sleeping |
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

## Bug found 2026-10-07 ~11:20 — "it keeps turning on by itself" (FIXED)
Symptom: after sleeping with the power button the display kept coming back on without any press. Cause: the engine treats every wake reason not on its benign list as a user wake (fail-safe). `707 spm-irq` (a system-timer wake; 4 occurrences, ending sleeps after 11 s / 68 s / 131 s / 251 s) and `gpueb_mboxdev` (2 occurrences) were not on the list, so the engine ended the sleep, turned the display on and unblanked. Evidence: `dmesg` `suspend wake up by R12_SYSTIMER_EVENT_B … Resume caused by IRQ 707, spm-irq`. Fix: `*spm-irq*|*mboxdev*|*gpueb*` added to the benign list (user-wake patterns are still tested first, so POWER `rcs_irq` / VOL UP `Volume_Up` and the key-state guard are unaffected). Engine md5 `7b5cedfd…` (backup `fedora-suspend-dpms.sh.pre-spmirq`). Verified live: 5+ minutes asleep, a `spm-irq` wake at 11:33:47 ridden through, no self-wake. Lesson: any new unknown wake reason will end sleep until classified — check `sleepd.log` / `suspend-test.log` (`non-benign wake [...]`) and `dmesg` (`wake up by R12_…`) before allowlisting; never allowlist a reason that could be a button.

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

## INCIDENT 2026-10-07 12:25–13:00 — kernel deadlock in the MediaTek BT UART driver wedged the tablet (REBOOT REQUIRED) — autostart now DISABLED
**What the tester saw:** after ~1 h of good sleeping (3,600+ s2idle cycles, 2,176 engine cycles since 11:40) the screen would not wake with the power button; 30+ min later the display "came back" at 12:57 but touch was dead.
**What actually happened (evidence in (logs kept privately)):** the engine's cycle 2176 (12:25:14) never returned from `echo mem`. Kernel stack of the stuck `sh` (D state): `pm_suspend → pm_notifier_call_chain_robust → btmtk_pm_notification → btmtk_intcmd_system_status → btmtk_main_send_cmd → btmtk_uart_send_and_recv → btmtk_uart_driver_own` (**the BT UART driver's suspend notifier**, which runs BEFORE the freezer, so userspace kept running and adb stayed up). `sysrq-w` shows a multi-task mutex deadlock in `btmtk_uart_unify` / `8250_mtk` / `uarthub_drv`: `btmtk_uart_tx_thread` in `mtk8250_set_flush_flag`, `uarthub_wq trigger_uarthub_error_worker_handler` in `mtk8250_uart_dump`, `UART_RX_WQ flush_to_ldisc` in `mtk8250_uart_dump`, `btmtk_system_state_work` in `driver_own`, all in `mutex_lock`. Consequences: (1) the engine could never reach its "PowerSaveMode 0 + Unblank"; (2) `mtk_drm_pm_notifier` had already set `kernel_pm.status=SUSPEND` at PM_SUSPEND_PREPARE, so every DRM atomic commit blocked → display could not be re-enabled and mutter stalled; (3) gnome-shell stopped answering at 12:56:48, the supervisor ended the session at 12:57:48 (`gnome-shell unresponsive`), gnome-shell became a zombie whose KMS thread is stuck in `drm_release → flush_work`, so DRM master is never released and the Android restore (`fedora-restore.sh`) cannot start HWC/SF. No userspace recovery exists; the tablet needs a hardware restart (and a re-root).
**Why now and not earlier:** the BT notifier runs on EVERY suspend attempt; this engine suspends ~40 times/min (modem `CCIF_AP_DATA0` / mailbox wakes every ~1.5 s), so it exposes a rare race (~1 in 3,600) that stock Android (suspend every few minutes) almost never meets. The BT driver logs `btmtk_wakeup_uarthub ready_retry[49]` on healthy suspends, i.e. it was always near its retry limit.
**Mitigation state:** `touch /data/local/tmp/no-sleepd` was created at 12:59 (survives reboot) so the next session does NOT autostart the daemon. Candidate fixes (not built): (a) take Bluetooth out of the picture before sleeping (Android BT off on sleep, restore on wake — costs Buds audio) so `btmtk_pm_notification` is never hit; (b) cut the suspend rate (hold the SoC awake ≥ N s between cycles / only suspend when no modem traffic) — trades savings for exposure; (c) detect a stuck suspend early (engine heartbeat older than ~20 s while display off) and at least raise an alarm — it cannot be undone from userspace. The open-source drop does not contain `btmtk_uart_unify` (only sdio/usb BT variants), so the lock order could not be read.
**Rule:** do NOT re-enable autostart (`rm /data/local/tmp/no-sleepd`) until mitigation (a) or (b) is implemented and soak-tested on a short session.

## MITIGATION built 2026-10-07 ~14:00 (audited, deployed, NOT yet soak-tested; autostart still OFF via /data/local/tmp/no-sleepd)
**Idea (chosen approach):** keep the BT chip/driver idle while sleeping, so `btmtk_pm_notification` has nothing to deadlock on. `fedora-bt-quiet.sh off|restore|status` (deployed md5 bddf0586…): snapshots `bluetooth_on` + `ble_scan_always_enabled` (both were 1 — Android's "Bluetooth scanning always on" keeps the chip powered even with Bluetooth "off", so it is turned off too) into `/data/local/tmp/sleepd.bt-state`, runs `cmd bluetooth_manager disable` + `wait-for-state:STATE_OFF` (all via `runcon u:r:shell:s0`, same route as android-bridge `bt-on/bt-off`); `restore` puts the exact snapshot back (ble setting first, then `enable` if Bluetooth was on). Serialized by a lock dir `sleepd.bt-lock` (pid + stale check) so a late background restore can never land after the next `off`.
**Wiring:** engine (md5 9cee2666…) runs `off` before blanking and **refuses to sleep if Bluetooth cannot be quieted** (`BT_QUIET=0` = manual-test override), restores in the background after unblank and in its EXIT trap; daemon (md5 6dbee37e…) restores a leftover snapshot at start and in its exit trap; supervisor (md5 d411d1af…, backup `fedora-session-gnome-shell.sh.pre-btquiet-20261007`) restores a leftover snapshot at its start (SF still running, even with no-sleepd), and again at the very end after Android is back. After ANY forced reboot with a snapshot present, run `sh /data/local/tmp/fedora-bt-quiet.sh restore` (or just start a session).
**Audit (fresh agent) GO-WITH-CHANGES, applied:** F1 `.restoring` age check used the snapshot's mtime (mv keeps it) → `touch` at claim; F2 restore↔next-`off` race (could leave BT on while suspending) → lock; F3 reboot with snapshot leaves BT off → restore at supervisor start; F4 teardown restore moved after the Android restore. Host stub tests (`BTQ_STUB=1 BTQ_DIR=…`): concurrent restores issue one restore; restore-then-off ordering correct.
**Unverified (live test needed):** whether the driver is really silent with the chip off (check dmesg for `btmtk_intcmd_system_status` / `btmtk_cif_suspend` per suspend; WITHOUT the mitigation every suspend printed them); that `wait-for-state:STATE_OFF` is reached with ble scan off on this Samsung build (the engine aborts without sleeping if not); that these Android toggles with SurfaceFlinger stopped never pop UI (the bridge precedent says OK).
**Soak plan:** short supervised session (~10 min) with the daemon started by hand: check BT-silence in dmesg, exact settings restore, wakes (power / VOL UP), chord asleep; then a longer soak before removing `no-sleepd`.
