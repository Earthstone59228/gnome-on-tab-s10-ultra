# Repository guide for coding agents

Start with `README.md`, then `docs/ARCHITECTURE.md`, then `docs/DEPLOYMENT.md`. This repository documents a Fedora 44 GNOME session on an already-rooted Galaxy Tab S10 Ultra SM-X926B. Rooting is out of scope. Keep the single Ghostlock sentence and Root My Galaxy link in the README; the approved `src/native/defex_off.c` source is included solely because the tested Fedora runtime needs it.

The current working source snapshot is in `src/android`, `src/fedora`, `src/native`, `src/hybris`, `integration`, and `config`. `experiments/graphics` contains optional work on OpenGL and Vulkan app compatibility (gl4es and MobileGL). The GNOME desktop and GPU compositor work; OpenGL and Vulkan apps are not a supported success criterion today.

When changing the session path, trace both the launch and restore flow. Preserve the supervisor, watchdog, `sfsentinel`, input isolation, and DRM-master check. Do not test display-service changes on a live tablet as a routine repository check. Use syntax checks and staged file review on the host; live testing requires a supervised session and the existing recovery path.

Never commit firmware, device images, extracted proprietary libraries, compiled binaries, logs, credentials, private keys, Wi-Fi profiles, device identifiers, or rooting instructions. Keep the source tree and docs usable without private project notes. Document any missing external device-specific inputs explicitly.
