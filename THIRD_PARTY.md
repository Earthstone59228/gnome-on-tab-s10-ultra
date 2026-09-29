# Third-party components

`integration/gnome-shell-fixes` contains modified GNOME Shell 50.4 JavaScript resource modules. GNOME Shell is licensed under GPL-2.0-or-later; the corresponding license text is in `LICENSES/GPL-2.0-or-later.txt`. The changes here are overlays for this tablet's GNOME session.

`src/hybris/patches` contains patches against libhybris commit `7079712`; libhybris itself is obtained from its upstream repository. The `src/hybris/build.sh` recipe also downloads Fedora packages from Fedora Koji. Keep upstream notices and licenses when building or redistributing those components.

The Fedora distribution, Android system libraries, Samsung firmware, Mali graphics libraries, and device images are not included in this repository.
