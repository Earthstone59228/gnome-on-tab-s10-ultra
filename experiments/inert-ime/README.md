# Inert session IME (experiment, NOT deployed by default)

Avoids the Android handwriting-service crash that ends a GNOME session when Samsung Keyboard is
evicted and rebinds while SurfaceFlinger is stopped. A session-only dummy IME advertises no
handwriting support and creates no window; the original keyboard is snapshotted and restored.

Status (2026-10-07): built, audited three times, and the supervised Android-only admission test
PASSED on a device (inert IME bound, killed, rebound as a new process, original keyboard restored,
exact settings restored, system_server unchanged). A separate integration audit approved
`stage/install-dummy-ime.sh`; it was installed and a supervised 2-minute GNOME session completed
cleanly (IME selected, SF stopped, restored, snapshots cleared, watchdog dismissed). Not yet
soaked over long sessions or under real memory pressure. Never run stages with GNOME active.

Live bugs the host tests and first audits missed (all fixed, now covered by on-device checks):
- Android mksh marks `exec 9>file` close-on-exec; toybox `flock` is fd-only -> lock is now a
  `{ ...; } 9>"$LOCK"` group.
- Android has no builtin `printf`; `dumpsys input_method` (~265 KB) exceeds the argv limit -> `p()` uses
  mksh `print -r --`.
- Android binds the selected IME lazily (editor focus / show), so the gate accepts "selected and
  nothing bound" or "selected and bound correctly", never another IME bound. Admission asks the
  operator to focus a text field. Residual risk for integration: the dummy's first bind could
  happen with SurfaceFlinger stopped.

Signing: `export IME_KS_PASS=<local passphrase>` then `./build.sh`; the key stays in `private/`.
