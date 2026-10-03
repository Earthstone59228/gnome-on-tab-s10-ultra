from pathlib import Path
root=Path('/tmp/mobilegl-20261001/MobileGL')
p=root/'MG_Backend/DirectGLES/DirectGLES.h';s=p.read_text().replace('Bool InitPbufferSurface(EGLint width, EGLint height);','Bool InitPbufferSurface(EGLint width, EGLint height);\n    Bool ResizePbufferSurface(EGLint width, EGLint height);');p.write_text(s)
p=root/'MG_Backend/DirectGLES/DirectGLES.cpp';s=p.read_text();pos=s.index('    namespace {',s.index('    Bool InitPbufferSurface('));s=s[:pos]+'''    Bool ResizePbufferSurface(EGLint width, EGLint height) {
        if (g_Display == EGL_NO_DISPLAY || g_Context == EGL_NO_CONTEXT) return false;
        const EGLint attrs[] = {EGL_WIDTH, width, EGL_HEIGHT, height, EGL_NONE};
        EGLSurface replacement = g_EGLFuncs.eglCreatePbufferSurface(g_Display, g_Config, attrs);
        if (replacement == EGL_NO_SURFACE) return false;
        EGLSurface previous = g_Surface;
        if (!ReleaseCurrent()) { g_EGLFuncs.eglDestroySurface(g_Display, replacement); return false; }
        g_Surface = replacement;
        if (!MakeCurrent()) {
            g_Surface = previous;
            MakeCurrent();
            g_EGLFuncs.eglDestroySurface(g_Display, replacement);
            return false;
        }
        g_EGLFuncs.eglDestroySurface(g_Display, previous);
        PublishDefaultFramebufferDepthStencilFormat();
        return true;
    }

'''+s[pos:];p.write_text(s)
p=root/'MG_Backend/DirectGLES/BackendObject_DirectGLES.h';s=p.read_text().replace('Bool InitPbufferSurface(EGLint width, EGLint height) override;','Bool InitPbufferSurface(EGLint width, EGLint height) override;\n        Bool ResizeEGLWindowSurface(EGLSurface surface, Uint32 width, Uint32 height) override;');p.write_text(s)
p=root/'MG_Backend/DirectGLES/BackendObject_DirectGLES.cpp';s=p.read_text();pos=s.index('    Bool BackendObject_DirectGLES::InitPbufferSurface(');s=s[:pos]+'''    Bool BackendObject_DirectGLES::ResizeEGLWindowSurface(EGLSurface surface, Uint32 width, Uint32 height) {
        const std::lock_guard<std::recursive_mutex> lock(m_eglStateMutex);
        auto it = m_eglSurfaces.find(surface);
        if (it == m_eglSurfaces.end() || it->second.Kind != SurfaceKind::Pbuffer)
            return BackendObject::ResizeEGLWindowSurface(surface, width, height);
        if (m_eglSurface == surface && !DirectGLES::ResizePbufferSurface(width, height)) return false;
        it->second.Width = width;
        it->second.Height = height;
        return true;
    }

'''+s[pos:];p.write_text(s)
p=root/'MG_Impl/GLXImpl/GLXImpl.cpp';s=p.read_text();a=s.index('            if (CopyPresentation()) {',s.index('        void SyncSurfaceSize'));b=s.index('            if (EGLImpl::ResizePlatformWindowSurface',a);s=s[:a]+s[b:];p.write_text(s)
