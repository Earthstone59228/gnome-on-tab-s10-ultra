from pathlib import Path
p=Path('/tmp/mobilegl-20261001/MobileGL/MG_Util/BackendLoaders/OpenGL/Loader.cpp');s=p.read_text();mark='#define INIT_GLES_FUNC(name)'
pos=s.index(mark)
s=s[:pos]+'''        void* explicitGLES = nullptr;
        if (const char* provider = std::getenv("MOBILEGL_GLES_LIBRARY")) {
            explicitGLES = dlopen(provider, RTLD_LOCAL | RTLD_NOW);
            if (!explicitGLES) {
                MGLOG_F("Explicit GLES backend failed: %s: %s", provider, dlerror());
                return;
            }
        }
        auto resolveGLES = [&](const char* name) {
            void* core = explicitGLES ? dlsym(explicitGLES, name) : nullptr;
            return core ? core : reinterpret_cast<void*>(procAddress(name));
        };

'''+s[pos:]
s=s.replace('procAddress(#name)', 'resolveGLES(#name)');p.write_text(s)
