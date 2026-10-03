from pathlib import Path
p=Path('/tmp/mobilegl-20261001/MobileGL/MG_Util/BackendLoaders/OpenGL/Loader.cpp')
s=p.read_text(); old='eglLib = OpenLib({"libEGL.so.1", "libEGL.so"});'
assert s.count(old)==1
s=s.replace(old, '''// Isolated Linux experiment: choose a backend independently of the
            // MobileGL frontend libEGL aliases. Never silently fall back if set.
            if (const char* provider = std::getenv("MOBILEGL_EGL_LIBRARY")) {
                eglLib = dlopen(provider, RTLD_LOCAL | RTLD_NOW);
                if (!eglLib) {
                    MGLOG_F("Explicit EGL backend failed: %s: %s", provider, dlerror());
                    return;
                }
                MGLOG_I("Explicit EGL backend: %s", provider);
            } else {
                eglLib = OpenLib({"libEGL.so.1", "libEGL.so"});
            }''')
p.write_text(s)
