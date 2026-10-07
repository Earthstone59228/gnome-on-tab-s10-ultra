# Inert Android session IME — staged, independent audit underway, NOT DEPLOYED

No tablet APK installation, settings mutation, process killing, display change, or runtime admission has occurred. All work lives in this directory; the source repository is untouched. The hardware recorder is separate and remains rolled back.

## Reviewed build / artifacts
- `app/`: public SDK Android 36 Java/manifest/resources. `InertIme` extends **AbstractInputMethodService**, avoiding inherited `InputMethodService` SoftInputWindow UI. It retains no InputConnection, creates no view/window, handles no keys, and advertises both handwriting flags false. A protected dump prints only a constant schema plus its own PID/UID, permitting proof that the current bound Binder belongs to a newly rebound process.
- `./build.sh`: AAPT2, javac, D8, zipalign and APK v3 signing. Local signing key in `private/` is not for publication.
- `build/fedora-session-inert-ime.apk`; identical staged APK.
- `stage/fedora-session-ime.sh`: exact setting snapshots, binding admission, original restoration and hash-pinned future launch gate.
- `supervisor.patch`, `settings.patch`: minimal diff against deployed baseline, with complete staged counterparts. `stage/fedora-restore.sh` is an unchanged reference and is NOT installed.
- `stage/SHA256SUMS`: manifest for the APK and eight mutable staged shell files. Any change invalidates prior runtime admission.
- `AUDIT.md`: independent auditor findings and exact Samsung lifecycle trace. Audit agent separately validates the final revision.

## Three distinct future stages
All review markers and runtime evidence are intentionally absent. These are instructions for a later supervised Android session, NOT permission to run while GNOME is active or the owner is absent.

1. `stage/install-dummy-ime-apk.sh`: requires the independent auditor's `APK_ADMISSION_REVIEWED` marker, real SF/HWC, no GNOME/sentinel/session/IME snapshot and no existing owner package. Installs only the fresh APK; it performs no keyboard selection and changes no launch scripts. A root-owned receipt pins the installed APK for eventual rollback.
2. `stage/admit-dummy-ime.sh supervised-android-only`: foreground, supervised test with real SF/HWC. Requires the same review marker and receipt. Snapshots exact default/enabled/subtype/subtype-history presence and values before selecting dummy. Confirms connected false-handwriting dummy, saves token/display, then validates exact dummy PID/cmdline/package UID before killing ONLY that process. Rebind must show a NEW exact dummy process, matching remote *current* Binder dump PID/UID for three consecutive samples, unchanged system_server PID, and unchanged mCurToken/display. EXIT/signal cleanup retries exact original restoration and connected-original verification. Success evidence is written only after originals restored. Restore failure starts a separate IME-only recovery worker (never changes display/framework services). Failed admission cannot authorize integration.
3. `stage/install-dummy-ime.sh`: requires independent `INTEGRATION_REVIEWED` marker **and** runtime `ADMISSION_PASSED` whose exact manifest matches current staged hashes. Verifies installed APK hash and pinned baseline scripts, then installs guard/settings/supervisor and writes an installed-code/APK hash marker. Neither installation stage starts a session. The integrated launcher AND direct `sup` invocation require this marker; missing/stale marker blocks future launch.

`stage/rollback-dummy-ime.sh` supports APK-only, failed admission, partial integration and complete integration. Requires real Android display, no active session, and our receipt. Uses the staged helper to restore any IME originals, verifies the original default is connected before uninstall, verifies installed APK hash belongs to our receipt, restores baseline scripts if backups exist, then uninstalls only our package. It invalidates future-launch/admission markers; receipts/backups remain for evidence. No broad Android reset or Samsung service kill.

## Settings / ordering / automatic recovery
Supervisor prepares dummy while real SF/HWC are up, before existing display-idle and toast waits. Three consecutive bound-user-0 samples must show correct selected/current component, main connection, non-null invoker, both handwriting flags false, and no IME window. A second check immediately precedes SF stop. Installed-method metadata alone cannot pass admission. Stale/incomplete original snapshots and unsupported users/defaults fail closed; no fallback guesses. Kernel flock releases automatically on process death; Binder commands use timeout plus forced KILL because supervisor children inherit ignored TERM.

Restore requires real SF/HWC, no sentinel and successful real SF layer-list command. It selects original IME, restores exact present/absent values, verifies exact bytes, waits for two consecutive original connected binding samples, then rereads exact bytes **again** to detect late subtype observers before deleting originals. Expected text from a failed Binder command cannot pass user/default verification. Plain restore keeps originals across zygote; final removes only after complete verification.

Pre-SF abort retries restoration and retains watchdog/phase on failure. Normal teardown and early post-SF abort retain watchdog through zygote/final verification under a bounded restoring phase. Only verified disappearance of both settings and IME snapshots dismisses recovery. Detached watchdog requires the complete settings helper `restore final` after shared recovery, so a shared restore rc0 alone cannot drop originals. Unrelated protective Android settings still restore even if IME restoration fails. Pending IME originals block a new supervisor launch.

Samsung Keyboard remains enabled; real Android/S Pen devices and GNOME OSK are unchanged. No native sfsentinel or defex module work.

## Pinned source / Samsung trace
Baseline read-only device pulls and MD5 verification:
- supervisor `598b853b0657b06a684cf3739bba9805`
- settings `f46d9f769df0a96f15c6291510db3ca2`
- shared restore `448466e8fcaf972871874adf583d115a`
Actual Samsung commands: `cmd input_method ime enable|set --user 0 COMPONENT`; absent package `cmd package path` returns rc1/empty (other failures rejected). Toybox supports flock and timeout -k.

Independent auditor inspected exact Samsung framework/services jars. False handwriting metadata controls actual firmware initializeHandwritingSpy gate. Public abstract IME default callbacks delegate into our no-op attach/start callbacks; protected dump routes from the current invoker. Inherited WindowProviderService attaches a configuration listener to an existing display and creates a WindowManager wrapper, without directly adding a window/surface. Missing-display recreation and binding-death/package-update paths can allocate Android surfaces; this is **not** universal surface safety. The Android-only new-process/unchanged-token admission targets the observed memory-pressure service-disconnection/rebind path. See independent AUDIT.md for binary hashes and exact trace.

## Host checks / remaining gate
- Build, APK v3 signature, metadata XML, shell syntax (including nested supervisor quoting), and stage hashes pass.
- Builder's nine state-model scenarios pass: exact empty/absent settings; stale snapshots; SF-down refusal; handwriting/set failure; wrong original binding; transient/persistent abort recovery.
- Independent auditor's nine adversarial regressions pass: wrong foreground/dump user; disconnected/null binding; visible/installed-only impostors; late observer; expected output followed by failed commands.
- Admission identity tests reject same PID, wrong UID/name, wrong remote Binder identity and system_server restart. Unreviewed stage scripts stop before device operations.

Host models do NOT prove Samsung runtime compatibility. Independent final source review and later supervised Android-only admission remain mandatory before integration. A subsequent time-boxed SF-off session still uses existing panic/watchdog and stable display. This mitigation targets the handwriting crash trigger; it does not solve GNOME capture memory retention or every Android surface-creation crash.
