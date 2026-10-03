from pathlib import Path
root=Path('/tmp/mobilegl-20261001/MobileGL')
p=root/'MG_Backend/DirectGLES/BackendObject_DirectGLES.cpp';s=p.read_text();a=s.index('    Bool BackendObject_DirectGLES::CreateEGLPbufferSurface(');b=s.index('    Bool BackendObject_DirectGLES::ResizeEGLWindowSurface(',a)
part=s[a:b].replace('if (m_eglSurfaceInitialized) {','if (m_eglSurfaceInitialized && m_eglSurfaceKind != SurfaceKind::Pbuffer) {');s=s[:a]+part+s[b:];p.write_text(s)
p=root/'MG_Backend/DirectGLES/DirectGLES.cpp';s=p.read_text();needle='''    Bool InitPbufferSurface(EGLint width, EGLint height) {
''';assert needle in s;s=s.replace(needle,needle+'''        // All frontend pbuffers share the backend object registry. Preserve its
        // ES objects when changing GLX canvases; only replace the native surface.
        if (g_Display != EGL_NO_DISPLAY && g_Context != EGL_NO_CONTEXT) {
            return ResizePbufferSurface(width, height);
        }
''');p.write_text(s)
