# Third-party components

`integration/gnome-shell-fixes` contains modified GNOME Shell 50.4 JavaScript resource modules. GNOME Shell is licensed under GPL-2.0-or-later; the corresponding license text is in `LICENSES/GPL-2.0-or-later.txt`. The changes here are overlays for this tablet's GNOME session.

`src/hybris/patches` contains patches against libhybris commit `7079712`; libhybris itself is obtained from its upstream repository. The `src/hybris/build.sh` recipe also downloads Fedora packages from Fedora Koji. Keep upstream notices and licenses when building or redistributing those components.

`experiments/graphics/gl4es-hybris-wayland.patch` applies to [ptitSeb/gl4es](https://github.com/ptitSeb/gl4es) at the pinned commit in that directory. The upstream gl4es source is MIT licensed and is not copied into this repository.

`config/input/60-input-id.rules` is from eudev 3.2.14. The eudev project publishes it under GPL-2.0-or-later; the license text is in `LICENSES/GPL-2.0-or-later.txt`. The other input rules and quirk are local device configuration.

`experiments/graphics/mobilegl` contains patches and scripts for [MobileGL](https://github.com/MobileGL-Dev/MobileGL) at the commit in `source-pin.txt`. MobileGL is licensed under the LGPL (`COPYING.LESSER` in its source tree); its source and built library are not copied into this repository, and its patches here apply to that upstream code. The upstream third-party submodules it pulls in keep their own licenses.

The Fedora distribution, Android system libraries, Samsung firmware, Mali graphics libraries, and device images are not included in this repository.
