# Application graphics experiments

The GNOME Shell compositor works on the Mali GPU. General desktop OpenGL and Vulkan application support is still broken or incomplete. These files record the ongoing app graphics work and are not deployed by `scripts/assemble-stage.sh`.

Two approaches are kept here: **gl4es** (OpenGL 2.1, below) and **[MobileGL](mobilegl)** (modern desktop OpenGL, tested up to a GL 3.3 core context and one real application in isolated displays).

## gl4es

The patch in this directory targets [ptitSeb/gl4es](https://github.com/ptitSeb/gl4es) at the commit in `gl4es-base-commit.txt`. It adapts gl4es's OpenGL 2.1 translation to this device's libhybris/Mali EGL path. The Wayland build used `cmake -DNOX11=ON -DEGL_WRAPPER=ON -DDEFAULT_ES=2 -DNO_GBM=ON` with GCC warning suppressions for incompatible pointer types, integer conversion, and return mismatch. The X11/GLX build is separate and uses a pbuffer copy path (`LIBGL_FB=3`). The two builds must be installed in distinct directories because both produce `libGL.so.1`.

| Launcher | Intended use | Current evidence |
| --- | --- | --- |
| `mali-gl` | gl4es OpenGL 2.1 over GLES, Wayland/EGL | Pixel checks passed offscreen; a live EGL/Wayland probe ran without a crash |
| `mali-gl-x11` | gl4es OpenGL 2.1 over GLES, X11/GLX | OpenSCAD rendered under Xvfb; KiCad starts but its board canvas goes black after resize |
| `mali-gles` | Direct GLES apps through libhybris | Wrapper for the existing Fedora GLES path |
| `mali-vulkan` | Vulkan loader experiment | Not a reliable general app path; Blender fails its hardware feature requirements |

The wrappers require built gl4es libraries, libhybris, and matching vendor libraries in the paths they name. They are not standalone installers. Apps needing OpenGL 3.2 or newer core profiles remain unsupported by gl4es. This experiment does not change the tested GNOME desktop setup.
