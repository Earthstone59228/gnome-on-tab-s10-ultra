# Inert session IME (experiment, NOT deployed by default)

Avoids the Android handwriting-service crash that ends a GNOME session when Samsung Keyboard is
evicted and rebinds while SurfaceFlinger is stopped. A session-only dummy IME advertises no
handwriting support and creates no window; the original keyboard is snapshotted and restored.

Status (2026-10-07): built and statically audited (see `AUDIT.md`, `HANDOFF.md`). The helper's lock
was fixed after a live failure (Android mksh makes `exec 9>file` close-on-exec, toybox `flock` is
fd-only) and re-audited. The supervised Android-only admission test has NOT passed on a device, so
integration (`stage/install-dummy-ime.sh`) must not be run. Never test with GNOME active.

Signing: `export IME_KS_PASS=<local passphrase>` then `./build.sh`; the key stays in `private/`.
